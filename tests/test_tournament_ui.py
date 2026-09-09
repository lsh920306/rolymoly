"""Tournament preparation through actual widgets, using only temporary databases."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.competition import Competition
from roly.core import Core, ROLES
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from roly.ui import services


ROOT = Path(__file__).resolve().parents[1]


class TournamentUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="roly-tournament-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.temporary.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(services.clear)
        self.core = Core(Path(self.temporary.name) / "rolymoly.sqlite3")
        self.competition = Competition(self.core)
        self.service = TournamentService(self.core, self.competition)
        self.clock_value = 2_000_000_000.0
        self.live = LiveAuction(self.core, self.competition, clock=lambda: self.clock_value)
        self.live_service_patch = patch("roly.auction_ui.live_service", return_value=self.live)
        self.live_service_patch.start()
        self.addCleanup(self.live_service_patch.stop)
        self.worker_patch = patch.object(self.live, "ensure_worker")
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.addCleanup(self.live.stop_worker)
        self.core.setup_admin("tadmin", "tournament-ui-password", "대회 관리자")
        self.token = self.core.login("tadmin", "tournament-ui-password")
        self.ids = []
        for team in range(4):
            for index, role in enumerate(ROLES):
                member_id = self.core.join_member(f"참가자{team}{index}#UI", role, ROLES[(index + 1) % 5])
                self.core.approve_member(self.token, member_id, 100 + team * 50 + index)
                self.ids.append(member_id)
        self.assignments = [{"member_id": member_id, "role": ROLES[index % 5]} for index, member_id in enumerate(self.ids)]
        self.captains = self.ids[::5]
        self.starts_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.app = self.new_app(self.token)

    def new_app(self, token):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.session_state["space"] = "운영 공간"
        app.session_state["demo_id"] = "isolated-tournament-ui"
        app.session_state["db_path"] = self.core.db_path
        app.session_state["token"] = token
        app.run()
        self.assertFalse(app.exception, [error.message for error in app.exception])
        app.switch_page("app_pages/auction.py").run()
        self.assertFalse(app.exception, [error.message for error in app.exception])
        self.assertTrue(Path(app.session_state["db_path"]).resolve().is_relative_to(Path(self.temporary.name).resolve()))
        return app

    def widget(self, kind, label):
        found = [widget for widget in getattr(self.app, kind) if widget.label == label]
        self.assertEqual(len(found), 1, f"Expected one {kind}: {label}")
        return found[0]

    def healthy(self):
        self.assertFalse(self.app.exception, [error.message for error in self.app.exception])

    def click(self, label):
        self.widget("button", label).click().run()
        self.healthy()

    def create_via_ui(self):
        self.app.button(key="t_open_create").click().run()
        self.healthy()
        self.widget("text_input", "경매 이름").set_value("20인 대회 운영 검증")
        self.widget("text_area", "참가 안내 (선택)").set_value("참가자 확인 후 팀을 편성합니다.")
        self.click("경매 생성")
        event_id = self.app.session_state["auction_event"]
        event = self.service.get_event(event_id)
        self.assertEqual((event["status"], event["team_count"], event["build_mode"]), ("DRAFT", 4, "AUCTION"))
        return event_id

    def test_twenty_players_captains_auction_and_bracket_persist(self):
        event_id = self.create_via_ui()
        self.click("참가자 선택 시작")
        self.click("참가자 등록·수정")
        self.app.multiselect(key=f"t_roster_{event_id}_members").set_value(self.ids).run()
        self.click("참가 명단 저장")
        self.assertEqual(len(self.service.get_event(event_id)["players"]), 20)
        self.assertFalse(any("출석" in button.label for button in self.app.button))
        self.assertFalse(any("출석" in caption.value for caption in self.app.caption))
        self.assertTrue(all("출석" not in table.value.columns for table in self.app.dataframe))
        self.click("참가자 확정")
        self.assertEqual(self.service.get_event(event_id)["status"], "CAPTAIN_SELECTION")
        self.click("팀장 지정하기")
        for member_id in self.captains[:3]:
            self.app.button(key=f"t_captain_toggle_{event_id}_{member_id}").click().run()
        self.healthy()
        self.assertTrue(self.widget("button", "선택 완료").disabled)
        self.app.button(key=f"t_captain_toggle_{event_id}_{self.captains[3]}").click().run()
        self.click("선택 완료")
        self.assertEqual(self.service.get_event(event_id)["status"], "TEAM_BUILDING")
        self.click("경매 설정하기")
        self.assertEqual(self.service.get_event(event_id)["status"], "AUCTION_READY")
        self.click("경매 설정 저장")
        self.click("경매 시작")
        captain_tokens = []
        for index, member_id in enumerate(self.captains):
            self.core.create_account(self.token, f"captain{index}", "captain-ui-password", role="member", member_id=member_id)
            captain_tokens.append(self.core.login(f"captain{index}", "captain-ui-password"))
        # Live bidding widgets have their own UI tests. Advance the real server
        # auction here with a controlled clock to reach its resulting roster.
        for index in range(16):
            lot = self.live.get_state(event_id)["current_lot"]
            team_index = self.ids.index(lot["member_id"]) // 5
            receipt = self.live.place_bid(captain_tokens[team_index], event_id, lot["id"], 0, str(uuid4()))
            self.assertEqual(receipt["closes_at"], min(lot["closes_at"] + 5, self.clock_value + 10))
            self.clock_value = receipt["closes_at"]
            self.live.settle_due()
            state = self.live.get_state(event_id)
            if state["status"] != "COMPLETED":
                self.clock_value = state["next_at"]
                self.live.settle_due()
        self.assertEqual(self.live.get_state(event_id)["status"], "COMPLETED")
        self.app.run()
        self.healthy()
        balanced = self.service.get_event(event_id)
        self.assertFalse(balanced["pool"])
        self.assertEqual({team["captain_id"] for team in balanced["teams"]}, set(self.captains))
        final_cards = [data["team"] for component in self.app.get("bidi_component")
            if (data := json.loads(component.proto.json)).get("kind") == "team"]
        self.assertEqual(len(final_cards), 4)
        self.assertTrue(any(title.value == "경매 완료" for title in self.app.subheader))
        self.assertFalse(any(widget.label == "참가자 검색" for widget in self.app.text_input))
        self.assertFalse(any(title.value == "참가 팀" for title in self.app.subheader))
        for team in balanced["teams"]:
            self.assertEqual(len(team["players"]), 5)
            self.assertEqual({player["role"] for player in team["players"]}, set(ROLES))
            self.assertIn(team["captain_id"], [player["member_id"] for player in team["players"]])
            card = next(item for item in final_cards if item["name"] == team["name"])
            captain = next(player for player in team["players"] if player["member_id"] == team["captain_id"])
            self.assertEqual(card["captain"]["name"], captain["riot_id"])
            self.assertEqual(card["remaining"], f"{team['remaining']:,} P")
            self.assertEqual({player["name"] for player in card["players"]},
                {player["riot_id"] for player in team["players"] if player["member_id"] != team["captain_id"]})
            self.assertTrue(all("낙찰 0 P" in player["meta"] for player in card["players"]))
        spectator = self.new_app(None)
        public_cards = [data["team"] for component in spectator.get("bidi_component")
            if (data := json.loads(component.proto.json)).get("kind") == "team"]
        self.assertEqual(public_cards, final_cards)
        self.assertFalse(any(button.label in ("팀 포지션 저장", "대진표 생성", "대진표 최종 확인") for button in spectator.button))
        self.click("대진표 생성")
        preview = self.service.get_event(event_id)
        self.assertEqual(preview["status"], "BRACKET_SETUP")
        self.assertEqual(len(preview["games"]), 6)
        self.click("대진표 최종 확인")
        self.click("대진표 확정하고 경기 준비")
        ready = self.service.get_event(event_id)
        self.assertEqual(ready["status"], "READY")
        saved_assignments = [(player["member_id"], player["team_id"], player["role"], player["score"]) for player in ready["players"]]
        self.app = self.new_app(self.token)
        self.assertEqual(self.app.selectbox(key="auction_event").value, event_id)
        self.assertTrue(any(button.label == "경매 결과 보기" for button in self.app.button))
        reloaded = self.service.get_event(event_id)
        self.assertEqual(saved_assignments, [(player["member_id"], player["team_id"], player["role"], player["score"]) for player in reloaded["players"]])
        self.assertEqual(self.core.list_games(), [])
        self.assertTrue(all(member["award_units"] == 0 for member in self.core.list_members()))

    def test_duplicate_participant_rejected_without_partial_roster(self):
        event_id = self.create_via_ui()
        self.click("참가자 선택 시작")
        self.click("참가자 등록·수정")
        # Native selection normally prevents duplicates; a forged widget value
        # must still be rejected by the service without partially writing.
        self.app.multiselect(key=f"t_roster_{event_id}_members").set_value([self.ids[0], self.ids[0]]).run()
        self.click("참가 명단 저장")
        self.assertTrue(self.app.error)
        self.assertEqual(self.service.get_event(event_id)["players"], [])
        self.assertEqual(self.service.get_event(event_id)["status"], "RECRUITING")

    def test_old_roster_dialog_cannot_overwrite_an_admin_update(self):
        event_id = self.create_via_ui()
        self.click("참가자 선택 시작")
        self.service.set_participants(self.token, event_id, self.assignments[:10])
        self.app.run()
        self.click("참가자 등록·수정")
        self.app.multiselect(key=f"t_roster_{event_id}_members").set_value(self.ids[:15]).run()
        original = self.service.get_event(event_id)
        self.service.set_participants(self.token, event_id, self.assignments[:19], original["roster_token"])
        saved = self.service.get_event(event_id)
        self.app.run()
        self.healthy()
        self.assertTrue(any("다른 화면" in row.value for row in self.app.warning))
        self.assertTrue(self.app.button(key=f"t_save_roster_{event_id}").disabled)
        self.assertEqual(self.app.multiselect(key=f"t_roster_{event_id}_members").value, self.ids[:15])
        self.assertEqual(self.service.get_event(event_id)["roster_token"], saved["roster_token"])
        self.app.button(key=f"t_roster_reload_{event_id}").click().run()
        self.healthy()
        self.assertEqual(self.app.multiselect(key=f"t_roster_{event_id}_members").value, self.ids[:19])
        self.assertFalse(self.app.button(key=f"t_save_roster_{event_id}").disabled)
        self.app.multiselect(key=f"t_roster_{event_id}_members").set_value(self.ids).run()
        self.click("참가 명단 저장")
        self.assertEqual(len(self.service.get_event(event_id)["players"]), 20)
        self.assertEqual(self.service.get_event(event_id)["status"], "RECRUITING")

    def test_six_and_eight_team_auction_creation_options(self):
        for team_count in (6, 8):
            with self.subTest(team_count=team_count):
                self.app.button(key="t_open_create").click().run()
                self.widget("segmented_control", "참가 인원").set_value(team_count).run()
                self.healthy()
                self.widget("text_input", "경매 이름").set_value(f"{team_count}팀 경매 대회")
                self.widget("selectbox", "경기 방식").set_value("GROUP_STAGE")
                self.click("경매 생성")
                event = self.service.get_event(self.app.session_state["auction_event"])
                self.assertEqual((event["team_count"], event["build_mode"], event["format"]), (team_count, "AUCTION", "GROUP_STAGE"))
                self.assertEqual(self.app.selectbox(key="auction_event").value, event["id"])

    def test_unstarted_legacy_auction_can_use_new_preparation(self):
        event_id = self.competition.create_auction(self.token, self.ids, self.captains, title="기존 경매")
        before = self.service.get_event(event_id)
        self.app.session_state["focus_event"] = event_id
        self.app.run()
        self.healthy()
        self.click("새 경매 준비로 전환")
        after = self.service.get_event(event_id)
        self.assertEqual(after["status"], "AUCTION_READY")
        self.assertEqual([(team["captain_id"], team["budget"], team["remaining"]) for team in before["teams"]],
                         [(team["captain_id"], team["budget"], team["remaining"]) for team in after["teams"]])
        self.assertEqual([player["member_id"] for player in before["players"]], [player["member_id"] for player in after["players"]])
        self.assertTrue(any(button.label == "경매 설정하기" for button in self.app.button))

    def test_other_organizer_and_spectator_are_read_only(self):
        event_id = self.create_via_ui()
        self.core.create_account(self.token, "otherhost", "other-host-password", "다른 진행자", role="organizer")
        other_token = self.core.login("otherhost", "other-host-password")
        self.app = self.new_app(other_token)
        self.assertFalse(any(button.label == "참가자 선택 시작" for button in self.app.button))
        with self.assertRaises((ValueError, PermissionError)):
            self.service.open_recruitment(other_token, event_id)
        self.app = self.new_app(None)
        self.assertFalse(any(button.label in ("경매 만들기", "경매 생성", "참가자 선택 시작") for button in self.app.button))
        self.core.create_account(self.token, "readonlymember", "readonly-member-password", role="member", member_id=self.ids[1])
        self.app = self.new_app(self.core.login("readonlymember", "readonly-member-password"))
        self.assertTrue(any(button.label == "경매 만들기" for button in self.app.button))
        self.assertFalse(any(button.label == "참가자 선택 시작" for button in self.app.button))
        self.assertEqual(self.service.get_event(event_id)["status"], "DRAFT")
        own_event_id = self.create_via_ui()
        self.assertNotEqual(event_id, own_event_id)
        actor = self.core.session(self.app.session_state["token"])
        self.assertEqual(actor["role"], "member")
        self.assertEqual(self.service.get_event(own_event_id)["created_by"], actor["id"])
        self.click("참가자 선택 시작")
        self.assertEqual(self.service.get_event(own_event_id)["status"], "RECRUITING")
        self.assertEqual(self.service.get_event(event_id)["status"], "DRAFT")

    def test_participant_warning_and_exclusion_keep_member_score(self):
        event_id = self.create_via_ui()
        self.service.open_recruitment(self.token, event_id)
        self.service.set_participants(self.token, event_id, self.assignments)
        self.app.run()
        self.healthy()
        member = self.core.get_member(self.ids[0])
        self.click("참가자 관리")
        self.app.selectbox(key=f"t_management_member_{event_id}").set_value(self.ids[0]).run()
        self.assertFalse(any("출석" in widget.label for widget in self.app.selectbox))
        self.assertTrue(all("출석" not in table.value.columns for table in self.app.dataframe))
        self.click("참가자 조치 적용")
        self.assertTrue(self.app.error)
        self.widget("text_input", "조치 사유").set_value("참가자 안내 위반 확인")
        self.click("참가자 조치 적용")
        warned = next(player for player in self.service.get_event(event_id)["players"] if player["member_id"] == self.ids[0])
        self.assertEqual(warned["warning_count"], 1)
        self.click("참가자 관리")
        self.app.selectbox(key=f"t_management_member_{event_id}").set_value(self.ids[0]).run()
        self.widget("selectbox", "참가자 조치").set_value("참가 제외")
        self.widget("text_input", "조치 사유").set_value("")
        self.click("참가자 조치 적용")
        self.assertTrue(self.app.error)
        self.assertEqual(len(self.service.get_event(event_id)["players"]), 20)
        self.widget("text_input", "조치 사유").set_value("본인 참가 취소 요청")
        self.click("참가자 조치 적용")
        excluded = self.service.get_event(event_id)
        self.assertEqual(len(excluded["players"]), 19)
        self.assertIn(self.ids[0], [player["member_id"] for player in excluded["excluded_players"]])
        after = self.core.get_member(self.ids[0])
        self.assertEqual((after["score"], after["status"]), (member["score"], "APPROVED"))

    def test_unstarted_configured_auction_can_reopen_preserved_roster(self):
        event_id = self.create_via_ui()
        self.service.open_recruitment(self.token, event_id)
        self.service.set_participants(self.token, event_id, self.assignments)
        self.service.confirm_participants(self.token, event_id)
        self.service.set_captains(self.token, event_id, self.captains)
        self.service.prepare_auction(self.token, event_id)
        self.app.run()
        self.healthy()
        self.click("경매 설정하기")
        self.click("경매 설정 저장")
        self.assertEqual(self.live.get_state(event_id)["status"], "READY")
        before = self.service.get_event(event_id)
        original_players = [(p["member_id"], p["riot_id"], p["role"], p["score"]) for p in before["players"]]
        self.click("참가 명단 다시 준비")
        self.assertTrue(self.app.error)
        self.assertEqual(self.service.get_event(event_id)["status"], "AUCTION_READY")
        self.assertEqual(self.live.get_state(event_id)["status"], "READY")

        self.core.create_account(self.token, "reopen_other", "reopen-other-password", role="organizer")
        self.core.create_account(self.token, "reopen_member", "reopen-member-password", role="member", member_id=self.ids[1])
        tokens = [self.core.login("reopen_other", "reopen-other-password"), self.core.login("reopen_member", "reopen-member-password"), None]
        for token in tokens:
            with self.subTest(role=self.core.session(token)["role"] if token else "guest"):
                readonly = self.new_app(token)
                self.assertFalse(any(button.label == "참가 명단 다시 준비" for button in readonly.button))
                self.assertFalse(any(expander.label == "참가 명단 다시 준비" for expander in readonly.expander))

        self.widget("text_input", "다시 준비하는 사유").set_value("팀장 오선택으로 다시 준비")
        self.click("참가 명단 다시 준비")
        reopened = self.service.get_event(event_id)
        self.assertEqual(reopened["status"], "RECRUITING")
        self.assertEqual(reopened["teams"], [])
        self.assertEqual([(p["member_id"], p["riot_id"], p["role"], p["score"]) for p in reopened["players"]], original_players)
        self.assertIsNone(self.live.get_state(event_id))
        self.assertTrue(any(row["action"] == "PREPARATION_REOPENED" and "팀장 오선택" in row["detail"] for row in reopened["audit"]))
        self.click("참가자 확정")
        self.assertEqual(self.service.get_event(event_id)["status"], "CAPTAIN_SELECTION")
        self.assertTrue(any(button.label == "팀장 지정하기" for button in self.app.button))
        self.assertEqual(self.core.list_games(), [])


if __name__ == "__main__":
    unittest.main()
