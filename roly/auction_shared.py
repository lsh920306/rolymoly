"""Bounded process-local display reuse with an authoritative read on every call.

Only public event data is retained. Session tokens, actor rows and authorization
decisions are never cached. Each returned identity and version is from one DB
snapshot; caller-owned copies cannot mutate another viewer's cached display.
"""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import threading
from time import monotonic


@dataclass(repr=False)
class _Entry:
    lock: object = field(default_factory=threading.Lock)
    users: int = 0
    touched: float = field(default_factory=monotonic)
    state: object = None
    version: object = None


class SharedSnapshots:
    def __init__(self, *, max_entries=32, idle_seconds=30, reader=None):
        if type(max_entries) is not int or max_entries < 1 or idle_seconds <= 0:
            raise ValueError("Shared display cache limits must be positive.")
        self.max_entries, self.idle_seconds, self.reader = max_entries, idle_seconds, reader
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._stats = {"reads": 0, "full_states": 0, "allocation_states": 0, "hot_states": 0,
                       "unchanged": 0, "evictions": 0, "capacity_fallbacks": 0}

    @contextmanager
    def _entry(self, live, event_id):
        # Keep the live owner in the key: two storage bindings/credential
        # generations must never share a version-matched but unrelated row.
        key = (live, event_id)
        with self._lock:
            stamp = monotonic()
            for old_key, old in list(self._entries.items()):
                if not old.users and stamp - old.touched > self.idle_seconds:
                    self._entries.pop(old_key)
                    self._stats["evictions"] += 1
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.max_entries:
                    victim = next((k for k, value in self._entries.items() if not value.users), None)
                    if victim is not None:
                        self._entries.pop(victim)
                        self._stats["evictions"] += 1
                entry = _Entry()
                if len(self._entries) < self.max_entries:
                    self._entries[key] = entry
                else:
                    # All slots are pinned by active readers. Perform a fresh
                    # uncached read rather than grow storage or serve stale data.
                    self._stats["capacity_fallbacks"] += 1
            if key in self._entries:
                self._entries.move_to_end(key)
            entry.users += 1  # Pins queued readers as well as the lock holder.
        try:
            with entry.lock:
                yield entry
        finally:
            with self._lock:
                entry.users -= 1
                entry.touched = monotonic()

    def snapshot(self, live, event_id, tokens=(), *, base_state=None, base_version=None, changed_hint=False):
        from .auction_state import age_state, read_snapshot, _finish
        if type(event_id) is not int or not 0 < event_id <= 2**53 - 1:
            raise ValueError("Invalid shared auction event.")
        with self._entry(live, event_id) as entry:
            result = (self.reader or read_snapshot)(live, event_id, tokens,
                base_state=entry.state, base_version=entry.version, changed_hint=changed_hint)
            if entry.version and result["revision"] < entry.version["revision"]:
                raise ValueError("Auction display revision moved backwards.")
            state = result["state"] if result["changed"] else entry.state
            version = {key: result.get(key) for key in ("revision", "detail_revision", "display_revision")}
            # Publish only a completed successful read. Exceptions leave the
            # last good display available for a later fresh authorization read.
            entry.state, entry.version = state, version
            with self._lock:
                self._stats["reads"] += 1
                kind = ("full_states" if result.get("display_changed") else "allocation_states"
                        if result["details_changed"] else "hot_states" if result["changed"] else "unchanged")
                self._stats[kind] += 1
            changed = base_version is None or version["revision"] != base_version.get("revision")
            details = base_version is None or version["detail_revision"] != base_version.get("detail_revision")
            display = (base_version is None or version["display_revision"] is None
                       or version["display_revision"] != base_version.get("display_revision"))
            # Initial/rerun callers request a full private state; a hub retaining
            # the same revision receives state=None and ages its own copy.
            returned = age_state(deepcopy(state), result["server_now"], result["sampled_at"]) if changed else None
            return _finish(result, returned, changed=changed, details_changed=details, display_changed=display)

    def stats(self):
        with self._lock:
            return {**self._stats, "entries": len(self._entries),
                    "active_readers": sum(entry.users for entry in self._entries.values())}


_shared = SharedSnapshots()


def shared_snapshot(live, event_id, tokens=(), **options):
    return _shared.snapshot(live, event_id, tokens, **options)
