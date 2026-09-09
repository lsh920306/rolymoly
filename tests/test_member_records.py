"""Public history, corrected results and safe dataframe selection changes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.member_records import member_records
from roly.tournament import TournamentService
from roly.ui import services


ROOT = Path(__file__).resolve().parents[1]


class MemberRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-member-records-")
        self.addCleanup(self.temp.cleanup)
        self.core = Core(Path(self.temp.name) / "rolymoly.sqlite3")
        self.comp = Competition(self.core)
        self.core.setup_admin("recordsadmin", "records-test-password")
        self.admin = self.core.login("recordsadmin", "records-test-password")
        self.ids = []
        for index in range(20):
            member_id = self.core.join_member(f"Record{index:02}#KR1", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, member_id, 100, "PRIVATE-MEMBER-NOTE")
            self.ids.append(member_id)

    def normal(self, key="record-game"):
        return self.core.record_game(self.admin, key, self.ids[:5], self.ids[5:10], "A", notes="PRIVATE-GAME-NOTE")

    def test_public_projection_excludes_private_fields_and_unplayed_statistics(self):
        self.core.create_account(self.admin, "private-login", "private-login-password", role="member", member_id=self.ids[0])
        self.core.adjust_score(self.admin, self.ids[0], 25, "PRIVATE-ADJUSTMENT-REASON")
        self.core.grant_award(self.admin, [self.ids[0]], 6, "PRIVATE-AWARD-REASON", "public-projection")
        records = member_records(self.core, self.ids[0])
        self.assertEqual((records["wins"], records["losses"], records["games"], records["tournaments"]), (0, 0, [], []))
        self.assertEqual((records["member"]["score"], records["member"]["stars"], records["member"]["cats"]), (125, 1, 1))
        public_json = json.dumps(records)
        for private in ("PRIVATE-", "private-login", "notes", "canonical_id", "base_score", "password", "salt", "account_id", "member_id", "username", "token_hash"):
            self.assertNotIn(private, public_json)
        self.assertNotIn("kda", public_json.lower())

    def test_normal_and_auction_history_use_actual_games_with_distinct_score_effects(self):
        normal_id = self.comp.create_normal(self.admin, [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(self.ids[:10])])
        normal = self.comp.get_event(normal_id)
        game = normal["games"][0]
        winning_team = next(team["id"] for team in normal["teams"] if any(p["member_id"] == self.ids[0] for p in team["players"]))
        self.comp.record_result(self.admin, normal_id, game["id"], winning_team)
        auction_id = self.comp.create_auction(self.admin, self.ids, self.ids[::5], title="Actual auction", format_name="TOURNAMENT")
        for team_index, team in enumerate(self.comp.get_event(auction_id)["teams"]):
            for member_id in self.ids[team_index * 5 + 1:team_index * 5 + 5]:
                self.comp.bid(self.admin, auction_id, member_id, team["id"], 42 if member_id == self.ids[1] else 0)
        self.comp.finalize_auction(self.admin, auction_id)
        auction = self.comp.get_event(auction_id)
        first_team = auction["teams"][0]["id"]
        game = next(game for game in auction["games"] if first_team in (game["team_a"], game["team_b"]))
        other = game["team_b"] if game["team_a"] == first_team else game["team_a"]
        self.comp.record_result(self.admin, auction_id, game["id"], other)
        records = member_records(self.core, self.ids[0])
        self.assertEqual((records["wins"], records["losses"]), (1, 1))
        self.assertEqual(len(records["games"]), 2)
        self.assertEqual({game["kind"]: game["delta"] for game in records["games"]}, {"NORMAL": 10, "AUCTION": 0})
        self.assertEqual(records["member"]["score"], 110)
        self.assertEqual((self.core.get_member(self.ids[0])["wins"], self.core.get_member(self.ids[0])["losses"]), (1, 0))
        auction_entry = next(item for item in member_records(self.core, self.ids[1])["tournaments"] if item["id"] == auction_id)
        self.assertEqual((auction_entry["price"], auction_entry["score"]), (42, 110))
        self.assertTrue(all(game["title"] and game["team_a_name"] and game["team_b_name"] for game in records["games"]))

    def test_corrected_and_void_games_replace_previous_win_loss_totals(self):
        game_id = self.normal()
        self.assertEqual(member_records(self.core, self.ids[0])["wins"], 1)
        self.core.correct_game(self.admin, game_id, "B", "correct winning team")
        corrected = member_records(self.core, self.ids[0])
        self.assertEqual((corrected["wins"], corrected["losses"], corrected["member"]["score"]), (0, 1, 90))
        self.assertEqual(len(corrected["games"]), 1)
        self.assertEqual(corrected["games"][0]["delta"], -10)
        self.core.void_game(self.admin, game_id, "game did not happen")
        voided = member_records(self.core, self.ids[0])
        self.assertEqual((voided["wins"], voided["losses"], voided["member"]["score"], voided["games"]), (0, 0, 100, []))

    def test_pending_kicked_and_excluded_participation_do_not_appear_as_active_records(self):
        pending = self.core.join_member("PendingRecord#KR1", "TOP", "JG")
        with self.assertRaises(ValueError):
            member_records(self.core, pending)
        self.core.kick_member(self.admin, self.ids[-1], "membership ended")
        with self.assertRaises(ValueError):
            member_records(self.core, self.ids[-1])
        service = TournamentService(self.core, self.comp)
        event_id = service.create(self.admin, "Recruitment", "2026-09-10T20:00:00+09:00", build_mode="AUCTION", team_count=4)
        service.open_recruitment(self.admin, event_id)
        service.set_participants(self.admin, event_id, [{"member_id": self.ids[0], "role": "TOP"}])
        self.assertEqual(len(member_records(self.core, self.ids[0])["tournaments"]), 1)
        service.exclude_participant(self.admin, event_id, self.ids[0], "withdrew from this event")
        self.assertEqual(member_records(self.core, self.ids[0])["tournaments"], [])
        self.assertEqual(self.core.get_member(self.ids[0])["status"], "APPROVED")

    def test_score_and_results_share_one_snapshot_during_concurrent_correction(self):
        game_id = self.normal()
        original_get = self.core.get_member

        def get_then_correct(member_id, conn=None):
            member = original_get(member_id, conn=conn)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(self.core.correct_game, self.admin, game_id, "B", "concurrent correction").result(timeout=3)
            return member

        with patch.object(self.core, "get_member", side_effect=get_then_correct):
            snapshot = member_records(self.core, self.ids[0])
        self.assertEqual((snapshot["member"]["score"], snapshot["wins"], snapshot["losses"]), (110, 1, 0))
        fresh = member_records(self.core, self.ids[0])
        self.assertEqual((fresh["member"]["score"], fresh["wins"], fresh["losses"]), (90, 0, 1))

    def test_member_row_buttons_keep_exact_identity_after_search(self):
        with patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.temp.name}):
            self.addCleanup(services.clear)
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
            app.session_state["space"] = "운영 공간"
            app.session_state["db_path"] = self.core.db_path
            app.session_state["token"] = None
            app.run()
            app.switch_page("app_pages/members.py").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["db_path"], self.core.db_path)
            target = self.core.get_member(self.ids[-1])
            next(item for item in app.text_input if item.label == "클랜원 검색").set_value(target["riot_id"]).run()
            self.assertFalse(app.exception)
            actions = [button for button in app.button if button.label == "프로필 상세보기"]
            self.assertEqual([button.key for button in actions], [f"member_profile_{target['id']}"])
            actions[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.query_params.get("member"), [str(target["id"])])
            self.assertFalse(app.get("dialog"))
            self.assertTrue(any(heading.value == "주력 챔피언" for heading in app.subheader))
            self.assertFalse(any("PRIVATE" in str(table.value) for table in app.dataframe))


if __name__ == "__main__":
    unittest.main()
