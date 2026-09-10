"""Captain selection and compact settings, including stale-dialog boundaries."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from tests import test_live_auction_ui
from roly.core import ROLES


class AuctionSetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_live_auction_ui.LiveAuctionUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def create_captain_selection(self, token):
        f = self.fixture
        event_id = f.preparation.create(token, "회원 주최 경매 준비 검증",
            datetime.now(timezone.utc).isoformat(), build_mode="AUCTION", team_count=4)
        f.preparation.open_recruitment(token, event_id)
        f.preparation.set_participants(token, event_id, [
            {"member_id": member_id, "role": ROLES[index % 5]} for index, member_id in enumerate(f.ids)])
        f.preparation.confirm_participants(token, event_id)
        f.event_id = event_id
        return event_id

    def test_member_host_selects_captains_then_settings_then_starts(self):
        f = self.fixture
        token = f.tokens[0]
        event_id = self.create_captain_selection(token)
        app = f.app(token)
        self.assertFalse(any(widget.label == "선수별 입찰 시간 (초)" for widget in app.selectbox))
        f.click(app, "팀장 지정하기")
        for member_id in f.captains:
            app.button(key=f"t_captain_toggle_{event_id}_{member_id}").click().run()
        f.click(app, "선택 완료")
        self.assertEqual(f.preparation.get_event(event_id)["status"], "TEAM_BUILDING")
        self.assertFalse(any(widget.label == "경매 준비 완료" for widget in app.button))
        self.assertFalse(any(widget.label == "선수별 입찰 시간 (초)" for widget in app.selectbox))
        f.click(app, "경매 설정하기")
        self.assertEqual(f.preparation.get_event(event_id)["status"], "AUCTION_READY")
        duration = f.widget(app, "selectbox", "선수별 입찰 시간 (초)")
        self.assertEqual(duration.value, 10)
        self.assertEqual(duration.options, ["5초", "10초", "15초", "20초", "25초", "30초"])
        duration.set_value(15)
        f.widget(app, "number_input", "1팀 시작 포인트").set_value(800)
        f.click(app, "경매 설정 저장")
        state = f.state()
        self.assertEqual(state["bid_seconds"], 15)
        self.assertEqual(state["teams"][0]["budget"], 800)
        self.assertFalse(any(widget.label == "선수별 입찰 시간 (초)" for widget in app.selectbox))
        self.assertTrue(any("15초" in item.value and "준비 완료" in item.value for item in app.markdown))
        self.assertNotIn("t_preparation_dialog", app.session_state)
        f.click(app, "경매 시작")
        self.assertEqual(f.state()["status"], "RUNNING")
        self.assertEqual(f.state()["current_lot"]["remaining_seconds"], 15)

    def test_cancelling_unsaved_settings_keeps_start_disabled(self):
        f = self.fixture
        app = f.app(f.admin)
        self.assertTrue(f.widget(app, "button", "경매 시작").disabled)
        self.assertFalse(app.number_input)
        f.click(app, "경매 설정하기")
        f.widget(app, "number_input", "1팀 시작 포인트").set_value(9000)
        f.widget(app, "selectbox", "선수별 입찰 시간 (초)").set_value(30)
        f.click(app, "취소")
        self.assertIsNone(f.state())
        self.assertTrue(f.widget(app, "button", "경매 시작").disabled)
        self.assertFalse(app.number_input)
        f.click(app, "경매 설정하기")
        self.assertEqual(f.widget(app, "selectbox", "선수별 입찰 시간 (초)").value, 10)
        self.assertNotEqual(f.widget(app, "number_input", "1팀 시작 포인트").value, 9000)

    def test_saved_settings_reopen_without_reordering_and_no_form_on_reconnect(self):
        f = self.fixture
        app = f.app(f.admin)
        f.click(app, "경매 설정하기")
        f.widget(app, "selectbox", "선수별 입찰 시간 (초)").set_value(30)
        f.click(app, "경매 설정 저장")
        order = [row["member_id"] for row in f.state()["lots"]]
        reconnected = f.app(f.admin)
        self.assertFalse(reconnected.number_input)
        self.assertFalse(any(widget.label == "선수별 입찰 시간 (초)" for widget in reconnected.selectbox))
        f.click(reconnected, "경매 설정하기")
        self.assertEqual(f.widget(reconnected, "selectbox", "선수별 입찰 시간 (초)").value, 30)
        f.widget(reconnected, "selectbox", "선수별 입찰 시간 (초)").set_value(5)
        f.click(reconnected, "경매 설정 저장")
        self.assertEqual(f.state()["bid_seconds"], 5)
        self.assertEqual([row["member_id"] for row in f.state()["lots"]], order)

    def test_other_captain_cannot_open_host_settings(self):
        f = self.fixture
        app = f.app(f.tokens[0])
        self.assertFalse(any(widget.label in ("경매 설정하기", "경매 시작") for widget in app.button))
        app.session_state["t_preparation_dialog"] = ("settings", f.event_id)
        app.run()
        f.healthy(app)
        self.assertFalse(any(widget.label == "경매 설정 저장" for widget in app.button))
        with self.assertRaises(PermissionError):
            f.live.configure(f.tokens[0], f.event_id)

    def test_stale_settings_after_other_operator_save_require_reload(self):
        f = self.fixture
        app = f.app(f.admin)
        f.click(app, "경매 설정하기")
        f.widget(app, "number_input", "1팀 시작 포인트").set_value(9990)
        f.live.configure(f.admin, f.event_id, bid_seconds=25)
        f.click(app, "경매 설정 저장")
        self.assertEqual(f.state()["bid_seconds"], 25)
        self.assertNotEqual(f.state()["teams"][0]["budget"], 9990)
        self.assertTrue(any("다른 화면" in warning.value for warning in app.warning))
        f.click(app, "최신 경매 설정 불러오기")
        self.assertEqual(f.widget(app, "selectbox", "선수별 입찰 시간 (초)").value, 25)

    def test_started_or_expired_session_cannot_save_open_settings(self):
        f = self.fixture
        app = f.app(f.admin)
        f.click(app, "경매 설정하기")
        f.live.configure(f.admin, f.event_id, bid_seconds=10)
        f.live.start(f.admin, f.event_id)
        f.click(app, "경매 설정 저장")
        self.assertEqual(f.state()["status"], "RUNNING")
        self.assertFalse(any(widget.label == "경매 설정 저장" for widget in app.button))
        # A different unstarted event exercises a genuinely revoked token.
        f.event_id = f.prepare_event(f.admin)
        app = f.app(f.admin)
        f.click(app, "경매 설정하기")
        f.core.logout(f.admin)
        f.click(app, "경매 설정 저장")
        self.assertIsNone(f.state())
        self.assertFalse(any(widget.label == "경매 설정 저장" for widget in app.button))

    def test_expired_member_host_cannot_confirm_an_open_captain_dialog(self):
        f = self.fixture
        token = f.tokens[0]
        event_id = self.create_captain_selection(token)
        app = f.app(token)
        f.click(app, "팀장 지정하기")
        for member_id in f.captains:
            app.button(key=f"t_captain_toggle_{event_id}_{member_id}").click().run()
        f.core.logout(token)
        f.click(app, "선택 완료")
        event = f.preparation.get_event(event_id)
        self.assertEqual(event["status"], "CAPTAIN_SELECTION")
        self.assertFalse(event["teams"])
        self.assertFalse(any(widget.label == "선택 완료" for widget in app.button))

    def test_captain_cards_keep_hidden_choices_and_selection_order(self):
        f = self.fixture
        event_id = self.create_captain_selection(f.tokens[0])
        app = f.app(f.tokens[0])
        f.click(app, "팀장 지정하기")
        self.assertFalse(app.multiselect)
        self.assertTrue(app.button(key=f"t_captains_confirm_{event_id}").disabled)
        # The host need not be a captain, and click order assigns team numbers.
        selected = [f.ids[6], f.ids[1], f.ids[11], f.ids[16]]
        app.button(key=f"t_captain_toggle_{event_id}_{selected[0]}").click().run()
        from roly.riot_profile import member_profiles
        with patch("roly.riot_profile.member_profiles", wraps=member_profiles) as loaded:
            app.text_input(key=f"t_captain_query_{event_id}").set_value("no-matching-member").run()
        self.assertEqual(loaded.call_args.args[1], [])
        self.assertEqual(app.session_state[f"t_captain_draft_{event_id}"]["selected"], selected[:1])
        self.assertFalse(any(str(widget.key).startswith("t_captain_toggle_") for widget in app.button))
        app.text_input(key=f"t_captain_query_{event_id}").set_value("").run()
        for member_id in selected[1:]:
            app.button(key=f"t_captain_toggle_{event_id}_{member_id}").click().run()
        self.assertTrue(app.button(key=f"t_captain_toggle_{event_id}_{f.ids[0]}").disabled)
        app.button(key=f"t_captain_toggle_{event_id}_{selected[1]}").click().run()
        self.assertTrue(app.button(key=f"t_captains_confirm_{event_id}").disabled)
        app.button(key=f"t_captain_toggle_{event_id}_{selected[1]}").click().run()
        f.click(app, "선택 완료")
        expected = [selected[0], selected[2], selected[3], selected[1]]
        self.assertEqual([team["captain_id"] for team in f.preparation.get_event(event_id)["teams"]], expected)

    def test_cancel_captain_selection_discards_draft_without_database_writes(self):
        f = self.fixture
        event_id = self.create_captain_selection(f.admin)
        before = f.preparation.get_event(event_id)
        app = f.app(f.admin)
        f.click(app, "팀장 지정하기")
        app.button(key=f"t_captain_toggle_{event_id}_{f.ids[1]}").click().run()
        f.click(app, "취소")
        after = f.preparation.get_event(event_id)
        self.assertEqual(after["roster_token"], before["roster_token"])
        self.assertFalse(after["teams"])
        self.assertNotIn(f"t_captain_draft_{event_id}", app.session_state)
        f.click(app, "팀장 지정하기")
        self.assertEqual(app.session_state[f"t_captain_draft_{event_id}"]["selected"], [])

    def test_captain_card_escapes_member_text_and_uses_cached_rank(self):
        from roly.tournament_ui import _captain_card
        html = _captain_card({"riot_id": '<img src=x onerror=alert(1)>#TAG', "role": "JG",
                              "main_role_snapshot": "MID", "sub_role_snapshot": "SUP"},
                             {"current_tier": "골드 2", "lp": 10}, 2)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)
        self.assertIn("골드 2", html)
        self.assertIn("주 미드", html)
        self.assertIn("부 서포터", html)
        self.assertIn("2팀 선택", html)


if __name__ == "__main__":
    unittest.main()
