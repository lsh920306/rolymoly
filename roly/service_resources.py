"""Versioned process resources with an explicit background-worker handover.

Streamlit hashes a cached factory's own source, not imported service classes.
Only this stable registry is a cache_resource; each entry explicitly includes
the backend source revision and private storage binding. It contains no member
data, tokens or authorization decisions. Callers still authenticate every action.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import streamlit as st

ROOT = Path(__file__).resolve().parent
BACKEND_FILES = ("core.py", "auth.py", "competition.py", "tournament.py", "live_auction.py",
                 "postgres.py", "member_profile.py", "member_ranks.py", "riot_profile.py",
                 "riot_sync.py", "riot_api.py", "result_revision.py", "lounge.py",
                 "storage_config.py", "deployment_config.py", "service_resources.py")
HANDOVER_ERROR = "서비스 업데이트를 마무리하고 있습니다. 잠시 후 다시 시도해 주세요."


@st.cache_resource(show_spinner=False)
def _registry():
    # Keep this factory independent of service implementation changes so the
    # old owner/thread remains reachable after a Streamlit source rerun.
    return {"lock": threading.RLock(), "bundles": {}, "riot": {}, "signature": None, "revision": None}


def backend_revision():
    state = _registry()
    with state["lock"]:
        paths = [ROOT / name for name in BACKEND_FILES]
        signature = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
        if signature != state["signature"]:
            digest = hashlib.sha256()
            for path in paths:
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
            state.update(signature=signature, revision=digest.hexdigest())
        return state["revision"]


def storage_binding(path):
    """Never load operational Secrets for a local SQLite resource."""
    if not str(path).startswith("supabase://"):
        return hashlib.sha256(str(path).encode()).hexdigest()
    from .storage_config import postgres_kwargs
    # Includes credential rotation; this opaque digest stays in server memory.
    return hashlib.sha256(json.dumps(postgres_kwargs(), sort_keys=True).encode()).hexdigest()


def _stop_riot(entry):
    workers, lock = entry["workers"]
    with lock:
        worker = workers.get(entry["path"])
        if worker:
            worker["stop"].set()
    if worker:
        worker["thread"].join(timeout=16)
        if worker["thread"].is_alive():
            raise sqlite3.OperationalError(HANDOVER_ERROR)


def _stop_live(bundle):
    live = bundle["live"]
    # Use the old owner's class registry, even if the current module has a new
    # LiveAuction class. stop_worker() alone does not report a join timeout.
    with live._worker_lock:
        worker = live._workers.get(live.core.db_path)
    live.stop_worker()
    if worker and worker["thread"].is_alive():
        raise sqlite3.OperationalError(HANDOVER_ERROR)


def _retire_riot(state, path=None):
    for name, entry in list(state["riot"].items()):
        if path is None or path in (entry["path"], entry["rate_path"]):
            _stop_riot(entry)
            state["riot"].pop(name)


def _retire_previous_revision(state, revision):
    for name, entry in list(state["riot"].items()):
        if entry["key"][0] != revision:
            _stop_riot(entry)
            state["riot"].pop(name)
    for name, bundle in list(state["bundles"].items()):
        if bundle["key"][0] != revision:
            _stop_live(bundle)
            state["bundles"].pop(name)


def service_bundle(path):
    path = str(path)
    key = (backend_revision(), storage_binding(path))
    state = _registry()
    with state["lock"]:
        _retire_previous_revision(state, key[0])
        current = state["bundles"].get(path)
        if current and current["key"] == key:
            return current
        if current:
            _retire_riot(state, path)
            _stop_live(current)
            state["bundles"].pop(path)
        from .core import Core
        from .competition import Competition
        from .live_auction import LiveAuction
        core = Core(path)
        competition = Competition(core)
        live = LiveAuction(core, competition)
        bundle = {"key": key, "core": core, "competition": competition, "live": live}
        # Publish the owner before any subsequent request can create a worker.
        # Keep it available for retirement even if the first active-state read fails.
        state["bundles"][path] = bundle
        try:
            if live.has_active_sessions():
                live.ensure_worker()
        except BaseException:
            _stop_live(bundle)
            state["bundles"].pop(path)
            raise
        return bundle


def clear_services(path=None):
    state = _registry()
    with state["lock"]:
        _retire_riot(state, str(path) if path is not None else None)
        for name, bundle in list(state["bundles"].items()):
            if path is None or name == str(path):
                _stop_live(bundle)
                state["bundles"].pop(name)


def riot_resource(path, key, rate_path, factory):
    state = _registry()
    with state["lock"]:
        _retire_previous_revision(state, key[0])
        previous = state["riot"].get(path)
        if previous and previous["key"] == key:
            return previous["sync"]
        if previous:
            _stop_riot(previous)
            state["riot"].pop(path)
        from . import riot_sync
        sync = factory()
        entry = {"path": path, "rate_path": rate_path, "key": key, "sync": sync,
                 "workers": (riot_sync._workers, riot_sync._worker_lock)}
        state["riot"][path] = entry
        try:
            sync.ensure_worker()
        except BaseException:
            _stop_riot(entry)
            state["riot"].pop(path)
            raise
        return sync


def clear_riot():
    state = _registry()
    with state["lock"]:
        _retire_riot(state)
