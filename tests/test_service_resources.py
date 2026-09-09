"""Service upgrades replace imported classes without overlapping workers."""
from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from roly import service_resources as resources
from roly import ui, auction_ui, riot_ui, riot_sync


class FakeLive:
    _workers = {}
    _worker_lock = threading.Lock()
    created = []
    events = []
    blocked = None

    def __init__(self, core, competition):
        self.core, self.competition = core, competition
        self.created.append(self)
        self.events.append("construct")

    def has_active_sessions(self):
        return True

    def ensure_worker(self):
        with self._worker_lock:
            if self.core.db_path in self._workers:
                return self._workers[self.core.db_path]["thread"]
            stop = threading.Event()
            blocked = self.blocked

            def run():
                stop.wait()
                if blocked is not None:
                    blocked.wait()
                self.events.append("stopped")

            thread = threading.Thread(target=run, daemon=True)
            self._workers[self.core.db_path] = {"stop": stop, "thread": thread}
            thread.start()
            return thread

    def stop_worker(self):
        with self._worker_lock:
            worker = self._workers.get(self.core.db_path)
            if worker:
                worker["stop"].set()
        if worker:
            worker["thread"].join(timeout=0.05)
            if not worker["thread"].is_alive():
                self._workers.pop(self.core.db_path, None)


class ServiceResourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="roly-resource-")
        self.path = str(Path(self.directory.name) / "isolated.sqlite3")
        self.revision = "backend-one"
        FakeLive._workers = {}
        FakeLive.created = []
        FakeLive.events = []
        FakeLive.blocked = None
        self.patches = [
            patch("roly.storage_config.postgres_kwargs", side_effect=AssertionError("local Secrets read")),
            patch("psycopg.connect", side_effect=AssertionError("shared PostgreSQL access")),
            patch("roly.riot_api._http_get", side_effect=AssertionError("Riot HTTP")),
            patch.object(resources, "backend_revision", side_effect=lambda: self.revision),
            patch("roly.live_auction.LiveAuction", FakeLive),
        ]
        for item in self.patches:
            item.start()
        resources.clear_services()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.finish)

    def finish(self):
        if FakeLive.blocked:
            FakeLive.blocked.set()
        resources.clear_services()
        ui.lounge_service.clear()
        for item in reversed(self.patches):
            item.stop()

    def test_services_live_and_lounge_share_one_real_local_core_without_secrets(self):
        core, competition = ui.services(self.path)
        live = auction_ui.live_service(self.path)
        self.assertIs(live.core, core)
        self.assertIs(live.competition, competition)
        self.assertIs(ui.services(self.path)[0], core)
        self.assertIs(ui.lounge_service(self.path), ui.lounge_service(self.path))
        self.assertEqual(len(FakeLive.created), 1)
        worker = FakeLive._workers[self.path]["thread"]
        auction_ui.live_service.clear(self.path)
        self.assertFalse(worker.is_alive())
        self.assertIsNot(ui.services(self.path)[0], core)

    def test_version_change_stops_old_class_owner_before_publishing_new_service(self):
        core = ui.services(self.path)[0]
        lounge = ui.lounge_service(self.path)
        old = auction_ui.live_service(self.path)
        thread = old._workers[self.path]["thread"]
        # A reload can produce a different class-level worker registry.
        replacement = type("NewLive", (FakeLive,), {"_workers": {}, "_worker_lock": threading.Lock()})
        self.revision = "backend-two"
        with patch("roly.live_auction.LiveAuction", replacement):
            current = auction_ui.live_service(self.path)
            self.assertIsNot(current.core, core)
            self.assertIsNot(ui.lounge_service(self.path), lounge)
            self.assertFalse(thread.is_alive())
            self.assertEqual(FakeLive.events[:3], ["construct", "stopped", "construct"])
            self.assertIs(ui.services(self.path)[0], current.core)
            resources.clear_services()

    def test_worker_timeout_blocks_new_constructor_and_retry_waits_for_old_exit(self):
        FakeLive.blocked = threading.Event()
        old = auction_ui.live_service(self.path)
        thread = old._workers[self.path]["thread"]
        self.revision = "backend-two"
        with self.assertRaisesRegex(sqlite3.OperationalError, "서비스 업데이트"):
            ui.services(self.path)
        self.assertTrue(thread.is_alive())
        self.assertEqual(len(FakeLive.created), 1)
        FakeLive.blocked.set()
        thread.join(timeout=1)
        FakeLive.blocked = None
        self.assertIsNot(auction_ui.live_service(self.path), old)
        self.assertEqual(len(FakeLive.created), 2)

    def test_parallel_sessions_construct_only_one_bundle(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            owners = list(pool.map(lambda _: auction_ui.live_service(self.path), range(24)))
        self.assertEqual(len({id(owner) for owner in owners}), 1)
        self.assertEqual(len(FakeLive.created), 1)

    def test_first_auction_activates_bundle_created_before_any_active_event(self):
        with patch.object(FakeLive, "has_active_sessions", return_value=False):
            core = ui.services(self.path)[0]
            self.assertNotIn(self.path, FakeLive._workers)
            live = auction_ui.live_service(self.path)
            self.assertIs(live.core, core)
            self.assertTrue(FakeLive._workers[self.path]["thread"].is_alive())
            self.assertEqual(len(FakeLive.created), 1)

    def test_storage_rotation_replaces_bundle_without_exposing_binding(self):
        path = "supabase://rolymoly"
        binding = {"host": "synthetic-local-only", "password": "synthetic-first"}
        self.patches[0].stop()
        with patch("roly.storage_config.postgres_kwargs", side_effect=lambda: dict(binding)), \
             patch("roly.core.Core", side_effect=lambda path: SimpleNamespace(db_path=path)), \
             patch("roly.competition.Competition", side_effect=lambda core: SimpleNamespace(core=core)):
            old = auction_ui.live_service(path)
            key = resources.service_bundle(path)["key"]
            self.assertNotIn("synthetic", str(key))
            thread = old._workers[path]["thread"]
            binding["password"] = "synthetic-second"
            current = auction_ui.live_service(path)
            self.assertIsNot(current, old)
            self.assertFalse(thread.is_alive())
            resources.clear_services(path)
        self.patches[0].start()

    def test_revision_change_retires_workers_for_other_local_databases(self):
        first = auction_ui.live_service(self.path)
        second_path = str(Path(self.directory.name) / "second.sqlite3")
        second = auction_ui.live_service(second_path)
        threads = [first._workers[self.path]["thread"], second._workers[second_path]["thread"]]
        self.revision = "backend-two"
        auction_ui.live_service(self.path)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_riot_cache_revision_stops_previous_worker_and_keeps_core_identity(self):
        core = ui.services(self.path)[0]
        workers, lock, created = {}, threading.Lock(), []

        class Sync:
            def __init__(self, core, config, rate_core):
                self.core, self.rate_core = core, rate_core
                created.append(self)

            def ensure_worker(self):
                stop = threading.Event()
                thread = threading.Thread(target=stop.wait, daemon=True)
                workers[self.core.db_path] = {"stop": stop, "thread": thread}
                thread.start()

        with patch.object(riot_sync, "_workers", workers), patch.object(riot_sync, "_worker_lock", lock), \
             patch.object(riot_sync, "RiotSync", Sync):
            one = riot_ui._service(self.path, "fake-key-hash", self.path, core, object())
            self.assertIs(riot_ui._service(self.path, "fake-key-hash", self.path, core, object()), one)
            thread = workers[self.path]["thread"]
            self.revision = "backend-two"
            two = riot_ui._service(self.path, "fake-key-hash", self.path, core, object())
            self.assertFalse(thread.is_alive())
            self.assertIsNot(one, two)
            self.assertIs(two.core, core)
            self.assertIs(two.rate_core, core)
            self.assertEqual(len(created), 2)
            riot_ui._service.clear()


class RevisionFingerprintTests(unittest.TestCase):
    def test_only_source_change_rehashes_files_and_registry_survives_reload(self):
        resources.clear_services()
        with tempfile.TemporaryDirectory(prefix="roly-revision-") as folder:
            root = Path(folder)
            path = root / "core.py"
            path.write_text("version = 1\n", encoding="utf-8")
            with patch.object(resources, "ROOT", root), patch.object(resources, "BACKEND_FILES", ("core.py",)):
                one = resources.backend_revision()
                with patch.object(Path, "read_bytes", side_effect=AssertionError("unnecessary source reread")):
                    self.assertEqual(resources.backend_revision(), one)
                path.write_text("version = 22\n", encoding="utf-8")
                self.assertNotEqual(resources.backend_revision(), one)
        previous = resources._registry()
        importlib.reload(resources)
        self.assertIs(resources._registry(), previous)


if __name__ == "__main__":
    unittest.main()
