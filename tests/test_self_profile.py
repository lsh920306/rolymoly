"""A member rename preserves account identity and immutable match rosters."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.riot_api import RiotConfig
from roly.riot_sync import RiotSync
from roly.tournament import TournamentService
from tests.test_riot_sync import Clock, FakeClient


class SelfProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="roly-self-profile-base-")
        cls.base = Core(Path(cls.directory.name) / "base.sqlite3")
        cls.base.setup_admin("admin", "synthetic-admin-password")
        cls.admin = cls.base.login("admin", "synthetic-admin-password")
        receipt = cls.base.register_member("member", "synthetic-member-password", "Original#QA", "TOP", "JG",
            request_key=str(uuid4()), current_tier="골드 2", current_tier_lp=40)
        cls.mid, cls.account_id = receipt["member_id"], receipt["account_id"]
        cls.base.approve_member(cls.admin, cls.mid, 170, "private staff notes")
        cls.base.update_member(cls.admin, cls.mid, "Original#QA", "TOP", "JG", 170, "clan review", clan_tier="플래티넘 3")
        cls.token = cls.base.login("member", "synthetic-member-password")
        cls.other = cls.base.join_member("Other#QA", "JG", "MID")
        cls.base.approve_member(cls.admin, cls.other, 100)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="roly-self-profile-case-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "isolated.sqlite3"
        with closing(self.base.connect()) as source, closing(sqlite3.connect(self.path)) as destination:
            source.backup(destination)
        self.core = Core(self.path)
        self.no_http = patch("roly.riot_api._http_get", side_effect=AssertionError("No HTTP during rename"))
        self.no_http.start()
        self.addCleanup(self.no_http.stop)

    def rename(self, name="Renamed#QA", token=None, version=None):
        return self.core.update_own_riot_id(token or self.token, name,
            expected_updated_at=version or self.core.get_member(self.mid)["updated_at"])

    def dump(self):
        with closing(self.core.connect()) as db:
            return tuple(db.iterdump())

    def cached(self):
        member = self.core.get_member(self.mid)
        with self.core.transaction() as db:
            db.execute("INSERT INTO riot_profiles(member_id,canonical_id,payload,fetched_at) VALUES(?,?,?,?)",
                (self.mid, member["canonical_id"], json.dumps({"current_tier": "다이아몬드 1", "lp": 70,
                 "flex_current_tier": "플래티넘 2", "champions": []}), 2_000_000_000.0))
            db.execute("UPDATE members SET current_tier='다이아몬드 1',current_tier_lp=70,current_tier_source='riot',current_tier_updated_at=updated_at WHERE id=?", (self.mid,))
        sync = RiotSync(self.core, RiotConfig("synthetic-key"), client_factory=FakeClient, clock=Clock())
        self.assertEqual(sync.enqueue(self.token, [self.mid], force=True), 1)
        return sync

    def test_rename_retains_account_session_login_and_admin_fields(self):
        before = self.core.get_member(self.mid)
        actor = self.core.session(self.token)
        with self.core.read_snapshot() as db:
            account = dict(db.execute("SELECT * FROM accounts WHERE id=?", (self.account_id,)).fetchone())
        with patch("roly.core.now", return_value=before["updated_at"]):
            self.assertEqual(self.rename("  New Name # KR1  "), self.mid)
        after = self.core.get_member(self.mid)
        self.assertEqual((after["riot_id"], after["canonical_id"]), ("New Name#KR1", "new name#kr1"))
        for field in ("id", "status", "main_role", "sub_role", "clan_tier", "base_score", "score", "wins", "losses", "award_units", "notes", "application_notes", "created_at"):
            self.assertEqual(after[field], before[field], field)
        self.assertGreater(after["updated_at"], before["updated_at"])
        self.assertEqual((after["current_tier"], after["current_tier_lp"], after["current_tier_updated_at"]), ("", None, None))
        current_actor = self.core.session(self.token)
        self.assertEqual((current_actor["id"], current_actor["member_id"], current_actor["username"]),
            (actor["id"], self.mid, "member"))
        self.assertEqual(current_actor["display_name"], "New Name#KR1")
        with self.core.read_snapshot() as db:
            updated_account = dict(db.execute("SELECT * FROM accounts WHERE id=?", (self.account_id,)).fetchone())
            self.assertEqual({key: value for key, value in updated_account.items() if key != "display_name"},
                {key: value for key, value in account.items() if key != "display_name"})
            audit = db.execute("SELECT * FROM audit WHERE action='MEMBER_SELF_RENAME'").fetchone()
            self.assertEqual((audit["actor_id"], audit["target"]), (self.account_id, str(self.mid)))
            self.assertEqual(json.loads(audit["details"])["riot_id_before"], "Original#QA")
        self.assertEqual(self.core.session(self.core.login("member", "synthetic-member-password"))["member_id"], self.mid)

    def test_only_approved_linked_current_session_can_rename(self):
        version = self.core.get_member(self.mid)["updated_at"]
        for token in (None, "invalid-session", self.admin):
            with self.subTest(token=token is None), self.assertRaises(PermissionError):
                self.core.update_own_riot_id(token, "Denied#QA", expected_updated_at=version)
        with self.core.transaction() as db:
            db.execute("UPDATE members SET status='PENDING' WHERE id=?", (self.mid,))
        with self.assertRaises(PermissionError):
            self.rename()
        with self.core.transaction() as db:
            db.execute("UPDATE members SET status='APPROVED' WHERE id=?", (self.mid,))
            db.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE token_hash=?", (hashlib.sha256(self.token.encode()).hexdigest(),))
        with self.assertRaises(PermissionError):
            self.rename()
        token = self.core.login("member", "synthetic-member-password")
        with self.core.transaction() as db:
            db.execute("UPDATE accounts SET active=0 WHERE id=?", (self.account_id,))
        with self.assertRaises(PermissionError):
            self.rename(token=token)
        self.assertEqual(self.core.get_member(self.mid)["riot_id"], "Original#QA")

    def test_missing_version_duplicate_id_and_extra_admin_fields_are_rejected(self):
        before = self.dump()
        for version in (None, "", "stale", 1):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.core.update_own_riot_id(self.token, "New#QA", expected_updated_at=version)
        for name in ("NoTag", "#QA", "Name#", "Too#Many#Tags", " other # qa "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.rename(name)
        for key, value in (("member_id", self.other), ("main_role", "MID"), ("sub_role", "SUP"), ("clan_tier", "챌린저"), ("base_score", 9999)):
            with self.subTest(key=key), self.assertRaises(TypeError):
                self.core.update_own_riot_id(self.token, "NoEscalation#QA",
                    expected_updated_at=self.core.get_member(self.mid)["updated_at"], **{key: value})
        self.assertEqual(self.dump(), before)

    def test_admin_edit_makes_old_own_dialog_stale(self):
        old = self.core.get_member(self.mid)
        self.core.update_member(self.admin, self.mid, old["riot_id"], "MID", "AD", 170, "review", clan_tier="마스터",
            expected_updated_at=old["updated_at"])
        before = self.dump()
        with self.assertRaisesRegex(ValueError, "최신"):
            self.rename(version=old["updated_at"])
        self.assertEqual(self.dump(), before)
        self.rename()
        latest = self.core.get_member(self.mid)
        self.assertEqual((latest["main_role"], latest["sub_role"], latest["clan_tier"]), ("MID", "AD", "마스터"))

    def test_concurrent_own_dialogs_have_one_commit_and_one_audit(self):
        version = self.core.get_member(self.mid)["updated_at"]
        barrier = Barrier(2)
        def rename(index):
            barrier.wait(timeout=10)
            try:
                self.rename(f"Choice{index}#QA", version=version)
                return index
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            winners = [result for result in executor.map(rename, (0, 1)) if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.core.get_member(self.mid)["riot_id"], f"Choice{winners[0]}#QA")
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='MEMBER_SELF_RENAME'").fetchone()[0], 1)

    def test_actual_identity_change_clears_both_api_cache_and_queued_work(self):
        sync = self.cached()
        self.rename()
        with self.core.read_snapshot() as db:
            for table in ("riot_profiles", "riot_jobs"):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE member_id=?", (self.mid,)).fetchone())
        self.assertFalse(sync.process_one())
        self.assertEqual(sync.client.calls, [])
        after = self.core.get_member(self.mid)
        self.assertEqual((after["current_tier"], after["current_tier_lp"], after["current_tier_source"], after["current_tier_updated_at"]), ("", None, "manual", None))

    def test_case_only_change_keeps_same_riot_account_and_exact_noop_has_no_audit(self):
        sync = self.cached()
        original = self.dump()
        self.rename("Original#QA")
        self.assertEqual(self.dump(), original)
        self.rename("ORIGINAL#qa")
        member = self.core.get_member(self.mid)
        self.assertEqual((member["current_tier"], member["current_tier_lp"], member["current_tier_source"]), ("다이아몬드 1", 70, "riot"))
        with self.core.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM riot_profiles WHERE member_id=?", (self.mid,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM riot_jobs WHERE member_id=?", (self.mid,)).fetchone())
        self.assertEqual(sync.client.calls, [])

    def test_audit_failure_rolls_back_identity_account_cache_and_queue(self):
        self.cached()
        before = self.dump()
        with patch.object(self.core, "_audit", side_effect=RuntimeError("synthetic failure")), self.assertRaises(RuntimeError):
            self.rename()
        self.assertEqual(self.dump(), before)

    def test_inflight_old_response_cannot_reappear_after_a_to_b_to_a_rename(self):
        sync = RiotSync(self.core, RiotConfig("synthetic-key"), client_factory=FakeClient, clock=Clock())
        self.assertEqual(sync.enqueue(self.token, [self.mid], force=True), 1)
        for _ in range(3):
            self.assertTrue(sync.process_one())
        def change_identity(stage, _identity):
            if stage == "masteries":
                self.rename("Temporary#QA")
                self.rename("Original#QA")
        sync.client.hook = change_identity
        with patch("roly.riot_sync._static_data", return_value=("16.17.1", {})):
            self.assertTrue(sync.process_one())
        member = self.core.get_member(self.mid)
        self.assertEqual((member["riot_id"], member["current_tier"]), ("Original#QA", ""))
        with self.core.read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM riot_profiles WHERE member_id=?", (self.mid,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM riot_jobs WHERE member_id=?", (self.mid,)).fetchone())

    def test_game_and_auction_snapshots_and_captain_permission_survive_rename(self):
        ids = [self.mid, self.other]
        for index in range(2, 20):
            member = self.core.join_member(f"History{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, member, 100)
            ids.append(member)
        comp = Competition(self.core)
        normal = comp.create_normal(self.token, [{"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(ids[:10])])
        game = comp.get_event(normal)["games"][0]
        game_id = comp.record_result(self.token, normal, game["id"], game["team_a"])
        tournament = TournamentService(self.core, comp)
        event = tournament.create(self.token, "Rename continuity", "2030-01-01T20:00:00+09:00", build_mode="AUCTION", team_count=4)
        tournament.open_recruitment(self.token, event)
        tournament.set_participants(self.token, event, [{"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(ids)],
            expected_roster_token=comp.get_event(event)["roster_token"])
        tournament.confirm_participants(self.token, event)
        captains = ids[::5]
        tournament.set_captains(self.token, event, captains)
        tournament.prepare_auction(self.token, event)
        live = LiveAuction(self.core, comp, clock=lambda: 2_000_000_000.0)
        live.configure(self.token, event, order=[member for member in ids if member not in captains])
        live.start(self.token, event)
        snapshots = {}
        with self.core.read_snapshot() as db:
            for table in ("game_players", "competition_players", "competition_teams", "score_ledger", "award_ledger", "registration_requests"):
                snapshots[table] = [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        score_before = self.core.get_member(self.mid)["score"]
        self.rename()
        with self.core.read_snapshot() as db:
            for table, expected in snapshots.items():
                self.assertEqual([tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")], expected, table)
        self.assertEqual(self.core.get_member(self.mid)["score"], score_before)
        historical = next(player for player in self.core.get_game(game_id)["players"] if player["member_id"] == self.mid)
        self.assertEqual((historical["riot_id"], historical["current_riot_id"]), ("Original#QA", "Renamed#QA"))
        receipt = live.place_bid(self.token, event, live.get_state(event)["current_lot"]["id"], 10, str(uuid4()))
        self.assertTrue(receipt["accepted"])
        self.assertEqual(comp.get_event(event)["created_by"], self.account_id)


if __name__ == "__main__":
    unittest.main()
