"""Security boundaries and official award integrity, using temporary data only."""
import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roly.core import Core, ROLES
from roly.competition import Competition


class SecurityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-security-test-")
        self.core = Core(Path(self.temp.name) / "test.sqlite3")
        self.core.setup_admin("admin", "review-password-1234")
        self.admin = self.core.login("admin", "review-password-1234")
        self.core.create_account(self.admin, "owner", "owner-password-1234", role="organizer")
        self.owner = self.core.login("owner", "owner-password-1234")
        self.competition = Competition(self.core)
        self.members = []
        for i in range(20):
            member = self.core.join_member(f"Member{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5], notes="PRIVATE-REVIEW-NOTE")
            self.core.approve_member(self.admin, member, 100)
            self.members.append(member)

    def tearDown(self):
        self.temp.cleanup()

    def auction(self, token=None, finish_games=True):
        token = token or self.owner
        event_id = self.competition.create_auction(token, self.members, self.members[::5], format_name="TOURNAMENT")
        event = self.competition.get_event(event_id)
        for team, chunk in zip(event["teams"], range(0, 20, 5)):
            for member in self.members[chunk + 1:chunk + 5]:
                self.competition.bid(token, event_id, member, team["id"], 0)
        self.competition.finalize_auction(token, event_id)
        if finish_games:
            while True:
                event = self.competition.get_event(event_id)
                ready = [game for game in event["games"] if game["status"] == "PENDING" and game["team_a"] is not None and game["team_b"] is not None]
                if not ready:
                    break
                game = ready[0]
                self.competition.record_result(token, event_id, game["id"], game["team_a"])
        event = self.competition.get_event(event_id)
        final = max(event["games"], key=lambda game: game["round"])
        winning_ids = [p["member_id"] for p in event["players"] if p["team_id"] == final["winner_team_id"]]
        return event_id, winning_ids

    def test_public_export_excludes_private_notes_and_identity_internals(self):
        contents = self.core.export_members_csv().decode("utf-8-sig")
        fields = next(csv.reader(io.StringIO(contents)))
        self.assertNotIn("notes", fields)
        self.assertNotIn("canonical_id", fields)
        self.assertNotIn("PRIVATE-REVIEW-NOTE", contents)
        self.assertIn("Member0#KR1", contents)

    def test_event_id_is_not_an_authorization_capability(self):
        with self.assertRaises(PermissionError):
            self.core.grant_award(self.owner, self.members[:5], 25, "invented", "direct", event_id=999)
        with self.assertRaises(ValueError):
            with self.core.transaction() as conn:
                self.core.grant_award(self.owner, self.members[:5], 25, "invented", "internal", event_id=999, conn=conn)
        self.assertTrue(all(member["award_units"] == 0 for member in self.core.list_members()))

    def test_other_organizer_cannot_grant_completed_event_awards(self):
        event_id, winners = self.auction(token=self.admin)
        with self.assertRaises(PermissionError):
            with self.core.transaction() as conn:
                self.core.grant_award(self.owner, winners, 1, "not my event", "wrong-owner", event_id=event_id, conn=conn)

    def test_unfinished_event_cannot_pay_an_award(self):
        event_id, _ = self.auction(finish_games=False)
        with self.assertRaises(ValueError):
            with self.core.transaction() as conn:
                self.core.grant_award(self.owner, self.members[:5], 1, "unfinished", "unfinished", event_id=event_id, conn=conn)

    def test_winners_amount_and_finalization_retries_are_verified(self):
        event_id, winners = self.auction()
        losers = [member for member in self.members if member not in winners][:5]
        for target, amount in ((losers, 1), (winners, 25), (winners[:4], 1)):
            with self.assertRaises(ValueError):
                with self.core.transaction() as conn:
                    self.core.grant_award(self.owner, target, amount, "invalid award", "invalid", event_id=event_id, conn=conn)
        winner = self.competition.finalize_event(self.owner, event_id)
        self.assertEqual(self.competition.finalize_event(self.owner, event_id), winner)
        with self.core.transaction() as conn:
            self.core.grant_award(self.owner, winners, 1, "new request spelling", "different-key", event_id=f"0{event_id}", conn=conn)
        self.assertEqual([self.core.get_member(member)["award_units"] for member in winners], [1] * 5)
        with self.core.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone()[0], 1)

    def test_inconsistent_core_game_blocks_competition_award(self):
        event_id, winners = self.auction()
        event = self.competition.get_event(event_id)
        game = next(game for game in event["games"] if game["status"] == "COMPLETED")
        # Simulate a separate mutation that failed to coordinate competition state.
        with self.core.transaction() as conn:
            self.core.void_game(self.admin, game["core_game_id"], "independent void", conn=conn)
        with self.assertRaises(ValueError):
            self.competition.finalize_event(self.owner, event_id)
        self.assertEqual(self.competition.get_event(event_id)["status"], "PLAYING")
        self.assertEqual([self.core.get_member(member)["award_units"] for member in winners], [0] * 5)

    def test_connection_does_not_bypass_event_record_permissions_and_kind(self):
        event_id = self.competition.create_normal(self.admin, [
            {"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(self.members[:10])])
        for token, target, kind, error in ((self.owner, event_id, "NORMAL", PermissionError),
                                          (self.admin, event_id, "AUCTION", ValueError),
                                          (self.admin, 999999, "NORMAL", ValueError)):
            with self.subTest(target=target, kind=kind, error=error):
                with self.core.transaction() as conn, self.assertRaises(error):
                    self.core.record_game(token, "injected-event", self.members[:5], self.members[5:10], "A",
                                          tournament_id=target, kind=kind, conn=conn)
        self.assertEqual(self.core.list_games(), [])

    def test_connection_cannot_mutate_closed_or_already_awarded_event(self):
        event_id, winners = self.auction()
        event = self.competition.get_event(event_id)
        game_id = event['games'][0]['core_game_id']
        game_before = self.core.get_game(game_id)
        with self.core.transaction() as conn:
            self.core.grant_award(self.owner, winners, 1, 'official winner', 'paid', event_id=event_id, conn=conn)
        # Award may exist just before the final event status is committed; both
        # that state and the completed event must protect the immutable results.
        for status in ('PLAYING', 'COMPLETED'):
            if status == 'COMPLETED':
                self.competition.finalize_event(self.owner, event_id)
            for operation in (
                lambda conn: self.core.correct_game(self.admin, game_id, 'B', 'direct correction', conn=conn),
                lambda conn: self.core.void_game(self.admin, game_id, 'direct void', conn=conn),
                lambda conn: self.core.record_game(self.owner, 'extra-after-award', self.members[:5], self.members[5:10],
                                                  'A', tournament_id=event_id, kind='AUCTION', conn=conn),
            ):
                with self.subTest(status=status):
                    with self.core.transaction() as conn, self.assertRaises(ValueError):
                        operation(conn)
        self.assertEqual(self.core.get_game(game_id), game_before)
        self.assertEqual([self.core.get_member(member)['award_units'] for member in winners], [1] * 5)


class UiSpaceTests(unittest.TestCase):
    def test_space_switch_clears_inputs_and_keeps_demo_auth_separate(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory(prefix="roly-ui-space-") as folder:
            with patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": folder}):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
                self.assertFalse(app.exception)
                original_path = app.session_state["db_path"]
                original_token = app.session_state["token"]
                self.assertTrue(original_token)
                app.session_state["stale_private_form"] = "old database form data"
                app.selectbox(key="space").select("운영 공간").run()
                self.assertFalse(app.exception)
                self.assertIsNone(app.session_state["token"])
                self.assertNotEqual(app.session_state["db_path"], original_path)
                with self.assertRaises(KeyError):
                    _ = app.session_state["stale_private_form"]
                app.selectbox(key="space").select("체험 공간").run()
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state["db_path"], original_path)
                self.assertEqual(app.session_state["token"], original_token)
                other = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
                self.assertFalse(other.exception)
                self.assertNotEqual(other.session_state["db_path"], original_path)
                self.assertNotEqual(other.session_state["token"], original_token)

    def test_interrupted_demo_can_start_a_new_private_space(self):
        from streamlit.testing.v1 import AppTest
        def interrupted_seed(core, **kwargs):
            core.setup_admin("demo", "temporary-review-password")
            raise RuntimeError("simulated setup interruption")
        with tempfile.TemporaryDirectory(prefix="roly-ui-recovery-") as folder:
            with patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": folder}):
                with patch("roly.demo.seed", interrupted_seed):
                    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertTrue(app.error)
                failed_path = app.session_state["db_path"]
                reset = next(button for button in app.button if button.label == "새 체험 공간 만들기")
                reset.click().run()
                self.assertFalse(app.exception)
                self.assertNotEqual(app.session_state["db_path"], failed_path)
                self.assertTrue(app.session_state["token"])


if __name__ == "__main__":
    unittest.main()
