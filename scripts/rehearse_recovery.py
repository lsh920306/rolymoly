"""Rehearse real worker process loss/restart in a disposable database only.

No browser or mocked clock participates. --remote creates and removes a fresh
private QA schema; it never accepts an existing schema or database path.
This verifies durable recovery, not Cloud sleep behavior or browser latency.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.competition import Competition, ROLES
from roly.core import Core
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from scripts import rehearse_postgres as harness
from scripts.rehearse_postgres import require
from scripts.test_release import manifest


def validate_target(target):
    """The private worker transport cannot select an operating database."""
    if not isinstance(target, str):
        raise ValueError("Invalid disposable worker target")
    if target.startswith("supabase://"):
        harness.require_qa_schema(target.removeprefix("supabase://"))
        return target
    try:
        path = Path(target).resolve(strict=True)
    except OSError as error:
        raise ValueError("Invalid disposable worker path") from error
    if (path.name != "synthetic.sqlite3" or not path.is_file()
            or not path.parent.name.startswith("roly-db-rehearsal-")):
        raise ValueError("Worker requires a newly generated rehearsal database")
    return str(path)


def worker_main():
    """Private stdin transport; credentials and driver errors never reach logs."""
    logging.disable(logging.CRITICAL)
    live = None
    try:
        payload = json.loads(sys.stdin.read())
        target = validate_target(payload["target"])
        ready = Path(payload["ready"])
        if ready.name != "ready.json" or not ready.parent.name.startswith("roly-worker-control-"):
            raise ValueError("Invalid worker control directory")
        core = Core(target)
        live = LiveAuction(core, Competition(core))
        with patch("roly.riot_api._http_get", side_effect=AssertionError("Riot HTTP disabled")):
            thread = live.ensure_worker(interval=0.1, persistent=True)
            require(thread.is_alive())
            temporary_ready = ready.with_suffix(".tmp")
            temporary_ready.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            temporary_ready.replace(ready)
            while thread.is_alive():
                time.sleep(0.1)
        return 1  # A persistent worker must not exit on its own.
    except Exception:
        return 1
    finally:
        if live is not None:
            live.stop_worker()


class WorkerProcess:
    """A separately killable Python process, never the user's running app."""
    def __init__(self, target):
        self.target = validate_target(target)
        self.process = None
        self.control = None

    def start(self):
        require(self.process is None)
        self.control = tempfile.TemporaryDirectory(prefix="roly-worker-control-")
        ready = Path(self.control.name) / "ready.json"
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        # Windows venv python.exe can be a redirector with a different PID.
        # Launch the actual interpreter and retain this environment's packages
        # so stop() kills the worker itself, not an intermediate launcher.
        executable = sys.executable
        if os.name == "nt":
            executable = getattr(sys, "_base_executable", sys.executable)
            environment["PYTHONPATH"] = os.pathsep.join(str(Path(item).resolve())
                for item in sys.path if item and Path(item).is_dir())
        try:
            self.process = subprocess.Popen(
                [executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "_worker"],
                cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.process.stdin.write(json.dumps({"target": self.target, "ready": str(ready)}).encode())
            self.process.stdin.close()
            limit = time.monotonic() + 60
            while not ready.exists():
                if self.process.poll() is not None or time.monotonic() >= limit:
                    raise RuntimeError("Disposable worker did not become ready")
                time.sleep(0.05)
            require(self.process.poll() is None)
            require(json.loads(ready.read_text(encoding="utf-8"))["pid"] == self.process.pid)
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
            require(self.process.poll() is not None)
        if self.control is not None:
            self.control.cleanup()
            self.control = None


def wait_for(probe, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Disposable recovery condition did not occur")
        time.sleep(0.1)


def exercise(core, report):
    validate_target(core.db_path)
    comp = Competition(core)
    tournament = TournamentService(core, comp)
    live, workers = LiveAuction(core, comp), []

    def start_worker():
        worker = WorkerProcess(core.db_path)
        workers.append(worker)
        return worker.start()

    try:
        with report.stage("fresh_database_personal_accounts_and_member_host"):
            require(not core.has_admin() and core.list_members(True) == [])
            password = secrets.token_urlsafe(24)
            core.setup_admin("recovery_admin", password)
            admin = core.login("recovery_admin", password)
            ids, tokens = [], {}
            for index in range(20):
                password = secrets.token_urlsafe(24)
                result = core.register_member(f"recovery_{index}", password,
                    f"RecoverySynthetic{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5],
                    request_key=str(uuid.uuid4()))
                member = result["member_id"]
                ids.append(member)
                core.approve_member(admin, member, 100)
                if index % 5 == 0 or index == 1:
                    tokens[member] = core.login(f"recovery_{index}", password)
            host, captains = tokens[ids[1]], ids[::5]
            require(core.session(host)["role"] == "member")
            event = tournament.create(host, "Synthetic process recovery", "2030-01-01T20:00:00+09:00",
                build_mode="AUCTION", team_count=4, format_name="TOURNAMENT")
            tournament.open_recruitment(host, event)
            tournament.set_participants(host, event,
                [{"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(ids)],
                expected_roster_token=comp.get_event(event)["roster_token"])
            tournament.confirm_participants(host, event)
            tournament.set_captains(host, event, captains)
            tournament.prepare_auction(host, event)
            teams = comp.get_event(event)["teams"]
            live.configure(host, event, bid_seconds=10,
                team_budgets={team["id"]: 100 for team in teams},
                order=[member for member in ids if member not in captains])
            report.data["counts"].update(personal_accounts=21, member_host=1, captains=4)

        def state():
            return live.get_state(event)  # Read only; never settles deadlines.

        def lot(lot_id):
            with core.read_snapshot() as db:
                return dict(db.execute("SELECT * FROM live_lots WHERE id=?", (lot_id,)).fetchone())

        def until_after(deadline):
            wait_for(lambda: state()["server_now"] > deadline + 0.2)

        def sold_once(lot_id, price):
            current = state()
            closed = next(row for row in current["lots"] if row["id"] == lot_id)
            team = next(row for row in current["teams"] if row["id"] == closed["highest_team_id"])
            require(closed["status"] == "SOLD")
            require(len([row for row in current["events"] if row["lot_id"] == lot_id and row["type"] == "SOLD"]) == 1)
            require(len([row for row in team["players"] if row["member_id"] == closed["member_id"] and row["price"] == price]) == 1)
            require(team["remaining"] == 100 - sum(row["price"] for row in team["players"]))
            return closed

        with report.stage("kill_only_worker_before_real_deadline"):
            first_worker = start_worker()
            live.start(host, event)
            first_lot = state()["current_lot"]["id"]
            receipt = live.place_bid(tokens[captains[0]], event, first_lot, 10, str(uuid.uuid4()))
            first_worker.stop()
            require(state()["server_now"] < receipt["closes_at"])

        with report.stage("no_worker_means_no_early_or_client_triggered_settlement"):
            until_after(receipt["closes_at"])
            require(lot(first_lot)["status"] == "OPEN")
            require(all(team["remaining"] == 100 for team in state()["teams"]))

        with report.stage("new_process_recovers_expired_sale_once_without_browser"):
            start_worker()
            wait_for(lambda: lot(first_lot)["status"] == "SOLD")
            first_closed = sold_once(first_lot, 10)
            require(first_closed["closed_at"] >= receipt["closes_at"])
            require(next(team for team in state()["teams"] if team["captain_id"] == captains[0])["remaining"] == 90)

        with report.stage("next_player_waits_at_least_three_seconds"):
            def next_open():
                current = state()
                candidate = current["current_lot"]
                return candidate if current["status"] == "RUNNING" and candidate["id"] != first_lot else None
            second_lot = wait_for(next_open)
            gap = second_lot["opened_at"] - first_closed["closed_at"]
            require(gap >= 3)
            report.data["counts"]["observed_transition_seconds"] = round(gap, 3)
            live.pause(host, event)
            paused = state()
            require(paused["status"] == "PAUSED" and paused["paused_phase"] == "RUNNING")

        with report.stage("pause_survives_process_loss_and_original_deadline"):
            for worker in workers:
                worker.stop()
            until_after(second_lot["closes_at"])
            start_worker()
            start_worker()  # Independent workers share one database.
            time.sleep(0.3)
            current = state()
            require(current["status"] == "PAUSED" and current["paused_phase"] == "RUNNING")
            require(current["pause_remaining"] == paused["pause_remaining"])
            require(current["current_lot"]["id"] == second_lot["id"] and current["current_lot"]["status"] == "OPEN")
            require(all(worker.process.poll() is None for worker in workers[-2:]))

        with report.stage("resume_two_workers_and_disconnected_client_settle_once"):
            live.resume(host, event)
            second_request = str(uuid.uuid4())
            second_receipt = live.place_bid(tokens[captains[1]], event, second_lot["id"], 20, second_request)
            # From this point, the parent performs no auction writes or ticks.
            wait_for(lambda: lot(second_lot["id"])["status"] == "SOLD")
            sold_once(second_lot["id"], 20)
            sold_once(first_lot, 10)
            require(next(team for team in state()["teams"] if team["captain_id"] == captains[1])["remaining"] == 80)
            replay = live.resolve_bid(tokens[captains[1]], event, second_lot["id"], 20, second_request)
            require(replay["id"] == second_receipt["id"] and replay["replayed"])
            sold_once(second_lot["id"], 20)
            report.data["counts"].update(worker_processes_started=len(workers), simultaneous_workers=2,
                paid_sales=2, sale_events=2, total_spent=30, duplicate_sales=0,
                browser_sessions=0, parent_settle_due_calls=0)
    finally:
        for worker in workers:
            worker.stop()
        live.stop_worker()
        report.data["worker_processes_stopped"] = all(worker.process is None or worker.process.poll() is not None for worker in workers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sqlite", action="store_true")
    mode.add_argument("--remote", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    before, generated = manifest(), []
    original = harness.new_qa_schema
    def new_schema():
        schema = original()
        harness.require_qa_schema(schema)
        generated.append(schema)
        return schema
    with patch.object(harness, "new_qa_schema", side_effect=new_schema), \
            patch.object(harness, "exercise", side_effect=exercise), \
            patch("roly.riot_api._http_get", side_effect=AssertionError("Riot HTTP disabled")), \
            redirect_stdout(StringIO()):
        code = harness.main(["--remote" if args.remote else "--sqlite", "--output", str(args.output)])
    report = json.loads(args.output.read_text(encoding="utf-8"))
    absent = not args.remote
    try:
        if args.remote:
            from roly.postgres import connect
            absent = len(generated) == 1
            for schema in generated:
                harness.require_qa_schema(schema)
                with connect(schema) as db:
                    absent = absent and db.execute("SELECT 1 FROM pg_namespace WHERE nspname=?", (schema,)).fetchone() is None
    except Exception as error:
        absent = False
        report["absence_check_error_type"] = type(error).__name__
    after = manifest()
    changed = [key for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
    report.update(qa_schema_absence_verified=absent, qa_schemas_checked=len(generated),
        source_before_sha256=before, source_sha256=after, changed_during_rehearsal=changed,
        source_unchanged=not changed, operating_user_data_accessed=False, riot_http_disabled=True,
        mocked_clock=False, browser_latency_verified=False, cloud_sleep_verified=False)
    report["ok"] = bool(code == 0 and report.get("ok") and report.get("cleaned_up") and absent
        and report.get("worker_processes_stopped") and not changed)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items()
        if key not in ("source_before_sha256", "source_sha256")}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(worker_main() if sys.argv[1:] == ["_worker"] else main())
