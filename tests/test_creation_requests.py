"""Creation receipts, atomic competing requests and reviewed roster confirmation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.competition import Competition, ROLES
from roly.core import Core
from roly.tournament import TournamentService


class CreationRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "creation-test-password")
        cls.admin = cls.base.login("admin", "creation-test-password")
        Competition(cls.base)
        cls.ids = []
        for i in range(25):
            mid = cls.base.join_member(f"Create{i}#QA", ROLES[i % 5], ROLES[(i + 1) % 5])
            cls.base.approve_member(cls.admin, mid, 100)
            cls.ids.append(mid)
        cls.base.create_account(cls.admin, "host", "creation-host-password", role="member", member_id=cls.ids[-1])
        cls.host = cls.base.login("host", "creation-host-password")

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        temp = TemporaryDirectory(prefix="roly-create-")
        self.addCleanup(temp.cleanup)
        self.core = Core(Path(temp.name) / "create.sqlite3")
        with closing(self.core.connect()) as db:
            self.base._keeper.backup(db)
        self.comp = Competition(self.core)
        self.service = TournamentService(self.core, self.comp)

    def assignments(self, count=10):
        return [{"member_id": mid, "role": ROLES[i % 5]} for i, mid in enumerate(self.ids[:count])]

    def counts(self):
        with self.core.read_snapshot() as db:
            return tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in
                         ("competition_events", "competition_creation_requests", "competition_players", "competition_teams", "competition_games"))

    def auction(self, key, token=None, title="Same title"):
        return self.service.create(token or self.host, title, "2026-10-01T20:00:00+09:00", "saved description",
                                   "AUCTION", 4, "TOURNAMENT", request_key=key)

    def test_normal_receipts_survive_profile_changes_and_reject_changed_intent(self):
        saved = []
        for count in (10, 20):
            key = str(uuid4())
            body = dict(assignments=self.assignments(count), title="Same title", balanced=False, format_name="TOURNAMENT")
            eid = self.comp.create_normal(self.host, **body, request_key=key)
            saved.append((key, body, eid, self.comp.get_event(eid)["roster_token"]))
            self.assertEqual(self.comp.create_normal(self.host, **body, request_key=key), eid)
        before = self.counts()
        original = self.core.get_member(self.ids[0])
        self.core.update_member(self.admin, original["id"], "Renamed#QA", "SUP", "MID", 900,
                                "profile changed after creation", current_tier="마스터", current_tier_lp=100)
        self.core.kick_member(self.admin, original["id"], "later membership change")
        for key, body, eid, snapshot in saved:
            self.assertEqual(self.comp.create_normal(self.host, **body, request_key=key), eid)
            self.assertEqual(self.comp.get_event(eid)["roster_token"], snapshot)
            with self.assertRaisesRegex(ValueError, "같은 생성"):
                self.comp.create_normal(self.host, **{**body, "title": "different"}, request_key=key)
            with self.assertRaisesRegex(ValueError, "같은 생성"):
                self.comp.create_normal(self.admin, **body, request_key=key)
            with self.assertRaisesRegex(ValueError, "같은 생성"):
                self.auction(key)
        self.assertEqual(self.counts(), before)

    def test_concurrent_normal_and_auction_creations_have_one_receipt(self):
        for kind in ("NORMAL", "AUCTION"):
            key = str(uuid4())
            def submit(_):
                if kind == "NORMAL":
                    return self.comp.create_normal(self.host, self.assignments(), "Same title", False, request_key=key)
                return self.auction(key)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(submit, range(4)))
            self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.counts()[:2], (2, 2))
        self.auction(str(uuid4()))
        self.assertEqual(self.counts()[:2], (3, 3))
        key = str(uuid4())
        def different(title):
            try:
                return ("created", self.auction(key, title=title))
            except ValueError:
                return ("rejected", None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(different, ("First", "Second")))
        self.assertEqual(sorted(x[0] for x in outcomes), ["created", "rejected"])
        self.assertEqual(self.counts()[:2], (4, 4))

    def test_failed_creation_rolls_back_receipt_teams_and_fixtures(self):
        key = str(uuid4())
        with patch.object(self.comp, "_make_schedule", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertRaises(sqlite3.OperationalError):
                self.comp.create_normal(self.host, self.assignments(), balanced=False, request_key=key)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
        original = self.comp._save_creation_request
        def save_then_fail(*args):
            original(*args)
            raise sqlite3.OperationalError("synthetic")
        with patch.object(self.comp, "_save_creation_request", side_effect=save_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.auction(key)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
        self.auction(key)
        self.assertEqual(self.counts()[:2], (1, 1))
        for invalid in ("", "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.assertRaisesRegex(ValueError, "UUID"):
                self.auction(invalid)
        self.assertEqual(self.counts()[:2], (1, 1))

    def test_confirm_checks_reviewed_roster_in_same_transaction(self):
        eid = self.auction(str(uuid4()))
        self.service.open_recruitment(self.host, eid)
        self.service.set_participants(self.host, eid, self.assignments(20))
        reviewed = self.comp.get_event(eid)
        changed = self.assignments(20)
        changed[0] = {"member_id": self.ids[20], "role": "TOP"}
        self.service.set_participants(self.admin, eid, changed, expected_roster_token=reviewed["roster_token"])
        with self.assertRaisesRegex(ValueError, "최신 명단"):
            self.service.confirm_participants(self.host, eid, expected_roster_token=reviewed["roster_token"])
        current = self.comp.get_event(eid)
        self.assertEqual(current["status"], "RECRUITING")
        self.assertIsNone(current["participants_confirmed_at"])
        self.service.confirm_participants(self.host, eid, expected_roster_token=current["roster_token"])
        self.assertEqual(self.comp.get_event(eid)["status"], "CAPTAIN_SELECTION")


if __name__ == "__main__":
    unittest.main()
