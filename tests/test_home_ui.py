"""Home regressions against the real entrypoint and disposable databases."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition, ROLES


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


class HomeUITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="roly-home-ui-")
        self.environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.directory.name})
        self.environment.start()
        private_settings = patch("roly.storage_config._runtime_document", side_effect=AssertionError("real settings read"))
        private_settings.start()
        self.addCleanup(private_settings.stop)
        riot_settings = patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("real Riot settings read"))
        riot_settings.start()
        self.addCleanup(riot_settings.stop)
        worker = patch("roly.live_auction.LiveAuction.ensure_worker")
        worker.start()
        self.addCleanup(worker.stop)
        self.at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assert_clean()
        self.core = Core(self.at.session_state["db_path"])
        self.comp = Competition(self.core)
        self.token = self.at.session_state["token"]

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def assert_clean(self):
        self.assertFalse(self.at.exception, [error.message for error in self.at.exception])

    def test_home_routes_preserve_database_session_and_selected_event(self):
        identity = {key: self.at.session_state[key] for key in ("db_path", "demo_id", "token")}
        active = [event for event in self.comp.list_events() if event["status"] not in ("COMPLETED", "CANCELLED")]
        ready = next(event for event in active if event["status"] == "READY")
        auction = next(event for event in active if event["kind"] == "AUCTION")
        self.at.button(key=f"home_event_{ready['id']}").click().run()
        self.assert_clean()
        self.assertEqual(self.at.selectbox(key="events_selection_NORMAL").value, ready["id"])
        self.assertEqual({key: self.at.session_state[key] for key in identity}, identity)
        other = next(event for event in self.comp.list_events() if event["status"] == "COMPLETED")
        self.at.selectbox(key="events_selection_NORMAL").select(other["id"]).run()
        self.at.switch_page("app_pages/home.py").run()
        self.at.button(key=f"home_event_{ready['id']}").click().run()
        self.assert_clean()
        self.assertEqual(self.at.selectbox(key="events_selection_NORMAL").value, ready["id"])
        self.assertEqual({key: self.at.session_state[key] for key in identity}, identity)
        self.at.switch_page("app_pages/home.py").run()
        self.at.button(key=f"home_event_{auction['id']}").click().run()
        self.assert_clean()
        self.assertEqual(self.at.selectbox(key="auction_event").value, auction["id"])
        self.assertEqual({key: self.at.session_state[key] for key in identity}, identity)
        self.at.switch_page("app_pages/home.py").run()
        self.assert_clean()
        self.assertEqual([tab.label for tab in self.at.tabs], ["일반 내전", "경매 내전"])
        self.assertEqual(self.at.button(key="home_create_auction").label, "경매 내전 만들기")
        self.assertFalse(any("클랜원과 함께할 경매" in item.value for item in self.at.markdown))
        self.assertFalse(self.at.get("dialog"))
        self.assertEqual({key: self.at.session_state[key] for key in identity}, identity)
        self.at.selectbox(key="space").select("운영 공간").run()
        operating_path = self.at.session_state["db_path"]
        self.at.switch_page("app_pages/join.py").run()
        self.assert_clean()
        self.assertEqual(self.at.session_state["space"], "운영 공간")
        self.assertEqual(self.at.session_state["db_path"], operating_path)
        self.assertIsNone(self.at.session_state["token"])

    def test_kst_date_month_boundary_void_exclusion_and_actual_results(self):
        today = datetime.now(KST)
        month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        members = self.core.list_members()
        by_role = {role: [member for member in members if member["main_role"] == role] for role in ROLES}
        teams = [[{"member_id": by_role[role][index]["id"], "role": role} for role in ROLES] for index in (0, 1)]
        initial_month = sum(
            game["status"] == "CONFIRMED" and datetime.fromisoformat(game["played_at"]).astimezone(KST).strftime("%Y-%m") == today.strftime("%Y-%m")
            for game in self.core.list_games()
        )
        before = (month_start - timedelta(seconds=1)).astimezone(timezone.utc).isoformat()
        boundary = month_start.astimezone(timezone.utc).isoformat()
        self.core.record_game(self.token, "home-before-month", teams[0], teams[1], "A", played_at=before)
        self.core.record_game(self.token, "home-at-month", teams[0], teams[1], "B", played_at=boundary)
        void_id = self.core.record_game(self.token, "home-void-month", teams[0], teams[1], "A", played_at=boundary)
        self.core.void_game(self.token, void_id, "홈 무효 경기 집계 검증")
        self.at.run()
        self.assert_clean()
        self.assertEqual(self.at.title[0].value, "라운지")
        self.assertFalse(self.at.metric)
        captions = [item.value for item in self.at.caption]
        self.assertIn(f"{today.year}년 {today.month}월 {today.day}일 {WEEKDAYS[today.weekday()]} · 한국 시간", captions)
        self.assertTrue(any(f"이번 달 확정 경기 {initial_month + 1}건" in caption for caption in captions))
        labels = self.comp.game_labels()
        for game_id, label in labels.items():
            detail = self.core.get_game(game_id)
            self.assertTrue(any(item.value == f"{label['team_a_name']} vs {label['team_b_name']}" for item in self.at.markdown))
            winner = label["team_a_name"] if detail["winner"] == "A" else label["team_b_name"]
            self.assertIn(f"단판 · {winner} 승", captions)
        results = [item.value for item in self.at.markdown if item.value in ("1 : 0", "0 : 1")]
        self.assertEqual(len(results), 5)
        confirmed = [game for game in self.core.list_games() if game["status"] == "CONFIRMED"]
        confirmed.sort(key=lambda game: datetime.fromisoformat(game["played_at"]), reverse=True)
        self.assertEqual(results, ["1 : 0" if game["winner"] == "A" else "0 : 1" for game in confirmed[:5]])
        self.assertTrue(any(caption.startswith("개설 ") for caption in captions))

    def test_all_normal_records_link_clears_previous_auction_focus(self):
        members = self.core.list_members()
        assignments = [{"member_id": member["id"], "role": role} for role in ROLES
            for member in [member for member in members if member["main_role"] == role][:2]]
        for index in range(4):
            self.comp.create_normal(self.token, assignments, title=f"추가 일반내전 {index}", balanced=False)
        auction = next(event for event in self.comp.list_events() if event["kind"] == "AUCTION")
        self.at.session_state["focus_event"] = auction["id"]
        self.at.switch_page("app_pages/events.py").run()
        self.assert_clean()
        self.assertEqual(self.at.session_state["events_kind_tab"], "경매")
        self.at.switch_page("app_pages/home.py").run()
        self.at.session_state["focus_event"] = auction["id"]
        self.at.button(key="home_normal_history").click().run()
        self.assert_clean()
        self.assertEqual(self.at.session_state["events_kind_tab"], "일반내전")
        self.assertEqual(self.comp.get_event(self.at.selectbox(key="events_selection_NORMAL").value)["kind"], "NORMAL")
        self.assertFalse(any(box.key == "events_selection_AUCTION" for box in self.at.selectbox))

    def test_pending_requests_are_visible_only_to_admin(self):
        self.core.join_member("가입대기검증#QA", "MID", "SUP")
        self.at.run()
        pending = [member for member in self.core.list_members(True) if member["status"] == "PENDING"]
        self.assertTrue(pending)
        self.assertTrue(all(any(item.value == member["riot_id"] for item in self.at.text) for member in pending))
        self.core.create_account(self.token, "home-host", "test-only-password", role="organizer")
        self.at.session_state["token"] = self.core.login("home-host", "test-only-password")
        self.at.run()
        self.assert_clean()
        self.assertFalse(any(item.value == member["riot_id"] for item in self.at.text for member in pending))
        self.assertFalse(any("최근 가입 신청" in item.value for item in self.at.markdown))
        self.at.session_state["token"] = None
        self.at.run()
        self.assert_clean()
        self.assertFalse(any(item.value == member["riot_id"] for item in self.at.text for member in pending))
        self.assertFalse(any("최근 가입 신청" in item.value for item in self.at.markdown))

    def test_empty_operating_space_has_real_empty_states(self):
        self.at.selectbox(key="space").select("운영 공간").run()
        self.assert_clean()
        self.assertEqual(self.at.title[0].value, "라운지")
        self.assertFalse(self.at.metric)
        captions = [item.value for item in self.at.caption]
        self.assertIn("진행 중인 경매 내전이 없습니다.", captions)
        self.assertIn("확정된 경기 기록이 없습니다.", captions)
        self.assertIn("활동 회원 0명  ·  이번 달 확정 경기 0건  ·  진행 중 경매 0개", captions)
        self.assertIn("진행 중인 일반 내전이 없습니다.", captions)
        self.assertFalse(any("모임 일정" in item.value for item in [*self.at.subheader, *self.at.caption]))
        self.assertFalse(any((button.key or "").startswith("home_event_") for button in self.at.button))
        self.assertFalse(any("최근 가입 신청" in item.value for item in self.at.markdown))

    def test_event_tabs_show_all_active_rows_and_separate_complete_history_without_detail_reads(self):
        base = self.comp.list_events()[0]
        summaries = []
        active_ids = {"NORMAL": [], "AUCTION": []}
        complete_ids = []
        for kind, amount in (("NORMAL", 8), ("AUCTION", 9)):
            for index in range(amount + 5):
                event_id = len(summaries) + 1000
                status = "READY" if index < amount else "COMPLETED" if index < amount + 4 else "CANCELLED"
                summaries.append({**base, "id": event_id, "title": f"{kind} 목록 {index}", "kind": kind,
                                  "status": status, "participant_count": 10 if kind == "NORMAL" else 20,
                                  "team_count": 2 if kind == "NORMAL" else 4})
                if status == "READY":
                    active_ids[kind].append(event_id)
                elif status == "COMPLETED":
                    complete_ids.append(event_id)
        with patch.object(Competition, "list_events", return_value=summaries), patch.object(
            Competition, "get_event", side_effect=AssertionError("home must use saved event summaries")
        ):
            self.at.run()
        self.assert_clean()
        self.assertEqual([tab.label for tab in self.at.tabs], ["일반 내전", "경매 내전"])
        for tab, kind in zip(self.at.tabs, ("NORMAL", "AUCTION")):
            kind_complete_ids = [event["id"] for event in summaries if event["kind"] == kind and event["status"] == "COMPLETED"]
            self.assertEqual([button.key for button in tab.button if (button.key or "").startswith("home_event_")],
                             [f"home_event_{event_id}" for event_id in active_ids[kind] + kind_complete_ids])
            self.assertTrue(any(f" · {10 if kind == 'NORMAL' else 20}명 · " in item.value for item in tab.caption))
            history = next(item for item in tab.expander if item.label == "완료된 내전 4개")
            self.assertEqual([button.key for button in history.button], [f"home_event_{event_id}" for event_id in kind_complete_ids])
        shown = [button.key for button in self.at.button if (button.key or "").startswith("home_event_")]
        expected = {*active_ids["NORMAL"], *active_ids["AUCTION"], *complete_ids}
        self.assertEqual(set(shown), {f"home_event_{event_id}" for event_id in expected})
        self.assertEqual(len(shown), len(expected))
        self.assertEqual([item.value for item in self.at.subheader].count("내전 목록"), 1)
        self.assertNotIn("경매 일정", [item.value for item in self.at.subheader])

    def test_lounge_card_opens_same_profile_page_using_saved_data(self):
        from roly.lounge import Lounge
        lounge = Lounge(self.core)
        lounge.update_profile(self.token, name="테스트 클랜", description="함께할 클랜원을 모집합니다.",
                              founded_on="2025-04-03", capacity=60, contact_url="https://example.com/clan")
        self.at.run()
        self.assert_clean()
        self.assertIn("테스트 클랜", [item.value for item in self.at.subheader])
        self.assertIn(f"{len(self.core.list_members())} / 60명", [item.value for item in self.at.text])
        members = self.core.list_members()
        component = self.at.get("bidi_component")[0]
        self.assertEqual({item["member_id"] for item in json.loads(component.proto.json)["members"]}, {item["id"] for item in members})
        self.assertFalse(any((widget.key or "").startswith("home_member_page") for widget in self.at.selectbox))
        member = self.core.list_members()[0]
        self.at.text_input(key="home_member_search").set_value(member["riot_id"]).run()
        self.assert_clean()
        component = self.at.get("bidi_component")[0]
        self.assertEqual([item["riot_id"] for item in json.loads(component.proto.json)["members"]], [member["riot_id"]])
        states = self.at._tree.get_widget_states()
        selection = states.widgets.add()
        # AppTest has no CCv2 click helper; send the native trigger envelope.
        from streamlit.components.v2.bidi_component.main import _make_trigger_id
        selection.id = _make_trigger_id(component.proto.id, "events")
        selection.json_trigger_value = json.dumps([{"event": "selected", "value": member["id"]}])
        self.at._run(states)
        self.assert_clean()
        self.assertFalse(self.at.get("dialog"))
        self.assertEqual(self.at.query_params.get("member"), [str(member["id"])])
        self.assertTrue(any(item.value == "주력 챔피언" for item in self.at.subheader))
        self.assertTrue(any("전력점수" in item.value for item in [*self.at.caption, *self.at.markdown]))

    def test_profile_save_survives_navigation(self):
        from roly.lounge import Lounge
        next(button for button in self.at.button if button.label == "소개 수정").click().run()
        next(widget for widget in self.at.text_input if widget.label == "클랜명").set_value("수정된 클랜")
        next(button for button in self.at.button if button.label == "소개 저장").click().run()
        self.assert_clean()
        self.assertEqual(Lounge(self.core).profile()["name"], "수정된 클랜")
        self.at.switch_page("app_pages/members.py").run()
        self.at.switch_page("app_pages/home.py").run()
        self.assertIn("수정된 클랜", [item.value for item in self.at.subheader])

    def test_creation_buttons_open_existing_forms_without_creating_events(self):
        from uuid import uuid4
        receipt = self.core.register_member("lounge-member", "synthetic-lounge-password", "라운지개설#QA", "TOP", "JG", request_key=str(uuid4()))
        self.core.approve_member(self.token, receipt["member_id"], 200)
        self.at.session_state["token"] = self.core.login("lounge-member", "synthetic-lounge-password")
        identity = {key: self.at.session_state[key] for key in ("db_path", "demo_id", "token")}
        events_before = self.comp.list_events()
        self.at.run()
        self.assert_clean()
        self.assertEqual(self.core.session(identity["token"])["role"], "member")
        normal_button = self.at.button(key="home_create_normal")
        auction_button = self.at.button(key="home_create_auction")
        self.assertFalse(normal_button.disabled)
        self.assertFalse(auction_button.disabled)
        self.assertEqual((normal_button.proto.type, auction_button.proto.type), ("primary", "primary"))
        self.at.session_state["focus_event"] = events_before[0]["id"]
        normal_button.click().run()
        self.assert_clean()
        self.assertEqual(self.at.title[0].value, "일반내전")
        self.assertTrue(any(item.label == "내전 이름" for item in self.at.text_input))
        self.assertNotIn("focus_event", self.at.session_state)
        self.at.switch_page("app_pages/home.py").run()
        self.at.session_state["home_dialog"] = ("profile", None)
        self.at.session_state["t_preparation_dialog"] = ("participants", events_before[0]["id"])
        self.at.button(key="home_create_auction").click().run()
        self.assert_clean()
        self.assertTrue(self.at.get("dialog"))
        self.assertTrue(any(item.label == "경매 생성" for item in self.at.button))
        self.assertNotIn("t_preparation_dialog", self.at.session_state)
        self.assertNotIn("home_dialog", self.at.session_state)
        self.assertEqual({key: self.at.session_state[key] for key in identity}, identity)
        self.assertEqual(self.comp.list_events(), events_before)

    def test_creation_buttons_disable_for_guest_pending_and_revoked_sessions(self):
        from uuid import uuid4
        receipt = self.core.register_member("lounge-pending", "synthetic-lounge-password", "라운지대기#QA", "TOP", "JG", request_key=str(uuid4()))
        pending_token = self.core.login("lounge-pending", "synthetic-lounge-password")
        events_before = self.comp.list_events()
        for token in (None, pending_token):
            with self.subTest(token_kind="guest" if token is None else "pending"):
                self.at.session_state["token"] = token
                self.at.run()
                self.assert_clean()
                if token is None:
                    for key in ("home_create_normal", "home_create_auction"):
                        self.assertTrue(self.at.button(key=key).disabled)
                else:
                    self.assertTrue(any("가입 승인 후 이용할 수 있습니다" in item.value for item in self.at.info))
                    self.assertFalse(any((button.key or "").startswith("home_create_") for button in self.at.button))
                self.assertFalse(self.at.get("dialog"))
        self.core.approve_member(self.token, receipt["member_id"], 200)
        self.at.session_state["token"] = pending_token
        self.at.run()
        self.assertFalse(self.at.button(key="home_create_normal").disabled)
        self.core.logout(pending_token)
        self.at.button(key="home_create_auction").click().run()
        self.assert_clean()
        self.assertEqual(self.at.title[0].value, "라운지")
        self.assertFalse(self.at.get("dialog"))
        self.assertTrue(self.at.button(key="home_create_auction").disabled)
        self.assertEqual(self.comp.list_events(), events_before)


if __name__ == "__main__":
    unittest.main()
