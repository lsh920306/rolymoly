"""Normalized rank ownership, identity changes and atomic storage on local DBs."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.core import Core
from roly.member_ranks import (RANK_FIELDS, RANK_JOINS, RANK_SELECT, initialize_ranks,
                               save_riot_profile)
from roly.riot_api import RiotAPIError, RiotConfig
from roly.riot_profile import member_profiles
from roly.riot_sync import RiotSync
from tests.test_riot_sync import Clock, FakeClient


PROFILE = {"current_tier": "골드 2", "lp": 37, "rank_wins": 8, "rank_losses": 2,
           "flex_current_tier": "마스터", "flex_lp": 123, "flex_rank_wins": 15,
           "flex_rank_losses": 5, "champions": [], "profile_icon_url": "",
           "puuid": "internal-test-identity", "updated_at": "2026-09-09T00:00:00+00:00"}


class MemberRanksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dir = TemporaryDirectory(prefix="roly-ranks-base-")
        cls.base = Core(Path(cls.base_dir.name) / "base.sqlite3")
        cls.base.setup_admin("admin", "synthetic-admin-password")
        cls.admin = cls.base.login("admin", "synthetic-admin-password")
        receipt = cls.base.register_member("member", "synthetic-member-password", "Member#QA", "TOP", "JG", request_key=str(uuid4()))
        cls.mid = receipt["member_id"]
        cls.base.approve_member(cls.admin, cls.mid, 170)
        cls.member_token = cls.base.login("member", "synthetic-member-password")

    @classmethod
    def tearDownClass(cls):
        cls.base_dir.cleanup()

    def setUp(self):
        folder = TemporaryDirectory(prefix="roly-ranks-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "isolated.sqlite3"
        with closing(self.base.connect()) as source, closing(sqlite3.connect(self.path)) as destination:
            source.backup(destination)
        self.core = Core(self.path)
        self.clock = Clock()
        self.sync = RiotSync(self.core, RiotConfig("synthetic-key"), client_factory=FakeClient, clock=self.clock)
        for replacement in (patch("roly.riot_sync._static_data", return_value=("", {})),
                            patch("roly.riot_api._http_get", side_effect=AssertionError("No HTTP")),
                            patch("roly.postgres._connection_pool", side_effect=AssertionError("No remote DB"))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def rows(self):
        with self.core.read_snapshot() as db:
            return [dict(row) for row in db.execute("SELECT * FROM member_ranks ORDER BY member_id,queue_type")]

    def cache(self, payload=None):
        with self.core.transaction() as db:
            save_riot_profile(db, self.mid, "member#qa", PROFILE if payload is None else payload, self.clock.value)

    def test_normalized_rows_are_the_only_rank_source_for_member_and_public_views(self):
        before = self.core.get_member(self.mid)
        self.cache()
        rows = self.rows()
        self.assertEqual([(row["queue_type"], row["tier"], row["division"], row["league_points"]) for row in rows],
                         [("FLEX", "MASTER", None, 123), ("SOLO", "GOLD", 2, 37)])
        with self.core.transaction() as db:
            raw = db.execute("SELECT current_tier,current_tier_lp FROM members WHERE id=?", (self.mid,)).fetchone()
            self.assertEqual(tuple(raw), ("", None))
            metadata = json.loads(db.execute("SELECT payload FROM riot_profiles WHERE member_id=?", (self.mid,)).fetchone()[0])
            self.assertFalse(RANK_FIELDS.intersection(metadata))
            # Stale deprecated columns/JSON must never override the new owner.
            db.execute("UPDATE members SET current_tier='실버 1',current_tier_lp=1 WHERE id=?", (self.mid,))
            metadata.update(current_tier="아이언 4", lp=0, flex_current_tier="언랭크")
            db.execute("UPDATE riot_profiles SET payload=? WHERE member_id=?", (json.dumps(metadata), self.mid))
        member = self.core.get_member(self.mid)
        public = member_profiles(self.core, [self.mid])[self.mid]
        self.assertEqual((member["current_tier"], member["current_tier_lp"], public["current_tier"], public["flex_current_tier"]),
                         ("골드 2", 37, "골드 2", "마스터"))
        self.assertNotIn("puuid", public)
        self.assertFalse(any(key.startswith("_rank_") for key in member))
        for field in ("score", "wins", "losses", "clan_tier", "main_role", "sub_role", "award_units"):
            self.assertEqual(member[field], before[field])

    def test_two_queues_never_duplicate_members_and_batch_profile_read_is_one_statement(self):
        self.cache()
        second = self.core.join_member("Second#QA", "MID", "AD")
        self.core.approve_member(self.admin, second, 200)
        self.assertEqual([member["id"] for member in self.core.list_members()], [self.mid, second])
        statements = []
        connections = []
        connect = self.core.connect
        def traced():
            db = connect()
            connections.append(db)
            db.set_trace_callback(statements.append)
            return db
        from roly.riot_profile import public_profile
        def after_release(payload):
            for db in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")
            return public_profile(payload)
        with patch.object(self.core, "connect", side_effect=traced), patch("roly.riot_profile.public_profile", side_effect=after_release):
            profiles = member_profiles(self.core, [self.mid, second, self.mid])
        self.assertEqual(set(profiles), {self.mid})
        self.assertEqual(sum(statement.lstrip().upper().startswith("SELECT") for statement in statements), 1)

    def test_queue_write_failure_rolls_back_both_ranks_and_cached_metadata(self):
        self.cache()
        before = self.rows()
        with self.core.transaction() as db:
            db.execute("CREATE TRIGGER reject_flex BEFORE INSERT ON member_ranks WHEN NEW.queue_type='FLEX' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
            old_cache = tuple(db.execute("SELECT * FROM riot_profiles").fetchone())
        with self.assertRaises(sqlite3.IntegrityError):
            self.cache(dict(PROFILE, current_tier="다이아몬드 1", lp=99, profile_icon_url="changed"))
        self.assertEqual(self.rows(), before)
        with self.core.read_snapshot() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM riot_profiles").fetchone()), old_cache)

    def test_unqueried_flex_differs_from_successfully_unranked(self):
        self.assertEqual(self.rows(), [])
        solo = {key: value for key, value in PROFILE.items() if not key.startswith("flex_")}
        self.cache(solo)
        self.assertEqual([row["queue_type"] for row in self.rows()], ["SOLO"])
        self.assertNotIn("flex_current_tier", member_profiles(self.core, [self.mid])[self.mid])
        self.cache(dict(PROFILE, flex_current_tier="언랭크", flex_lp=None, flex_rank_wins=0, flex_rank_losses=0))
        public = member_profiles(self.core, [self.mid])[self.mid]
        self.assertEqual((public["flex_current_tier"], public["flex_lp"], public["flex_rank_wins"]), ("언랭크", None, 0))

    def test_rank_fetched_time_survives_delay_before_mastery_completion(self):
        self.sync.enqueue(self.admin, [self.mid])
        for _ in range(3):
            self.sync.process_one()
        observed = self.clock.value
        self.clock.value += 30
        self.sync.process_one()
        self.assertEqual(self.rows()[0]["fetched_at"], observed)
        self.assertNotEqual(self.rows()[0]["updated_at"], PROFILE["updated_at"])
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT fetched_at FROM riot_profiles").fetchone()[0], observed + 30)

    def test_404_preserves_normalized_ranks_and_their_success_timestamp(self):
        self.cache()
        before = self.rows()
        self.sync.enqueue(self.admin, [self.mid], force=True)
        self.sync.client.failures["account"] = [RiotAPIError("not_found", status=404)]
        self.sync.process_one()
        self.assertEqual(self.rows(), before)
        public = self.sync.get_profiles([self.mid])[self.mid]
        self.assertEqual((public["current_tier"], public["last_error"]), ("골드 2", "not_found"))

    def test_rename_back_blocks_in_flight_job_and_clears_both_queues(self):
        self.cache()
        self.sync.enqueue(self.admin, [self.mid], force=True)
        for _ in range(3):
            self.sync.process_one()
        def rename_during_response(stage, unused):
            if stage == "masteries":
                for name in ("Temporary#QA", "Member#QA"):
                    self.core.update_own_riot_id(self.member_token, name, expected_updated_at=self.core.get_member(self.mid)["updated_at"])
        self.sync.client.hook = rename_during_response
        self.sync.process_one()
        self.assertEqual(self.rows(), [])
        self.assertEqual(member_profiles(self.core, [self.mid]), {})
        self.assertEqual(self.core.get_member(self.mid)["current_tier"], "")
        self.assertEqual(self.core.session(self.member_token)["member_id"], self.mid)

    def test_existing_manual_rank_edit_invalidates_a_queued_riot_result(self):
        member = self.core.get_member(self.mid)
        self.core.update_member(self.admin, self.mid, member["riot_id"], "TOP", "JG", 170, "manual before", current_tier="실버 2", current_tier_lp=15)
        self.sync.enqueue(self.admin, [self.mid])
        for _ in range(3):
            self.sync.process_one()
        self.core.update_member(self.admin, self.mid, member["riot_id"], "TOP", "JG", 170, "manual after", current_tier="플래티넘 2", current_tier_lp=20)
        self.sync.process_one()
        self.assertEqual(self.core.get_member(self.mid)["current_tier"], "플래티넘 2")
        self.assertEqual(self.sync.get_profiles([self.mid])[self.mid]["last_error"], "profile_changed")

    def test_v6_upgrade_preserves_account_member_manual_and_dual_riot_records_once(self):
        manual = self.core.join_member("Manual#QA", "MID", "SUP")
        self.core.approve_member(self.admin, manual, 200)
        with self.core.transaction() as db:
            db.execute("DROP TABLE member_ranks")
            db.execute("UPDATE members SET current_tier='실버 1',current_tier_lp=18 WHERE id=?", (manual,))
            db.execute("UPDATE members SET current_tier='골드 2',current_tier_lp=37,current_tier_source='riot' WHERE id=?", (self.mid,))
            db.execute("INSERT INTO riot_profiles VALUES(?,?,?,?)", (self.mid, "member#qa", json.dumps(PROFILE), self.clock.value))
        upgraded = Core(self.path)
        self.assertEqual(upgraded.get_member(manual)["current_tier"], "실버 1")
        self.assertEqual(upgraded.get_member(self.mid)["current_tier"], "골드 2")
        self.assertEqual(member_profiles(upgraded, [self.mid])[self.mid]["flex_current_tier"], "마스터")
        self.assertEqual(upgraded.session(self.member_token)["member_id"], self.mid)
        once = self.rows()
        Core(self.path)
        self.assertEqual(self.rows(), once)

    def test_v6_missing_or_damaged_cache_retains_api_solo_without_fabricating_other_data(self):
        for mode in ("absent", "damaged_flex"):
            with self.subTest(mode=mode):
                with self.core.transaction() as db:
                    db.execute("DROP TABLE member_ranks")
                    db.execute("DELETE FROM riot_profiles")
                    db.execute("UPDATE members SET current_tier='골드 2',current_tier_lp=37,current_tier_source='riot',current_tier_updated_at=NULL WHERE id=?", (self.mid,))
                    if mode == "damaged_flex":
                        db.execute("INSERT INTO riot_profiles VALUES(?,?,?,?)", (self.mid, "member#qa", json.dumps(dict(PROFILE, flex_rank_wins=-1)), self.clock.value))
                upgraded = Core(self.path)
                member = upgraded.get_member(self.mid)
                self.assertEqual((member["current_tier"], member["current_tier_lp"], member["current_tier_source"]), ("골드 2", 37, "riot"))
                ranks = self.rows()
                self.assertEqual([row["queue_type"] for row in ranks], ["SOLO"])
                self.assertTrue(all(ranks[0][field] is None for field in ("wins", "losses", "puuid", "fetched_at")))
                from roly.member_profile_page import profile_header
                header = profile_header(member, None)
                self.assertIn("Riot API · 상세 정보 없음", header)
                self.assertNotIn("수기 입력", header)
                with self.assertRaises(ValueError):
                    upgraded.update_member(self.admin, self.mid, "Member#QA", "TOP", "JG", 170, "must stay API managed", current_tier="실버 1", current_tier_lp=1)

    def test_stale_identity_cannot_replace_current_rank_at_the_storage_boundary(self):
        self.cache()
        before = self.rows()
        with self.assertRaises(ValueError):
            with self.core.transaction() as db:
                save_riot_profile(db, self.mid, "previous#qa", PROFILE, self.clock.value)
        self.assertEqual(self.rows(), before)

    def test_postgres_schema_path_uses_bigint_and_preserves_same_projection_contract(self):
        # Execute the portable PostgreSQL declaration against a local connection;
        # no assertion here claims a live PostgreSQL server rehearsal.
        class DeclarationAdapter:
            def __init__(self, db):
                self.db, self.ddl = db, ""
            @property
            def in_transaction(self):
                return self.db.in_transaction
            def execute(self, sql, params=()):
                if sql == "SELECT to_regclass('member_ranks')":
                    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name='member_ranks'"
                if sql.startswith("CREATE TABLE"):
                    self.ddl = sql
                return self.db.execute(sql, params)
        with self.core.transaction() as db:
            db.execute("DROP TABLE member_ranks")
            adapter = DeclarationAdapter(db)
            initialize_ranks(adapter, postgres=True)
            self.assertIn("member_id BIGINT NOT NULL REFERENCES members(id)", adapter.ddl)
            save_riot_profile(adapter, self.mid, "member#qa", PROFILE, self.clock.value)
            count = db.execute("SELECT COUNT(*) FROM members m " + RANK_JOINS).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.core.get_member(self.mid)["current_tier"], "골드 2")

    def test_database_rejects_duplicate_queue_invalid_division_and_orphan_member(self):
        self.cache()
        for statement, params in (
            ("UPDATE member_ranks SET division=NULL WHERE tier='GOLD'", ()),
            ("UPDATE member_ranks SET division=1 WHERE tier='MASTER'", ()),
            ("UPDATE member_ranks SET wins=-1 WHERE queue_type='SOLO'", ()),
            ("UPDATE member_ranks SET queue_type='SOLO' WHERE queue_type='FLEX'", ()),
            ("UPDATE member_ranks SET member_id=?", (999999,)),
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with self.core.transaction() as db:
                    db.execute(statement, params)
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
