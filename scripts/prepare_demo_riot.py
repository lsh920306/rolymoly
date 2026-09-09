"""Explicitly prepare the supplied demo roster's public Riot snapshot.

Member/job/cache data exists only in a temporary SQLite database. The configured
Supabase database is used solely for the application-key rate budget (apart from
Core's idempotent schema initialization). No background worker is started.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from roly.core import Core, identity
from roly.demo import DEMO_RIOT_IDS
from roly.riot_api import load_riot_config
from roly.riot_profile import public_profile
from roly.riot_sync import RiotSync

ERROR_CODES = frozenset(("disabled", "auth", "not_found", "rate_limited", "unavailable",
    "invalid_response", "invalid_request", "profile_changed", "time_limit"))


def require_enabled(config):
    if not config.enabled or not getattr(config, "allow_demo", False):
        raise ValueError("demo_riot_disabled")


def prepare_profiles(riot_ids, config, rate_core, *, max_seconds=240, client_factory=None,
                     clock=None, monotonic=time.monotonic, sleep=time.sleep, progress=None,
                     temporary_parent=None):
    """Return public data and count-only diagnostics; never expose job payloads."""
    require_enabled(config)
    if type(max_seconds) not in (int, float) or not 1 <= max_seconds <= 240:
        raise ValueError("invalid_time_limit")
    identities = [identity(value) for value in riot_ids]
    if not 1 <= len(identities) <= 40 or len({value[1] for value in identities}) != len(identities):
        raise ValueError("invalid_demo_roster")
    report = {"total": len(identities), "succeeded": 0, "failed": 0,
              "time_limit_seconds": max_seconds, "processed_stages": 0,
              "temporary_database_deleted": False, "operating_member_access": False,
              "operating_writes": "shared Riot rate budget only; Core schema initialization is idempotent"}
    previous_progress = None
    def emit(code):
        nonlocal previous_progress
        message = {"code": code, "total": report["total"], "succeeded": report["succeeded"],
                   "failed": report["failed"], "pending": report["total"] - report["succeeded"] - report["failed"]}
        if progress and message != previous_progress:
            progress(message)
        previous_progress = message

    profiles, errors = {}, {}
    temporary = tempfile.TemporaryDirectory(prefix="roly-demo-riot-", dir=temporary_parent)
    temporary_path = Path(temporary.name)
    try:
        core = Core(temporary_path / "profiles.sqlite3")
        password = secrets.token_urlsafe(30)
        core.setup_admin("snapshot_operator", password)
        token = core.login("snapshot_operator", password)
        member_ids = []
        for riot_id, _ in identities:
            member_id = core.join_member(riot_id, "TOP", "JG")
            core.approve_member(token, member_id, 0)
            member_ids.append(member_id)
        sync = RiotSync(core, config, client_factory=client_factory, clock=clock, rate_core=rate_core)
        sync.enqueue(token, member_ids)
        started = monotonic()
        deadline = started + max_seconds
        emit("queued")
        while len(profiles) + len(errors) < len(member_ids):
            states = sync.get_profiles(member_ids)
            for member_id, (_, canonical) in zip(member_ids, identities):
                if canonical in profiles or canonical in errors:
                    continue
                state = states.get(member_id, {})
                if state.get("status") == "DONE":
                    profile = public_profile(state)
                    if profile:
                        profiles[canonical] = profile
                    else:
                        errors[canonical] = "invalid_response"
                elif state.get("status") == "FAILED":
                    code = state.get("last_error")
                    errors[canonical] = code if code in ERROR_CODES else "unavailable"
            report.update(succeeded=len(profiles), failed=len(errors))
            emit("processing")
            if len(profiles) + len(errors) == len(member_ids):
                break
            if monotonic() >= deadline:
                for _, canonical in identities:
                    if canonical not in profiles and canonical not in errors:
                        errors[canonical] = "time_limit"
                break
            # A rejected/expired key cannot make progress until configuration
            # changes. Do not clear a shared 429 cooldown or retry in a loop.
            cooldown = sync.limiter.status()
            if cooldown["blocked"]:
                for _, canonical in identities:
                    if canonical not in profiles and canonical not in errors:
                        errors[canonical] = "auth"
                break
            if sync.process_one():
                report["processed_stages"] += 1
            else:
                emit("waiting")
                sleep(min(1.0, max(0.0, deadline - monotonic())))
        report.update(succeeded=len(profiles), failed=len(errors),
                      error_counts=dict(Counter(errors.values())),
                      elapsed_seconds=round(monotonic() - started, 3),
                      ok=not errors)
        emit("complete" if not errors else "complete_with_errors")
        return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "profiles": profiles, "errors": errors}, report
    finally:
        temporary.cleanup()
        report["temporary_database_deleted"] = not temporary_path.exists()


def write_json(path, document):
    """Publish a complete snapshot atomically, retaining an old file on error."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _published_profile(value, generated_at):
    profile = public_profile(value)
    if not profile:
        return None
    # Keep the original observation time when an older snapshot relied on its
    # document timestamp. A later failed refresh must not make it look new.
    observed = profile["updated_at"] or generated_at
    try:
        if datetime.fromisoformat(observed).tzinfo is None:
            return None
    except (ValueError, TypeError):
        return None
    profile["updated_at"] = observed
    return profile


