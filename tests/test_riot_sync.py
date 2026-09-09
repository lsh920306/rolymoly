"""Riot queue/rate behavior on disposable SQLite, with every HTTP call mocked."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.core import Core
from roly.competition import Competition
from roly.riot_api import RiotAPIError, RiotConfig
from roly.riot_sync import DatabaseRateLimiter, FORCE_SECONDS, LEASE_SECONDS, RiotSync, TTL_SECONDS, _lock_key
from roly.riot_sync import _static_data as static_data_function


class Clock:
    def __init__(self):
        self.value = 2_000_000_000.0

    def __call__(self):
        return self.value


class FakeClient:
    def __init__(self, config, *, before_request):
        self.before_request = before_request
        self.calls = []
        self.failures = {}
        self.hook = None

    def request(self, stage, identity, result):
        self.before_request("asia" if stage == "account" else "kr")
        self.calls.append((stage, identity))
        if self.hook:
            self.hook(stage, identity)
        failures = self.failures.get(stage, [])
        if failures:
            raise failures.pop(0)
        return result

    def account_by_riot_id(self, name, tag):
        return self.request("account", name + "#" + tag, {"puuid": "private-puuid-" + name})

    def summoner_by_puuid(self, puuid):
        return self.request("summoner", puuid, {"profile_icon_id": 1, "summoner_level": 100})

    def solo_rank_by_puuid(self, puuid):
        return self.request("rank", puuid, {"tier": "GOLD", "division": "II", "lp": 37})

    def top_masteries(self, puuid):
        return self.request("masteries", puuid, [{"champion_id": 1, "points": 12345, "level": 7}])


class RiotSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="roly-riot-sync-")
        self.addCleanup(temporary.cleanup)
        self.core = Core(Path(temporary.name) / "isolated.sqlite3")
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.admin = self.core.login("admin", "synthetic-admin-password")
        self.clock = Clock()
        self.config = RiotConfig("synthetic-key-one")
        self.sync = RiotSync(self.core, self.config, client_factory=FakeClient, clock=self.clock)
        self.mid = self.member("Member")
        static = patch("roly.riot_sync._static_data", return_value=("16.17.1", {
            "1": {"name": "애니", "image": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/champion/Annie.png"}}))
        static.start()
        self.addCleanup(static.stop)

    def member(self, name):
        member_id = self.core.join_member(name + "#QA", "TOP", "JG")
        self.core.approve_member(self.admin, member_id, 170, "private operator note")
        return member_id

    def row(self, table, member_id=None):
        self.assertIn(table, {"riot_jobs", "riot_profiles", "members"})
        with self.core.read_snapshot() as db:
            key = "id" if table == "members" else "member_id"
            row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (member_id or self.mid,)).fetchone()
            return dict(row) if row else None

    def finish(self, count=4):
        for _ in range(count):
            self.assertTrue(self.sync.process_one())

    def test_profile_finish_keeps_power_notes_and_old_competition_snapshots(self):
        ids = [self.mid] + [self.member(f"Player{index}") for index in range(9)]
        comp = Competition(self.core)
        event_id = comp.create_normal(self.admin, [{"member_id": mid, "role": ("TOP", "JG", "MID", "AD", "SUP")[i % 5]} for i, mid in enumerate(ids)], balanced=False)
        before = self.core.get_member(self.mid)
        event_before = comp.get_event(event_id)
        self.assertEqual(self.sync.enqueue(self.admin, [self.mid]), 1)
        self.finish()
        after = self.core.get_member(self.mid)
        self.assertEqual((after["current_tier"], after["current_tier_lp"], after["current_tier_source"]), ("골드 2", 37, "riot"))
        self.assertGreater(after["updated_at"], before["updated_at"])
        for key in ("base_score", "score", "clan_tier", "notes", "application_notes", "wins", "losses", "award_units"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(comp.get_event(event_id), event_before)
        public = self.sync.get_profiles([self.mid])[self.mid]
        self.assertNotIn("puuid", public)
        self.assertEqual(public["champions"][0]["name"], "애니")
        self.assertEqual(public["status"], "DONE")
        self.assertIn("private-puuid", self.row("riot_profiles")["payload"])
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM score_ledger").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM award_ledger").fetchone()[0], 0)

    def test_permissions_dedup_ttl_and_force_cooldown(self):
        receipt = self.core.register_member("applicant", "synthetic-member-password", "Applicant#QA", "TOP", "JG", request_key=str(uuid4()))
        pending = self.core.login("applicant", "synthetic-member-password")
        with self.assertRaises(PermissionError):
            self.sync.enqueue(pending, [self.mid])
        with self.assertRaises(ValueError):
            self.sync.enqueue(self.admin, [self.mid, receipt["member_id"]])
        self.assertIsNone(self.row("riot_jobs"))
        self.core.approve_member(self.admin, receipt["member_id"], 170)
        self.assertEqual(self.sync.enqueue(pending, [self.mid, self.mid]), 1)
        self.assertEqual(self.sync.enqueue(pending, [self.mid]), 0)
        self.finish()
        self.assertEqual(self.sync.enqueue(self.admin, [self.mid]), 0)
        self.assertEqual(self.sync.enqueue(self.admin, [self.mid], force=True), 0)
        self.clock.value += FORCE_SECONDS
        self.assertEqual(self.sync.enqueue(self.admin, [self.mid], force=True), 1)
        self.finish()
        self.clock.value += TTL_SECONDS
        self.assertEqual(self.sync.enqueue(self.admin, [self.mid]), 1)

    def test_one_rank_stage_stores_solo_and_flex_without_changing_member_power(self):
        normal = {"tier": "GOLD", "division": "II", "lp": 37, "wins": 10, "losses": 5}
        empty = {"tier": "UNRANKED", "division": "", "lp": 0, "wins": 0, "losses": 0}
        cases = [(normal, {"tier": "DIAMOND", "division": "IV", "lp": 64, "wins": 8, "losses": 2}, "골드 2", "다이아몬드 4", 64),
                 (empty, {"tier": "MASTER", "division": "I", "lp": 123, "wins": 9, "losses": 1}, "언랭크", "마스터", 123),
                 (empty, empty, "언랭크", "언랭크", None)]
        for index, (solo, flex, solo_tier, flex_tier, flex_lp) in enumerate(cases):
            with self.subTest(solo=solo_tier, flex=flex_tier):
                member_id = self.member(f"DualRank{index}")
                before = self.core.get_member(member_id)
                self.sync.enqueue(self.admin, [member_id])
                previous_calls = len(self.sync.client.calls)
                result = dict(solo, flex=dict(flex, queue="RANKED_FLEX_SR"))
                with patch.object(self.sync.client, "solo_rank_by_puuid",
                                  side_effect=lambda puuid: self.sync.client.request("rank", puuid, result)):
                    self.finish()
                self.assertEqual([stage for stage, _ in self.sync.client.calls[previous_calls:]],
                                 ["account", "summoner", "rank", "masteries"])
                stored = json.loads(self.row("riot_profiles", member_id)["payload"])
                public = self.sync.get_profiles([member_id])[member_id]
                for profile in (stored, public):
                    self.assertEqual((profile["current_tier"], profile["flex_current_tier"], profile["flex_lp"]),
                                     (solo_tier, flex_tier, flex_lp))
                    self.assertEqual((profile["rank_wins"], profile["rank_losses"]), (solo["wins"], solo["losses"]))
                    self.assertEqual((profile["flex_rank_wins"], profile["flex_rank_losses"]), (flex["wins"], flex["losses"]))
                after = self.core.get_member(member_id)
                self.assertEqual(after["current_tier"], solo_tier)
                for field in ("score", "base_score", "clan_tier", "wins", "losses", "award_units", "notes"):
                    self.assertEqual(after[field], before[field])
                self.assertNotIn("puuid", public)

    def test_legacy_partial_rank_without_flex_keeps_it_unqueried(self):
        for index, include_none in enumerate((False, True)):
            member_id = self.member(f"LegacySolo{index}")
            result = {"tier": "GOLD", "division": "II", "lp": 37}
            if include_none:
                result["flex"] = None
            self.sync.enqueue(self.admin, [member_id])
            with patch.object(self.sync.client, "solo_rank_by_puuid",
                              side_effect=lambda puuid: self.sync.client.request("rank", puuid, result)):
                self.finish(3)
            # Simulate restart after the legacy League response was persisted.
            replacement = RiotSync(self.core, self.config, client_factory=FakeClient, clock=self.clock)
            self.assertTrue(replacement.process_one())
            self.assertEqual([stage for stage, _ in replacement.client.calls], ["masteries"])
            stored = json.loads(self.row("riot_profiles", member_id)["payload"])
            public = replacement.get_profiles([member_id])[member_id]
            self.assertFalse(any(key.startswith("flex_") for key in stored))
            self.assertFalse(any(key.startswith("flex_") for key in public))

    def test_five_masteries_store_both_ranks_without_changing_roles_clan_or_power(self):
        before = self.core.get_member(self.mid)
        rank = {"tier": "GOLD", "division": "II", "lp": 37, "wins": 10, "losses": 5,
                "flex": {"tier": "PLATINUM", "division": "III", "lp": 64, "wins": 12, "losses": 7}}
        masteries = [{"champion_id": index, "points": 1000 - index, "level": index} for index in range(1, 6)]
        self.sync.enqueue(self.admin, [self.mid])
        with patch.object(self.sync.client, "solo_rank_by_puuid",
                          side_effect=lambda puuid: self.sync.client.request("rank", puuid, rank)), \
                patch.object(self.sync.client, "top_masteries",
                             side_effect=lambda puuid: self.sync.client.request("masteries", puuid, masteries)):
            self.finish()
        stored = json.loads(self.row("riot_profiles")["payload"])
        public = self.sync.get_profiles([self.mid])[self.mid]
        for profile in (stored, public):
            self.assertEqual([champion["id"] for champion in profile["champions"]], [1, 2, 3, 4, 5])
            self.assertEqual([champion["level"] for champion in profile["champions"]], [1, 2, 3, 4, 5])
            self.assertEqual((profile["rank_wins"], profile["rank_losses"], profile["flex_rank_wins"], profile["flex_rank_losses"]),
                             (10, 5, 12, 7))
            self.assertEqual((profile["current_tier"], profile["flex_current_tier"]), ("골드 2", "플래티넘 3"))
        after = self.core.get_member(self.mid)
        for field in ("main_role", "sub_role", "clan_tier", "score", "base_score", "wins", "losses", "award_units"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(len(self.sync.client.calls), 4)
        self.assertNotIn("puuid", public)

    def test_rate_windows_are_shared_across_threads_scopes_and_processes(self):
        limiters = [DatabaseRateLimiter(self.core, self.sync.key_hash, self.clock) for _ in range(24)]
        def reserve(index):
            try:
                limiters[index].reserve("kr" if index % 2 else "asia")
                return "accepted"
            except RiotAPIError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(24)))
        self.assertEqual(results.count("accepted"), 20)
        # AppTest replaces sys.modules['__main__'] with app.py. Windows spawn
        # would re-execute the app before reaching a ProcessPool task. A fresh
        # module entry point keeps four real processes independent of that UI.
        def reserve_process(_):
            return subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-m", "tests.riot_rate_worker",
                 "--database", self.core.db_path, "--key-hash", self.sync.key_hash,
                 "--stamp", str(self.clock.value)],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
            )
        with ThreadPoolExecutor(max_workers=4) as pool:
            children = list(pool.map(reserve_process, range(4)))
        processes = []
        for child in children:
            self.assertEqual(child.returncode, 0)
            self.assertEqual(child.stderr, "")
            response = json.loads(child.stdout)
            self.assertEqual(set(response), {"status", "pid"})
            self.assertNotIn(self.sync.key_hash, child.stdout)
            self.assertNotIn(self.core.db_path, child.stdout)
            self.assertNotIn(self.config.api_key, child.stdout)
            processes.append(response)
        self.assertEqual([response["status"] for response in processes], ["rate_limited"] * 4)
        self.assertEqual(len({response["pid"] for response in processes}), 4)
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM riot_rate_hits WHERE key_hash=?",
                                        (self.sync.key_hash,)).fetchone()[0], 20)
        for _ in range(4):
            self.clock.value += 1
            for _ in range(20):
                self.sync.limiter.reserve("kr")
        self.clock.value += 1
        with self.assertRaises(RiotAPIError) as limited:
            self.sync.limiter.reserve("asia")
        self.assertGreater(limited.exception.retry_after, 100)
        DatabaseRateLimiter(self.core, "different-key-hash", self.clock).reserve("kr")
        self.clock.value += 120
        self.sync.limiter.reserve("kr")

    def test_forty_members_finish_160_requests_across_budget_without_restart(self):
        ids = [self.mid] + [self.member(f"Bulk{index}") for index in range(39)]
        self.assertEqual(self.sync.enqueue(self.admin, ids), 40)
        for _ in range(400):
            if not self.sync.process_one():
                self.clock.value += 1
            if all(value["status"] == "DONE" for value in self.sync.get_profiles(ids).values()):
                break
        else:
            self.fail("Queue failed to make progress through its shared rate budget")
        self.assertEqual(len(self.sync.client.calls), 160)
        self.assertEqual(sum(stage == "account" for stage, _ in self.sync.client.calls), 40)
        self.assertEqual(len(self.sync.get_profiles(ids)), 40)

    def test_429_waits_globally_then_resumes_the_failed_stage(self):
        other = self.member("Other")
        self.sync.enqueue(self.admin, [self.mid])
        self.finish(2)
        self.sync.client.failures["rank"] = [RiotAPIError("rate_limited", retry_after=10, status=429)]
        self.assertTrue(self.sync.process_one())
        job = self.row("riot_jobs")
        self.assertEqual(job["stage"], 2)
        self.assertIn("puuid", json.loads(job["partial_payload"]))
        self.sync.enqueue(self.admin, [other])
        self.assertFalse(self.sync.process_one())
        self.clock.value += 9
        self.assertFalse(self.sync.process_one())
        self.clock.value += 1
        self.finish(6)
        self.assertEqual(self.row("riot_jobs")["status"], "DONE")
        self.assertEqual(sum(stage == "account" and identity == "Member#QA" for stage, identity in self.sync.client.calls), 1)
        self.assertEqual(sum(stage == "rank" for stage, _ in self.sync.client.calls), 3)

    def test_auth_failure_stops_same_key_and_key_rotation_resumes_pending_work(self):
        self.sync.client.failures["account"] = [RiotAPIError("auth", status=403)]
        self.sync.enqueue(self.admin, [self.mid])
        self.assertTrue(self.sync.process_one())
        self.clock.value += 1000
        for _ in range(3):
            self.assertFalse(self.sync.process_one())
        self.assertEqual(len(self.sync.client.calls), 1)
        replacement = RiotSync(self.core, RiotConfig("synthetic-key-two"), client_factory=FakeClient, clock=self.clock)
        for _ in range(4):
            self.assertTrue(replacement.process_one())
        self.assertEqual(self.row("riot_jobs")["status"], "DONE")

    def test_explicit_force_after_cooldown_unblocks_auth_without_new_identity(self):
        self.sync.client.failures["account"] = [RiotAPIError("auth", status=401)]
        self.sync.enqueue(self.admin, [self.mid])
        self.sync.process_one()
        self.clock.value += FORCE_SECONDS
        self.sync.enqueue(self.admin, [self.mid], force=True)
        self.finish()
        self.assertEqual(self.row("riot_jobs")["status"], "DONE")

    def test_http_holds_no_writer_lock_and_only_one_lease_is_active(self):
        entered, release = Event(), Event()
        def block(stage, identity):
            entered.set()
            if not release.wait(10):
                raise AssertionError("HTTP gate was not released")
        self.sync.client.hook = block
        self.sync.enqueue(self.admin, [self.mid])
        with ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(self.sync.process_one)
            self.assertTrue(entered.wait(5))
            try:
                writer = pool.submit(self.core.adjust_score, self.admin, self.mid, 7, "writer proceeds while HTTP waits")
                self.assertIsInstance(writer.result(timeout=5), int)
                self.assertIsNone(self.sync._claim())
            finally:
                release.set()
            self.assertTrue(request.result(timeout=5))
        self.assertEqual(len(self.sync.client.calls), 1)
        self.assertEqual(self.row("riot_jobs")["stage"], 1)

    def test_expired_lease_cannot_overwrite_its_replacement(self):
        self.sync.enqueue(self.admin, [self.mid])
        old = self.sync._claim()
        self.clock.value += LEASE_SECONDS
        new = self.sync._claim()
        self.assertNotEqual(old["lease_id"], new["lease_id"])
        self.sync._progress(old, {"puuid": "stale"})
        current = self.row("riot_jobs")
        self.assertEqual(current["lease_id"], new["lease_id"])
        self.assertEqual(current["stage"], 0)

    def test_identity_change_and_manual_tier_edit_block_old_final_response(self):
        for change_identity in (True, False):
            with self.subTest(change_identity=change_identity):
                self.sync.enqueue(self.admin, [self.mid], force=True)
                self.finish(3)
                old = self.core.get_member(self.mid)
                def mutate(stage, identity):
                    if stage == "masteries":
                        self.core.update_member(self.admin, self.mid, "Renamed#QA" if change_identity else old["riot_id"], "TOP", "JG", 170, "concurrent profile change", current_tier="실버 1", current_tier_lp=5)
                self.sync.client.hook = mutate
                self.assertTrue(self.sync.process_one())
                member = self.core.get_member(self.mid)
                self.assertEqual((member["current_tier"], member["current_tier_lp"]), ("실버 1", 5))
                self.assertIsNone(self.row("riot_profiles"))
                if change_identity:
                    self.assertIsNone(self.row("riot_jobs"))
                else:
                    self.assertEqual(self.row("riot_jobs")["status"], "FAILED")
                self.sync.client.hook = None
                self.clock.value += FORCE_SECONDS
                # Ensure the next edit is a real tier change too.
                self.core.update_member(self.admin, self.mid, member["riot_id"], "TOP", "JG", 170, "prepare next race", current_tier="", current_tier_lp=None)

    def test_disabled_key_never_starts_worker_or_contacts_api(self):
        import roly.riot_sync as runtime
        service = RiotSync(self.core, RiotConfig(), client_factory=FakeClient, clock=self.clock)
        self.assertIsNone(service.ensure_worker())
        self.assertEqual(service.enqueue(self.admin, [self.mid]), 0)
        self.assertFalse(service.process_one())
        self.assertEqual(service.client.calls, [])
        previous_stop = Event()
        with patch.dict(runtime._workers, {self.core.db_path: {"stop": previous_stop}}):
            self.assertIsNone(service.ensure_worker())
            self.assertTrue(previous_stop.is_set())

    def test_metadata_locks_are_distinct_from_domain_writer_and_each_other(self):
        from roly.postgres import PostgresConnection, advisory_key
        from roly.riot_sync import _metadata_transaction
        from tests.test_postgres_adapter import RecordingDriver
        core = type("Storage", (), {"schema": "rolymoly_qa_0123456789abcdef"})()
        values = {advisory_key(core.schema), _lock_key(core, "jobs"), _lock_key(core, "rate:" + self.sync.key_hash)}
        self.assertEqual(len(values), 3)
        for purpose in ("jobs", "rate:" + self.sync.key_hash):
            raw = RecordingDriver()
            storage = SimpleNamespace(is_postgres=True, schema=core.schema,
                connect=lambda: PostgresConnection(core.schema, raw))
            with _metadata_transaction(storage, purpose) as db:
                self.assertTrue(db.in_transaction)
                db.execute("SELECT 7")
            locks = [args[0] for query, args in raw.calls if "pg_advisory_xact_lock" in query]
            self.assertEqual(locks, [_lock_key(storage, purpose)])
            self.assertNotIn(advisory_key(core.schema), locks)
            self.assertEqual(raw.calls[-1][0], "COMMIT")
            self.assertTrue(raw.closed)

    def test_data_dragon_outage_keeps_last_successful_names_and_icons(self):
        import roly.riot_sync as runtime
        cached = {"until": 0.0, "version": "16.17.1", "champions": {"1": {"name": "애니", "image": "saved-image"}}}
        with patch.dict(runtime._static_cache, cached, clear=True), patch.object(runtime, "DataDragonClient", side_effect=RiotAPIError("unavailable")) as client:
            version, champions = static_data_function()
            self.assertEqual(version, "16.17.1")
            self.assertEqual(champions, cached["champions"])
            self.assertGreater(runtime._static_cache["until"], 0)
            self.assertEqual(static_data_function(), (version, champions))
            client.assert_called_once_with()

    def test_server_retry_after_is_not_shortened_and_last_good_cache_survives_404(self):
        self.sync.enqueue(self.admin, [self.mid])
        self.finish()
        original = self.row("riot_profiles")
        self.clock.value += FORCE_SECONDS
        self.sync.enqueue(self.admin, [self.mid], force=True)
        self.sync.client.failures["account"] = [RiotAPIError("rate_limited", retry_after=172800)]
        self.sync.process_one()
        with self.core.read_snapshot() as db:
            until = db.execute("SELECT until_at FROM riot_rate_cooldowns WHERE key_hash=?", (self.sync.key_hash,)).fetchone()[0]
        self.assertEqual(until, self.clock.value + 172800)
        self.clock.value += 86401
        self.assertFalse(self.sync.process_one())
        self.clock.value = until
        self.sync.client.failures["account"] = [RiotAPIError("not_found", status=404)]
        self.sync.process_one()
        self.assertEqual(self.row("riot_profiles"), original)
        public = self.sync.get_profiles([self.mid])[self.mid]
        self.assertEqual(public["current_tier"], "골드 2")
        self.assertEqual(public["last_error"], "not_found")
        self.assertTrue(public["error"])

    def test_uncached_manual_tier_is_not_reported_as_riot_and_projection_is_bounded(self):
        self.core.update_member(self.admin, self.mid, "Member#QA", "TOP", "JG", 170, "manual input", current_tier="실버 1", current_tier_lp=12)
        public = self.sync.get_profiles([self.mid])[self.mid]
        self.assertEqual((public["current_tier"], public["lp"]), ("", None))
        self.sync.enqueue(self.admin, [self.mid])
        with patch("roly.riot_sync._static_data", return_value=("", {})):
            self.finish()
        payload = json.loads(self.row("riot_profiles")["payload"])
        payload.update(key_hash="must-stay-private", partial_payload="must-stay-private")
        with self.core.transaction() as db:
            db.execute("UPDATE riot_profiles SET payload=? WHERE member_id=?", (json.dumps(payload), self.mid))
        public = self.sync.get_profiles([self.mid])[self.mid]
        self.assertEqual(public["current_tier"], "골드 2")
        self.assertEqual(public["profile_icon_url"], "")
        self.assertEqual(public["champions"][0]["id"], 1)
        self.assertFalse({"puuid", "key_hash", "partial_payload"} & set(public))

    def test_enqueue_and_public_read_use_one_bulk_member_query(self):
        ids = [self.mid] + [self.member(f"Batch{index}") for index in range(9)]
        queries = []
        original = self.core.connect
        def traced():
            db = original()
            db.set_trace_callback(queries.append)
            return db
        with patch.object(self.core, "connect", side_effect=traced):
            self.assertEqual(self.sync.enqueue(self.admin, ids), 10)
            self.assertEqual(len([query for query in queries if query.lstrip().startswith("SELECT m.id,m.riot_id")]), 1)
            queries.clear()
            self.assertEqual(len(self.sync.get_profiles(ids)), 10)
            self.assertEqual(len([query for query in queries if query.lstrip().startswith("SELECT")]), 1)

    def test_idle_shutdown_race_preserves_enqueue_and_worker_restart(self):
        import roly.riot_sync as runtime
        checked_empty, let_return, processed = Event(), Event(), Event()
        pending = [False]
        calls = [0]
        def process():
            calls[0] += 1
            if pending[0]:
                processed.set()
                with runtime._worker_lock:
                    runtime._workers[self.core.db_path]["stop"].set()
                return True
            return False
        def has_pending():
            if calls[0] == 1:
                checked_empty.set()
                if not let_return.wait(5):
                    raise AssertionError("idle decision was not released")
                return False
            return pending[0]
        monotonic_calls = [0]
        def monotonic():
            monotonic_calls[0] += 1
            return 0 if monotonic_calls[0] == 1 else 40
        with patch.object(self.sync, "process_one", side_effect=process), patch.object(self.sync, "_pending_work", side_effect=has_pending), patch.object(runtime.time, "monotonic", side_effect=monotonic):
            worker = self.sync.ensure_worker()
            try:
                self.assertTrue(checked_empty.wait(5))
                pending[0] = True
                self.assertIs(self.sync.ensure_worker(), worker)
                let_return.set()
                self.assertTrue(processed.wait(5))
            finally:
                let_return.set()
                with runtime._worker_lock:
                    state = runtime._workers.get(self.core.db_path)
                    if state:
                        state["stop"].set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
