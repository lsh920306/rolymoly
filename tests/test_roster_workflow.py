"""Member hosts, stale Kakao roster drafts, and unplayed normal replacement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
import unittest

from roly.core import Core
from roly.competition import Competition, ROLES
from roly.tournament import TournamentService
from roly.live_auction import LiveAuction


class RosterWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "roster-testing-password")
        cls.admin = cls.base.login("admin", "roster-testing-password")
        Competition(cls.base)
        cls.ids = []
        for index in range(40):
            member_id = cls.base.join_member(f"명단회원{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            cls.base.approve_member(cls.admin, member_id, 100 + index)
            cls.ids.append(member_id)
        cls.tokens = []
        for index in range(2):
            cls.base.create_account(cls.admin, f"member{index}", "member-testing-password", role="member", member_id=cls.ids[index])
            cls.tokens.append(cls.base.login(f"member{index}", "member-testing-password"))

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.core = Core(Path(self.directory.name) / "roster.sqlite3")
        with closing(self.core.connect()) as db:
            self.base._keeper.backup(db)
        self.comp = Competition(self.core)
        self.service = TournamentService(self.core, self.comp)
        self.host, self.other = self.tokens

    def tearDown(self):
        self.directory.cleanup()

    def assignments(self, count=20, replacement=None):
        ids = self.ids[:count]
        if replacement is not None:
            ids = [self.ids[replacement], *ids[1:]]
        return [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(ids)]

    def recruiting(self):
        event_id = self.service.create(self.host, "카카오톡 확정 명단", "2030-01-01T20:00:00+09:00", build_mode="AUCTION", team_count=4)
        self.service.open_recruitment(self.host, event_id)
        event = self.comp.get_event(event_id)
        self.service.set_participants(self.host, event_id, self.assignments(), expected_roster_token=event["roster_token"])
        return event_id

    def test_approved_member_hosts_normal_results_but_not_others_or_manual_scores(self):
        event_id = self.comp.create_normal(self.host, self.assignments(10), balanced=False)
        event = self.comp.get_event(event_id)
        self.assertEqual(event["created_by"], self.core.session(self.host)["id"])
        game = event["games"][0]
        with self.assertRaises(PermissionError):
            self.comp.record_result(self.other, event_id, game["id"], game["team_a"])
        self.comp.record_result(self.host, event_id, game["id"], game["team_a"])
        self.comp.finalize_event(self.host, event_id)
        self.assertEqual(self.comp.get_event(event_id)["status"], "COMPLETED")
        self.assertEqual(sum(abs(self.core.get_member(mid)["score"] - (100 + self.ids.index(mid))) for mid in self.ids[:10]), 100)
        with self.assertRaises(PermissionError):
            self.core.adjust_score(self.host, self.ids[0], 10, "not an admin")
        with self.assertRaises(PermissionError):
            self.comp.create_normal(None, self.assignments(10))

    def test_auction_host_and_admin_stale_save_and_aba_revision(self):
        event_id = self.recruiting()
        original = self.comp.get_event(event_id)
        with self.assertRaises(PermissionError):
            self.service.set_participants(self.other, event_id, self.assignments(19), original["roster_token"])
        self.service.set_participants(self.admin, event_id, self.assignments(19), original["roster_token"])
        changed = self.comp.get_event(event_id)
        with self.assertRaisesRegex(ValueError, "다른 화면"):
            self.service.set_participants(self.host, event_id, self.assignments(), original["roster_token"])
        self.assertEqual(self.comp.get_event(event_id), changed)
        self.service.set_participants(self.host, event_id, self.assignments(), changed["roster_token"])
        restored = self.comp.get_event(event_id)
        self.assertNotEqual(original["roster_token"], restored["roster_token"])
        with self.assertRaisesRegex(ValueError, "다른 화면"):
            self.service.set_participants(self.admin, event_id, self.assignments(19), original["roster_token"])
        self.service.confirm_participants(self.host, event_id)
        self.service.set_captains(self.host, event_id, self.ids[:20:5])
        self.service.prepare_auction(self.host, event_id)
        self.assertEqual(self.comp.get_event(event_id)["status"], "AUCTION_READY")
        with self.assertRaises(ValueError):
            self.service.set_captains(self.host, event_id, self.ids[1:20:5])

    def test_simultaneous_host_admin_whole_roster_saves_have_one_winner(self):
        event_id = self.recruiting()
        token = self.comp.get_event(event_id)["roster_token"]
        barrier = Barrier(2)

        def save(actor, replacement):
            barrier.wait(timeout=5)
            try:
                self.service.set_participants(actor, event_id, self.assignments(replacement=replacement), token)
                return "SAVED", replacement
            except ValueError as error:
                self.assertIn("다른 화면", str(error))
                return "STALE", replacement

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(save, self.host, 20)
            second = workers.submit(save, self.admin, 25)
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertCountEqual([result[0] for result in results], ["SAVED", "STALE"])
        replacement = next(index for state, index in results if state == "SAVED")
        after = self.comp.get_event(event_id)
        self.assertEqual({p["member_id"] for p in after["players"]}, {p["member_id"] for p in self.assignments(replacement=replacement)})
        self.assertEqual(len([a for a in after["audit"] if a["action"] == "PARTICIPANTS"]), 2)

    def test_member_host_finishes_live_auction_matches_and_awards_once(self):
        event_id = self.recruiting()
        captains = self.ids[:20:5]
        captain_tokens = [self.host]
        for index, member_id in enumerate(captains[1:], start=1):
            self.core.create_account(self.admin, f"captain{index}", "captain-testing-password", role="member", member_id=member_id)
            captain_tokens.append(self.core.login(f"captain{index}", "captain-testing-password"))
        self.service.confirm_participants(self.host, event_id)
        self.service.set_captains(self.host, event_id, captains)
        self.service.prepare_auction(self.host, event_id)
        stamp = [2_000_000_000.0]
        live = LiveAuction(self.core, self.comp, clock=lambda: stamp[0])
        live.configure(self.host, event_id)
        live.start(self.host, event_id)
        # Random lot order is irrelevant: each real captain bids for their
        # predetermined five-role roster through the production bid service.
        for _ in range(16):
            state = live.get_state(event_id)
            lot = state["current_lot"]
            team_index = self.ids.index(lot["member_id"]) // 5
            receipt = live.place_bid(captain_tokens[team_index], event_id, lot["id"], 1, str(uuid4()))
            stamp[0] = receipt["closes_at"]
            live.settle_due()
            state = live.get_state(event_id)
            if state["status"] != "COMPLETED":
                stamp[0] = state["next_at"]
                live.settle_due()
        self.assertEqual(live.get_state(event_id)["status"], "COMPLETED")
        for team in self.service.get_event(event_id)["teams"]:
            self.service.set_team_roles(self.host, event_id, team["id"],
                {player["member_id"]: player["role"] for player in team["players"]})
        self.service.build_bracket(self.host, event_id, "TOURNAMENT")
        self.service.confirm_bracket(self.host, event_id)
        for _ in range(3):
            game = next(game for game in self.comp.get_event(event_id)["games"]
                if game["status"] == "PENDING" and game["team_a"] and game["team_b"])
            self.comp.record_result(self.host, event_id, game["id"], game["team_a"])
        with self.assertRaises(PermissionError):
            self.comp.finalize_event(self.other, event_id)
        winner = self.comp.finalize_event(self.host, event_id)
        self.assertEqual(self.comp.finalize_event(self.host, event_id), winner)
        event = self.comp.get_event(event_id)
        self.assertEqual(event["status"], "COMPLETED")
        winners = {player["member_id"] for player in event["players"] if player["team_id"] == winner}
        self.assertEqual(len(winners), 5)
        for index, member_id in enumerate(self.ids):
            member = self.core.get_member(member_id)
            self.assertEqual(member["score"], 100 + index)
            self.assertEqual(member["award_units"], 1 if member_id in winners else 0)
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM games WHERE tournament_id=?", (str(event_id),)).fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone()[0], 1)

    def test_normal_replacement_preserves_event_rebuilds_10_20_and_refuses_stale(self):
        for count in (10, 20):
            with self.subTest(count=count):
                event_id = self.comp.create_normal(self.host, self.assignments(count), balanced=False, format_name="TOURNAMENT")
                before = self.comp.get_event(event_id)
                replaced = self.assignments(count, replacement=20)
                self.comp.replace_normal_roster(self.host, event_id, replaced, "카카오톡 불참으로 선수 교체", before["roster_token"], balanced=False)
                after = self.comp.get_event(event_id)
                self.assertEqual(after["id"], before["id"])
                self.assertEqual(after["policy_snapshot"], before["policy_snapshot"])
                self.assertEqual(len(after["teams"]), count // 5)
                self.assertEqual(len(after["games"]), 1 if count == 10 else 3)
                self.assertTrue({g["id"] for g in before["games"]}.isdisjoint(g["id"] for g in after["games"]))
                self.assertEqual({p["member_id"] for p in after["players"]}, {p["member_id"] for p in replaced})
                self.assertEqual(after["audit"][0]["action"], "NORMAL_ROSTER_REPLACE")
                with self.assertRaisesRegex(ValueError, "다른 화면"):
                    self.comp.replace_normal_roster(self.admin, event_id, self.assignments(count), "stale form", before["roster_token"])
                self.assertEqual(self.comp.get_event(event_id), after)
                game = next(g for g in after["games"] if g["team_a"] and g["team_b"])
                self.comp.record_result(self.host, event_id, game["id"], game["team_a"])
                with self.assertRaisesRegex(ValueError, "첫 경기"):
                    self.comp.replace_normal_roster(self.host, event_id, replaced, "too late", after["roster_token"])

    def test_normal_failed_rebuild_rolls_back_and_checks_permission_capacity_reason(self):
        event_id = self.comp.create_normal(self.host, self.assignments(20), balanced=False, format_name="TOURNAMENT")
        before = self.comp.get_event(event_id)
        with patch.object(self.comp, "_make_schedule", side_effect=ValueError("fixture construction failed")):
            with self.assertRaisesRegex(ValueError, "construction"):
                self.comp.replace_normal_roster(self.host, event_id, self.assignments(replacement=20), "test rollback", before["roster_token"], balanced=False)
        self.assertEqual(self.comp.get_event(event_id), before)
        for assignments, reason, version in ((self.assignments(10), "wrong capacity", before["roster_token"]), (self.assignments(), "", before["roster_token"]), (self.assignments(), "missing version", None)):
            with self.assertRaises(ValueError):
                self.comp.replace_normal_roster(self.host, event_id, assignments, reason, version)
        with self.assertRaises(PermissionError):
            self.comp.replace_normal_roster(self.other, event_id, self.assignments(), "other host", before["roster_token"])
        self.assertEqual(self.comp.get_event(event_id), before)


if __name__ == "__main__":
    unittest.main()