def publish_snapshot(path, fresh, riot_ids):
    """Merge successful observations; failed refreshes keep the last good data."""
    path = Path(path)
    allowed = {identity(value)[1] for value in riot_ids}
    previous = {}
    try:
        if path.stat().st_size <= 2_000_000:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
    except (OSError, UnicodeError, ValueError):
        pass
    def profiles(document):
        values = document.get("profiles", {})
        if not isinstance(values, dict):
            return {}
        result = {}
        for canonical, value in values.items():
            if canonical in allowed:
                profile = _published_profile(value, document.get("generated_at"))
                if profile:
                    result[canonical] = profile
        return result
    old_profiles, successful = profiles(previous), profiles(fresh)
    retained = len(set(old_profiles) - set(successful))
    if not successful:
        return {"snapshot_written": False, "retained_profiles": len(old_profiles),
                "published_profiles": len(old_profiles), "updated_profiles": 0,
                "publication_code": "no_success_kept_previous_file"}
    errors = {}
    for document in (previous, fresh):
        values = document.get("errors", {})
        if isinstance(values, dict):
            errors.update({canonical: code for canonical, code in values.items()
                           if canonical in allowed and isinstance(code, str) and code in ERROR_CODES})
    for canonical in successful:
        errors.pop(canonical, None)
    old_profiles.update(successful)
    write_json(path, {"generated_at": fresh["generated_at"], "profiles": old_profiles, "errors": errors})
    return {"snapshot_written": True, "retained_profiles": retained,
            "published_profiles": len(old_profiles), "updated_profiles": len(successful),
            "publication_code": "merged_successful_profiles"}


def source_hashes():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in
            ("roly/core.py", "roly/riot_api.py", "roly/riot_sync.py", "roly/riot_profile.py", "roly/demo.py")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-seconds", type=int, default=240)
    args = parser.parse_args(argv)
    report = {"ok": False, "source_before": source_hashes(), "snapshot_written": False}
    try:
        # Check the opt-in before even constructing the shared operating backend.
        config = load_riot_config()
        require_enabled(config)
        if not 1 <= args.max_seconds <= 240:
            raise ValueError("invalid_time_limit")
        if len(DEMO_RIOT_IDS) != 22:
            raise ValueError("invalid_demo_roster")
        rate_core = Core("supabase://rolymoly")
        snapshot, result = prepare_profiles(DEMO_RIOT_IDS, config, rate_core,
            max_seconds=args.max_seconds, progress=lambda message: print(json.dumps(message), flush=True))
        publication = publish_snapshot(ROOT / "static" / "demo-riot-profiles.json", snapshot, DEMO_RIOT_IDS)
        report.update(result, **publication)
    except Exception as error:
        # No exception text, URL, credentials, PUUID or private payload is printed.
        report.update(ok=False, error_type=type(error).__name__)
    report["source_unchanged"] = source_hashes() == report["source_before"]
    write_json(ROOT / "test-artifacts" / "demo-riot-preparation.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
