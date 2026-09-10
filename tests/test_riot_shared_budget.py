"""Independent demo databases share a Riot key budget without sharing profiles."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from roly.core import Core
from roly.riot_api import RiotAPIError, RiotConfig
from roly.riot_sync import FORCE_SECONDS, RiotSync, stop_workers_except_key
from tests.test_riot_sync import Clock, FakeClient


class RiotSharedBudgetTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="roly-shared-riot-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.first = Core(root / "first-demo.sqlite3")
        self.first.setup_admin("admin", "synthetic-shared-budget-password")
        self.token = self.first.login("admin", "synthetic-shared-budget-password")
        self.mid = self.first.join_member("FirstDemo#QA", "TOP", "JG")
        self.first.approve_member(self.token, self.mid, 170, "private first memo")
        # Start both member databases with the same local row IDs, as real
        # session-specific demos do. Only their rate records will be shared.
        with self.first.read_snapshot() as source:
            import sqlite3
            target = sqlite3.connect(root / "second-demo.sqlite3")
            try:
                source.backup(target)
            finally:
                target.close()
        self.second = Core(root / "second-demo.sqlite3")
        self.second.update_member(self.token, self.mid, "SecondDemo#QA", "MID", "SUP", 170,
                                  "isolated demo fixture", current_tier="실버 4", current_tier_lp=12)
        self.rate = Core(root / "shared-budget.sqlite3")
        self.clock = Clock()
        self.config = RiotConfig("synthetic-shared-riot-key")
        self.a = self.service(self.first)
        self.b = self.service(self.second)
        static = patch("roly.riot_sync._static_data", return_value=("", {}))
        static.start()
        self.addCleanup(static.stop)
        guard = patch("roly.riot_sync.load_riot_config", side_effect=AssertionError("No live settings in shared-budget tests"))
        guard.start()
        self.addCleanup(guard.stop)

    def service(self, core, *, config=None, rate_core=None):
        return RiotSync(core, self.config if config is None else config,
                        client_factory=FakeClient, clock=self.clock,
                        rate_core=self.rate if rate_core is None else rate_core)

    def count(self, core, table):
        self.assertIn(table, {"riot_rate_hits", "riot_rate_cooldowns", "riot_jobs", "riot_profiles", "members", "accounts", "score_ledger"})
        with core.read_snapshot() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def job(self, core):
        with core.read_snapshot() as db:
            return dict(db.execute("SELECT * FROM riot_jobs WHERE member_id=?", (self.mid,)).fetchone())

    def test_two_demos_and_operating_connection_share_both_rate_windows(self):
        operating = self.service(self.rate)
        services = (self.a, self.b, operating)
        def reserve(index):
            try:
                services[index % 3].limiter.reserve("asia" if index % 2 else "kr")
                return "accepted"
            except RiotAPIError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(reserve, range(24)))
        self.assertEqual(replies.count("accepted"), 20)
        self.assertEqual(replies.count("rate_limited"), 4)
        for _ in range(4):
            self.clock.value += 1
            for index in range(20):
                self.assertEqual(reserve(index), "accepted")
        self.clock.value += 1
        self.assertEqual([reserve(index) for index in range(3)], ["rate_limited"] * 3)
        self.assertEqual(self.count(self.rate, "riot_rate_hits"), 100)
        self.assertEqual(self.count(self.first, "riot_rate_hits"), 0)
        self.assertEqual(self.count(self.second, "riot_rate_hits"), 0)
        self.clock.value += 115
        self.assertEqual(reserve(0), "accepted")
        self.assertEqual(self.count(self.rate, "riot_rate_hits"), 81)

    def test_429_from_one_demo_pauses_all_demos_and_preserves_job_stage(self):
        self.a.enqueue(self.token, [self.mid])
        self.b.enqueue(self.token, [self.mid])
        self.a.process_one()
        self.a.process_one()
        self.a.client.failures["rank"] = [RiotAPIError("rate_limited", retry_after=12, status=429)]
        self.a.process_one()
        self.assertEqual(self.job(self.first)["stage"], 2)
        self.assertFalse(self.b.process_one())
        self.assertEqual(self.b.client.calls, [])
        self.assertTrue(self.b._pending_work())
        self.assertEqual(self.b.limiter.status(), {"blocked": False, "retry_after": 12, "last_error": "rate_limited"})
        self.clock.value += 5
        self.assertEqual(self.b.limiter.status()["retry_after"], 7)
        self.b.limiter.reset_auth()
        self.assertEqual(self.b.limiter.status()["retry_after"], 7)
        self.clock.value += 7
        for _ in range(2):
            self.assertTrue(self.a.process_one())
        for _ in range(4):
            self.assertTrue(self.b.process_one())
        self.assertEqual(sum(stage == "account" for stage, _ in self.a.client.calls), 1)
        self.assertEqual(self.job(self.first)["status"], "DONE")
        self.assertEqual(self.job(self.second)["status"], "DONE")

    def test_auth_block_and_explicit_refresh_reset_are_shared_and_authorized(self):
        self.a.enqueue(self.token, [self.mid])
        self.b.enqueue(self.token, [self.mid])
        self.a.client.failures["account"] = [RiotAPIError("auth", status=403)]
        self.assertTrue(self.a.process_one())
        self.assertTrue(self.b.limiter.status()["blocked"])
        self.assertFalse(self.b._pending_work())
        self.assertFalse(self.b.process_one())
        self.clock.value += FORCE_SECONDS
        with self.assertRaises(PermissionError):
            self.b.enqueue("invalid-session", [self.mid], force=True)
        self.assertTrue(self.a.limiter.status()["blocked"])
        self.assertEqual(self.b.enqueue(self.token, [self.mid], force=True), 0)
        self.assertFalse(self.a.limiter.status()["blocked"])
        for _ in range(4):
            self.assertTrue(self.b.process_one())
        self.assertEqual(self.job(self.second)["status"], "DONE")
        self.assertEqual(self.count(self.first, "riot_rate_cooldowns"), 0)
        self.assertEqual(self.count(self.second, "riot_rate_cooldowns"), 0)

    def _assert_auth_reset_keeps_overlapping_server_cooldown(self, errors):
        other = self.first.join_member("OtherBudget#QA", "MID", "SUP")
        self.first.approve_member(self.token, other, 190)
        self.a.enqueue(self.token, [self.mid, other])
        members_before = self.first.list_members()
        deadline = self.clock.value + 7200
        # Separate workers can finish previously reserved requests in either
        # order. Their authentication block and 429 deadline share one key.
        for code in errors:
            service = self.a if code == "rate_limited" else self.b
            service.limiter.backoff(RiotAPIError(code, status=429 if code == "rate_limited" else 403,
                                                retry_after=7200 if code == "rate_limited" else None))
        self.assertTrue(self.a.limiter.status()["blocked"])
        self.clock.value += FORCE_SECONDS
        self.assertEqual(self.a.enqueue(self.token, [self.mid], force=True), 0)
        self.assertEqual(self.a.limiter.status(), {
            "blocked": False, "retry_after": deadline - self.clock.value,
            "last_error": "rate_limited"})
        self.assertEqual(self.b.limiter.status(), self.a.limiter.status())
        self.assertFalse(self.a.process_one())
        self.assertEqual(self.a.client.calls, [])
        self.assertEqual(self.first.list_members(), members_before)
        self.clock.value = deadline - 0.001
        self.assertFalse(self.a.process_one())
        self.clock.value = deadline
        self.assertTrue(self.a.process_one())
        # The other member was already waiting. A force request for the first
        # member must not allow that independent job to bypass the shared 429.
        self.assertEqual(self.a.client.calls, [("account", "OtherBudget#QA")])
        with self.first.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT stage FROM riot_jobs WHERE member_id=?", (other,)).fetchone()[0], 1)

    def test_force_refresh_keeps_long_429_when_auth_error_arrives_after_rate_error(self):
        self._assert_auth_reset_keeps_overlapping_server_cooldown(("rate_limited", "auth"))

    def test_force_refresh_keeps_long_429_when_rate_error_arrives_after_auth_error(self):
        self._assert_auth_reset_keeps_overlapping_server_cooldown(("auth", "rate_limited"))

    def test_new_key_resumes_partial_job_without_old_key_auth_block(self):
        self.a.enqueue(self.token, [self.mid])
        self.a.process_one()
        self.a.client.failures["summoner"] = [RiotAPIError("auth", status=401)]
        self.a.process_one()
        self.assertEqual(self.job(self.first)["stage"], 1)
        replacement = self.service(self.first, config=RiotConfig("synthetic-replacement-key"))
        for _ in range(3):
            self.assertTrue(replacement.process_one())
        self.assertEqual([stage for stage, _ in replacement.client.calls], ["summoner", "rank", "masteries"])
        self.assertTrue(self.a.limiter.status()["blocked"])
        self.assertFalse(replacement.limiter.status()["blocked"])

    def test_shared_budget_never_merges_members_profiles_sessions_or_scores(self):
        before_second = self.second.get_member(self.mid)
        self.a.enqueue(self.token, [self.mid])
        for _ in range(4):
            self.assertTrue(self.a.process_one())
        self.assertEqual(self.first.get_member(self.mid)["current_tier"], "골드 2")
        self.assertEqual(self.second.get_member(self.mid), before_second)
        self.assertEqual(self.b.get_profiles([self.mid])[self.mid]["current_tier"], "")
        self.assertEqual(self.count(self.second, "riot_profiles"), 0)
        self.assertEqual(self.count(self.second, "riot_jobs"), 0)
        for table in ("members", "accounts", "riot_jobs", "riot_profiles", "score_ledger"):
            self.assertEqual(self.count(self.rate, table), 0)
        self.assertEqual(self.first.get_member(self.mid)["score"], 170)
        self.assertEqual(self.first.session(self.token)["role"], "admin")
        self.assertEqual(self.second.session(self.token)["role"], "admin")

    def test_remote_status_and_reset_waits_do_not_hold_member_writer(self):
        self.a.enqueue(self.token, [self.mid])
        for method in ("status", "reset_auth"):
            with self.subTest(method=method):
                self.clock.value += FORCE_SECONDS
                entered, release = Event(), Event()
                original = getattr(self.a.limiter, method)
                def delayed():
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("shared budget wait was not released")
                    return original()
                action = self.a.process_one if method == "status" else lambda: self.a.enqueue(self.token, [self.mid], force=True)
                with patch.object(self.a.limiter, method, side_effect=delayed), ThreadPoolExecutor(max_workers=2) as pool:
                    waiting = pool.submit(action)
                    try:
                        self.assertTrue(entered.wait(3))
                        writer = pool.submit(self.first.adjust_score, self.token, self.mid, 1, "writer stays available")
                        writer.result(timeout=3)
                    finally:
                        release.set()
                    waiting.result(timeout=3)

    def test_blocked_http_keeps_member_and_shared_budget_writers_available(self):
        self.a.enqueue(self.token, [self.mid])
        entered, release = Event(), Event()
        def blocked_http(stage, identity):
            entered.set()
            if not release.wait(5):
                raise AssertionError("HTTP gate was not released")
        self.a.client.hook = blocked_http
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(self.a.process_one)
            try:
                self.assertTrue(entered.wait(3))
                pool.submit(self.first.adjust_score, self.token, self.mid, 1, "concurrent score").result(timeout=3)
                pool.submit(self.b.limiter.reserve, "kr").result(timeout=3)
            finally:
                release.set()
            self.assertTrue(pending.result(timeout=3))

    def test_worker_preserves_rate_store_and_replaces_same_key_changed_store(self):
        import roly.riot_sync as runtime
        first_entered, second_entered, release = Event(), Event(), Event()
        alternate = self.service(self.first, rate_core=self.second)
        def process(entered):
            entered.set()
            release.wait(5)
            return False
        # This assertion concerns this service's workers, not workers cached by
        # earlier AppTests. Join both owned threads before restoring the registry.
        with patch.object(runtime, "_workers", {}), \
                patch.object(self.a, "process_one", side_effect=lambda: process(first_entered)), \
                patch.object(alternate, "process_one", side_effect=lambda: process(second_entered)), \
                patch.object(self.a, "_pending_work", return_value=False), \
                patch.object(alternate, "_pending_work", return_value=False):
            first = self.a.ensure_worker()
            second = None
            try:
                self.assertTrue(first_entered.wait(3))
                self.assertIs(self.a.ensure_worker(), first)
                with runtime._worker_lock:
                    old_state = runtime._workers[self.first.db_path]
                    self.assertEqual(old_state["rate_path"], self.rate.db_path)
                second = alternate.ensure_worker()
                self.assertIsNot(second, first)
                self.assertTrue(second_entered.wait(3))
                self.assertTrue(old_state["stop"].is_set())
                with runtime._worker_lock:
                    self.assertEqual(runtime._workers[self.first.db_path]["rate_path"], self.second.db_path)
                self.assertEqual(stop_workers_except_key(self.config), 0)
                self.assertEqual(stop_workers_except_key(RiotConfig("different-synthetic-key")), 1)
            finally:
                stop_workers_except_key(RiotConfig())
                release.set()
                first.join(timeout=3)
                if second:
                    second.join(timeout=3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            with runtime._worker_lock:
                self.assertEqual(runtime._workers, {})

    def test_key_rotation_retires_every_previous_demo_worker_and_blank_stops_all(self):
        import roly.riot_sync as runtime
        replacement = self.service(self.rate, config=RiotConfig("new-synthetic-key"))
        states = {"first": {"key_hash": self.a.key_hash, "stop": Event()},
                  "second": {"key_hash": self.b.key_hash, "stop": Event()},
                  "new": {"key_hash": replacement.key_hash, "stop": Event()}}
        with patch.dict(runtime._workers, states, clear=True):
            self.assertEqual(stop_workers_except_key(replacement.config), 2)
            self.assertTrue(states["first"]["stop"].is_set())
            self.assertTrue(states["second"]["stop"].is_set())
            self.assertFalse(states["new"]["stop"].is_set())
            self.assertEqual(stop_workers_except_key(RiotConfig()), 1)
            self.assertEqual(stop_workers_except_key(RiotConfig()), 0)

    def test_closing_demo_gate_stops_demo_workers_and_preserves_operation(self):
        import roly.riot_sync as runtime
        states = {"demo-a.sqlite3": {"stop": Event()}, "demo-b.sqlite3": {"stop": Event()},
                  "supabase://rolymoly": {"stop": Event()}}
        with patch.dict(runtime._workers, states, clear=True):
            self.assertEqual(runtime.stop_demo_workers(), 2)
            self.assertTrue(states["demo-a.sqlite3"]["stop"].is_set())
            self.assertTrue(states["demo-b.sqlite3"]["stop"].is_set())
            self.assertFalse(states["supabase://rolymoly"]["stop"].is_set())
            self.assertEqual(runtime.stop_demo_workers(), 0)
