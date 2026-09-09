"""Click real auction widgets against isolated shared SQLite state.

AppTest runs server-side Streamlit widgets, not multiple browser clients.
Background workers are deliberately disabled; domain tests cover those threads.
"""
from datetime import datetime, timezone
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.competition import Competition
from roly.core import Core, ROLES
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from roly.ui import services
from tests.live_panel_client import panel_client


ROOT = Path(__file__).resolve().parents[1]


class LiveAuctionUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-live-ui-")
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(services.clear)
        worker = patch.object(LiveAuction, "ensure_worker")
        worker.start()
        self.addCleanup(worker.stop)
        self.core = Core(Path(self.temp.name) / "rolymoly.sqlite3")
        self.comp = Competition(self.core)
        self.preparation = TournamentService(self.core, self.comp)
        self.clock_value = 2_000_000_000.0
        self.live = LiveAuction(self.core, self.comp, clock=lambda: self.clock_value)
        live_service = patch("roly.auction_ui.live_service", return_value=self.live)
        live_service.start()
        self.addCleanup(live_service.stop)
        self.addCleanup(self.live.stop_worker)
        self.core.setup_admin("uiadmin", "ui-admin-password", "경매 관리자")
        self.admin = self.core.login("uiadmin", "ui-admin-password")
        self.ids = []
        for index in range(20):
            member_id = self.core.join_member(f"입찰회원{index}#UI", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, member_id, 100 + index)
            self.ids.append(member_id)
        self.captains = self.ids[::5]
        self.tokens = []
        for index, member_id in enumerate(self.captains):
            self.core.create_account(self.admin, f"captain{index}", "ui-captain-password", f"팀장{index}", role="member", member_id=member_id)
            self.tokens.append(self.core.login(f"captain{index}", "ui-captain-password"))
        self.event_id = self.prepare_event(self.admin)

    def prepare_event(self, token):
        event_id = self.preparation.create(token, "실시간 입찰 UI 검증", datetime.now(timezone.utc).isoformat(), build_mode="AUCTION", team_count=4)
        self.preparation.open_recruitment(token, event_id)
        self.preparation.set_participants(token, event_id, [{"member_id": member_id, "role": ROLES[index % 5]} for index, member_id in enumerate(self.ids)])
        self.preparation.confirm_participants(token, event_id)
        self.preparation.set_captains(token, event_id, self.captains)
        self.preparation.prepare_auction(token, event_id)
        return event_id

    def app(self, token):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.session_state["space"] = "운영 공간"
        app.session_state["demo_id"] = "isolated-live-ui"
        app.session_state["db_path"] = self.core.db_path
        app.session_state["token"] = token
        app.session_state["focus_event"] = self.event_id
        app.run()
        self.healthy(app)
        app.switch_page("app_pages/auction.py").run()
        self.healthy(app)
        self.assertEqual(app.session_state["db_path"], self.core.db_path)
        return app

    def healthy(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])

    def widget(self, app, kind, label):
        if label in ("입찰하기", "입찰할 포인트", "금액 초기화", "+5", "+10", "+20", "+30", "+50", "+70", "+100"):
            return panel_client(app).widget(kind, label)
        matches = [widget for widget in getattr(app, kind)
                   if (kind == "button" and label == "입찰하기" and (widget.key or "").startswith("live_bid_"))
                   or (label != "입찰하기" and widget.label == label)]
        self.assertEqual(len(matches), 1, f"Expected one {kind} labelled {label}")
        return matches[0]

    def click(self, app, label):
        self.widget(app, "button", label).click().run()
        self.healthy(app)

    def client(self, app):
        return panel_client(app)

    def start(self):
        self.live.configure(self.admin, self.event_id, bid_seconds=10)
        return self.live.start(self.admin, self.event_id)

    def state(self):
        return self.live.get_state(self.event_id)

    def presentation(self, app, kind):
        return [data for item in app.get("bidi_component")
                if (data := json.loads(item.proto.json)).get("kind") == kind]

    def test_settings_start_and_captain_click_bid_preserve_budget_until_sale(self):
        admin_app = self.app(self.admin)
        self.click(admin_app, "경매 설정하기")
        self.assertFalse(any("다시 시작" in widget.label for widget in admin_app.checkbox))
        self.assertTrue(any("유효 입찰마다 남은 시간 +5초" in item.value for item in admin_app.caption))
        self.assertEqual(self.widget(admin_app, "selectbox", "선수별 입찰 시간 (초)").options, ["5초", "10초", "15초", "20초", "25초", "30초"])
        self.widget(admin_app, "selectbox", "선수별 입찰 시간 (초)").set_value(10)
        self.widget(admin_app, "number_input", "1팀 시작 포인트").set_value(800)
        self.click(admin_app, "경매 설정 저장")
        self.assertEqual(self.state()["bid_seconds"], 10)
        self.assertEqual(self.state()["teams"][0]["budget"], 800)
        self.click(admin_app, "경매 시작")
        self.assertEqual(self.state()["status"], "RUNNING")
        captain = self.app(self.tokens[0])
        before = self.state()["teams"][0]["remaining"]
        self.click(captain, "+10")
        self.assertEqual(self.widget(captain, "number_input", "입찰할 포인트").value, 10)
        self.click(captain, "입찰하기")
        state = self.state()
        self.assertEqual(state["current_lot"]["highest_bid"], 10)
        self.assertEqual(state["current_lot"]["highest_team_id"], state["teams"][0]["id"])
        self.assertEqual(state["teams"][0]["remaining"], before)
        self.assertEqual(len(state["teams"][0]["players"]), 1)
        display = self.presentation(captain, "stage")[0]
        self.assertEqual(display["price"], "최고 입찰 10 P")
        self.assertIn("1팀 · 팀장 입찰회원0#UI", display["bidder"])
        self.assertNotEqual(display["player"]["name"], self.core.get_member(self.captains[0])["riot_id"])
        self.clock_value = state["current_lot"]["closes_at"]
        self.live.settle_due()
        captain.run()
        self.healthy(captain)
        self.assertEqual(self.state()["teams"][0]["remaining"], before - 10)
        self.assertEqual(len(self.state()["teams"][0]["players"]), 2)
        card = self.presentation(captain, "team")[0]["team"]
        self.assertEqual(card["captain"]["name"], "입찰회원0#UI")
        self.assertEqual(len(card["players"]), 1)
        self.assertIn("낙찰 10 P", card["players"][0]["meta"])
        self.assertEqual(card["remaining"], "790 P")

    def test_ten_second_cap_and_three_second_transition_in_captain_view(self):
        # This test checks exact server-clock boundaries. Rendering elapsed
        # time is tested separately by the component's countdown regression.
        self.enterContext(patch("roly.auction_ui.monotonic", return_value=100.0))
        started = self.start()
        self.clock_value += 3  # Seven seconds remain.
        captain = self.app(self.tokens[0])
        self.click(captain, "+10")
        self.click(captain, "입찰하기")
        current = self.state()["current_lot"]
        self.assertEqual(current["remaining_seconds"], 10)
        self.assertEqual(self.presentation(captain, "stage")[0]["seconds"], 10)
        self.clock_value = current["closes_at"]
        self.live.settle_due()
        captain.run()
        self.assertEqual(self.state()["next_in_seconds"], 3)
        display = self.presentation(captain, "stage")[0]
        self.assertEqual(display["seconds"], 3)
        self.assertEqual(display["clock_label"], "다음 선수 준비")
        self.assertTrue(self.widget(captain, "button", "입찰하기").disabled)
        self.clock_value += 2
        self.live.settle_due()
        self.assertEqual(self.state()["current_lot"]["id"], current["id"])
        self.clock_value += 1
        self.live.settle_due()
        captain.run()
        self.assertNotEqual(self.state()["current_lot"]["id"], current["id"])
        self.assertEqual(self.state()["current_lot"]["remaining_seconds"], 10)
        self.assertFalse(self.widget(captain, "button", "입찰하기").disabled)
        self.click(captain, "전체 팀 보기")
        self.assertEqual(len(self.presentation(captain, "overview")[0]["teams"]), 4)

    def test_waiting_clients_watch_start_without_rerunning_settings_form(self):
        from roly.auction_ui import watch_auction_start

        self.live.configure(self.admin, self.event_id, bid_seconds=10)
        with patch.object(self.live, "get_event_status", wraps=self.live.get_event_status) as status_read:
            captain = self.app(self.tokens[0])
            admin = self.app(self.admin)
        self.assertEqual(status_read.call_count, 2)  # Watch registered for both roles.
        self.assertEqual(len(self.presentation(captain, "auction_sync")), 1)
        self.assertEqual(len(self.presentation(admin, "auction_sync")), 1)
        self.assertTrue(any("진행자가 경매를 준비" in item.value for item in captain.caption))
        self.assertFalse(any((button.key or "").startswith("live_bid_") for button in captain.button))
        self.click(admin, "경매 설정하기")
        self.widget(admin, "selectbox", "선수별 입찰 시간 (초)").set_value(30)
        # AppTest has no browser timer. Execute one poll body deterministically;
        # real-browser QA separately verifies the one-second automatic trigger.
        with patch("roly.auction_ui.st.session_state", SimpleNamespace(db_path=self.core.db_path)), patch("roly.auction_ui.render_auction_sync"), patch("roly.auction_ui.st.rerun") as rerun:
            watch_auction_start.__wrapped__(self.event_id)
            watch_auction_start.__wrapped__(self.event_id)
            rerun.assert_not_called()
        self.click(admin, "경매 설정 저장")
        self.assertEqual(self.state()["bid_seconds"], 30)
        self.live.start(self.admin, self.event_id)
        with patch("roly.auction_ui.st.session_state", SimpleNamespace(db_path=self.core.db_path)), patch("roly.auction_ui.render_auction_sync"), patch("roly.auction_ui.st.rerun") as rerun:
            watch_auction_start.__wrapped__(self.event_id)
            rerun.assert_called_once_with(scope="app")
        from streamlit.components.v2.bidi_component.main import _make_trigger_id
        states = captain._tree.get_widget_states()
        component = next(item for item in captain.get("bidi_component") if json.loads(item.proto.json).get("kind") == "auction_sync")
        signal = states.widgets.add()
        signal.id = _make_trigger_id(component.proto.id, "events")
        signal.json_trigger_value = json.dumps([{"event": "sync", "value": {"sequence": 1, "source": "visibility"}}])
        captain._run(states)
        self.healthy(captain)
        self.assertEqual(len(self.presentation(captain, "auction_sync")), 0)
        self.assertEqual(len(self.presentation(captain, "stage")), 1)
        self.assertFalse(self.widget(captain, "button", "입찰하기").disabled)
        self.assertFalse(any("진행자가 경매를 준비" in item.value for item in captain.caption))

    def test_two_captain_views_compete_and_budget_error_preserves_highest_bid(self):
        self.start()
        first = self.app(self.tokens[0])
        self.click(first, "입찰하기")  # A first 0P bid is valid.
        second = self.app(self.tokens[1])
        self.click(second, "+5")
        self.click(second, "입찰하기")
        state = self.state()
        self.assertEqual(state["current_lot"]["highest_bid"], 5)
        self.assertEqual(state["current_lot"]["highest_team_id"], state["teams"][1]["id"])
        first.run()
        self.healthy(first)
        self.click(first, "+10")
        self.click(first, "입찰하기")
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 15)  # highest 5P + increment 10P
        state = self.state()
        self.widget(second, "number_input", "입찰할 포인트").set_value(state["teams"][1]["remaining"] + 1).run()
        self.click(second, "입찰하기")
        self.assertTrue(any("예산" in error.value for error in second.error))
        after = self.state()
        self.assertEqual(after["current_lot"]["highest_bid"], 15)
        self.assertEqual(len(after["bids"]), 3)
        self.assertEqual([team["remaining"] for team in after["teams"]], [team["budget"] for team in after["teams"]])
        for _ in range(2):
            second.run()
            self.healthy(second)
            self.assertTrue(any("예산" in error.value for error in second.error))
            self.assertEqual(self.widget(second, "number_input", "입찰할 포인트").value, state["teams"][1]["remaining"] + 1)
        self.widget(second, "number_input", "입찰할 포인트").set_value(20).run()
        self.click(second, "입찰하기")
        self.assertFalse(second.error)
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 20)

    def test_overview_stays_open_with_new_sales_and_closes_on_navigation(self):
        self.start()
        captain = self.app(self.tokens[0])
        self.click(captain, "전체 팀 보기")
        self.assertTrue(captain.get("dialog"))
        before = self.presentation(captain, "overview")[0]["teams"][0]
        lot = self.state()["current_lot"]
        receipt = self.live.place_bid(self.tokens[0], self.event_id, lot["id"], 10, str(uuid4()))
        self.clock_value = receipt["closes_at"]
        self.live.settle_due()
        captain.run()
        self.healthy(captain)
        self.assertTrue(captain.get("dialog"))
        after = self.presentation(captain, "overview")[0]["teams"][0]
        self.assertNotEqual(after["remaining"], before["remaining"])
        self.assertEqual(len(after["players"]), len(before["players"]) + 1)
        captain.switch_page("app_pages/members.py").run()
        self.healthy(captain)
        self.assertNotIn("live_overview_event", captain.session_state)
        captain.switch_page("app_pages/auction.py").run()
        self.healthy(captain)
        self.assertFalse(captain.get("dialog"))

    def test_team_carousel_wraps_read_only_and_viewing_another_team_does_not_change_bidder(self):
        self.start()
        lot_id = self.state()["current_lot"]["id"]
        self.live.place_bid(self.tokens[1], self.event_id, lot_id, 10, str(uuid4()))
        captain = self.app(self.tokens[0])
        before = self.state()
        request_key = self.client(captain).request_id
        team_names = [team["name"] for team in before["teams"]]
        self.assertEqual(self.presentation(captain, "team")[0]["team"]["name"], team_names[0])
        # Both edges wrap; each arrow selects a view, never mutates a team.
        for label, index in (("이전 팀", 3), ("다음 팀", 0), ("다음 팀", 1), ("다음 팀", 2)):
            self.click(captain, label)
            self.assertEqual(self.presentation(captain, "team")[0]["team"]["name"], team_names[index])
            self.assertEqual(self.state(), before)
            self.assertEqual(self.client(captain).request_id, request_key)
        # A captain looking at team 3 still bids for their own team 1.
        self.click(captain, "+5")
        self.click(captain, "입찰하기")
        self.assertEqual(self.state()["current_lot"]["highest_team_id"], before["teams"][0]["id"])
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 15)
        self.assertEqual(self.presentation(captain, "team")[0]["team"]["name"], team_names[2])

    def test_amount_reset_only_changes_draft_and_preserves_bid_receipt_budget_and_deadline(self):
        self.start()
        captain = self.app(self.tokens[0])
        lot_id = self.state()["current_lot"]["id"]
        self.click(captain, "+70")
        before = self.state()
        request_key = self.client(captain).request_id
        self.click(captain, "금액 초기화")
        self.assertEqual(self.widget(captain, "number_input", "입찰할 포인트").value, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.client(captain).request_id, request_key)
        self.live.place_bid(self.tokens[1], self.event_id, lot_id, 20, str(uuid4()))
        captain.run()
        self.healthy(captain)
        self.click(captain, "+100")
        self.assertEqual(self.widget(captain, "number_input", "입찰할 포인트").value, 120)
        before = self.state()
        for _ in range(2):
            self.click(captain, "금액 초기화")
            self.assertEqual(self.widget(captain, "number_input", "입찰할 포인트").value, 20)
            self.assertEqual(self.widget(captain, "button", "입찰하기").label, "20 P 입찰하기")
            self.assertEqual(self.state(), before)
            self.assertEqual(self.client(captain).request_id, request_key)

    def test_immutable_command_cannot_change_amount_when_replayed(self):
        self.start()
        captain = self.app(self.tokens[0])
        client = self.client(captain)
        self.click(captain, "+10")
        command = {"lot_id": self.state()["current_lot"]["id"], "amount": 10,
                   "request_id": client.request_id}
        client.submit()
        self.healthy(captain)
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 10)
        before = self.state()
        client.send(command)  # The same accepted intention is a receipt replay.
        self.assertEqual(self.state(), before)
        client.send({**command, "amount": 25})
        self.assertTrue(any("같은 요청 번호" in item.value for item in captain.error))
        self.assertEqual(self.state(), before)
        client.draft = 25
        client.submit()
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 25)

    def test_bid_error_does_not_follow_another_account_or_player(self):
        self.start()
        captain = self.app(self.tokens[0])
        self.widget(captain, "number_input", "입찰할 포인트").set_value(self.state()["teams"][0]["remaining"] + 1).run()
        self.click(captain, "입찰하기")
        self.assertTrue(captain.error)
        captain.session_state["token"] = self.tokens[1]
        captain.run()
        self.healthy(captain)
        self.assertFalse(captain.error)
        self.widget(captain, "number_input", "입찰할 포인트").set_value(self.state()["teams"][1]["remaining"] + 1)
        self.click(captain, "입찰하기")
        self.assertTrue(captain.error)
        lot = self.state()["current_lot"]
        self.clock_value = lot["closes_at"]
        self.live.settle_due()
        self.clock_value = self.state()["next_at"]
        self.live.settle_due()
        captain.run()
        self.healthy(captain)
        self.assertNotEqual(self.state()["current_lot"]["id"], lot["id"])
        self.assertFalse(captain.error)

    def test_valid_bid_click_adds_five_seconds_and_rejected_click_keeps_deadline(self):
        self.live.configure(self.admin, self.event_id, bid_seconds=10)
        initial = self.live.start(self.admin, self.event_id)["current_lot"]["closes_at"]
        captain = self.app(self.tokens[0])
        self.clock_value = initial - 2
        captain.run()
        self.click(captain, "입찰하기")
        self.assertEqual(self.state()["current_lot"]["closes_at"], initial + 5)
        self.assertEqual(self.state()["current_lot"]["remaining_seconds"], 7)
        self.click(captain, "입찰하기")
        self.assertTrue(any("최고 입찰가" in item.value for item in captain.error))
        self.assertEqual(self.state()["current_lot"]["closes_at"], initial + 5)
        self.clock_value = initial
        self.live.settle_due()
        self.assertEqual(self.state()["current_lot"]["status"], "OPEN")
        self.clock_value = initial + 4
        captain.run()
        self.click(captain, "+5")
        self.click(captain, "입찰하기")
        self.assertEqual(self.state()["current_lot"]["remaining_seconds"], 6)
        self.assertEqual(self.state()["current_lot"]["closes_at"], initial + 10)
        self.clock_value = initial + 10
        self.live.settle_due()
        captain.run()
        self.healthy(captain)
        self.assertEqual(self.state()["current_lot"]["status"], "SOLD")

    def test_admin_pause_resume_and_member_has_no_operation_controls(self):
        self.start()
        captain = self.app(self.tokens[0])
        for label in ("일시 정지", "경매 재개", "경매 설정 저장", "경매 대회 생성"):
            self.assertFalse(any(button.label == label for button in captain.button))
        admin = self.app(self.admin)
        self.assertEqual(admin.button(key='live_pause').proto.type, 'primary')
        self.assertEqual(admin.button(key='live_pause').label, '일시 정지')
        self.clock_value += 5
        self.click(admin, "일시 정지")
        self.assertEqual(self.state()["current_lot"]["remaining_seconds"], 5)
        captain.run()
        self.healthy(captain)
        self.assertTrue(any('진행자가 일시 정지했습니다' in caption.value for caption in captain.caption))
        self.assertTrue(self.widget(captain, "button", "입찰하기").disabled)
        self.assertTrue(self.widget(captain, "number_input", "입찰할 포인트").disabled)
        self.clock_value += 500
        self.live.settle_due()
        self.assertEqual(self.state()["status"], "PAUSED")
        self.click(admin, "경매 재개")
        self.assertEqual(self.state()["current_lot"]["closes_at"], self.clock_value + 5)
        captain.run()
        self.healthy(captain)
        self.assertFalse(self.widget(captain, "button", "입찰하기").disabled)
        self.click(captain, "+5")
        self.click(captain, "입찰하기")
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 5)

    def test_retry_button_waits_for_full_round_and_restarts_unsold_from_pause(self):
        self.start()
        admin = self.app(self.admin)

        def progress():
            key = f"live_progress_{self.event_id}"
            if key not in admin.session_state or not admin.session_state[key]:
                admin.session_state[key] = True
                admin.run()
            return next(table.value for table in admin.dataframe if '낙찰 포인트' in table.value.columns)

        self.assertEqual(list(progress().columns), ['순서', '선수', '포지션', '낙찰 포인트', '상태'])
        self.assertTrue((progress()['낙찰 포인트'] == '—').all())
        self.assertEqual(admin.button(key='live_retry').label, '유찰 재시작')
        self.assertTrue(admin.button(key='live_retry').disabled)
        for index in range(16):
            self.clock_value = self.state()['current_lot']['closes_at']
            self.live.settle_due()
            if index == 0:
                admin.run()
                self.healthy(admin)
                self.assertTrue(admin.button(key='live_retry').disabled)
                self.assertTrue(any('현재 순서를 마친 뒤' in caption.value for caption in admin.caption))
                self.assertIn('남은 선수를 모두 진행한 뒤', admin.button(key='live_retry').proto.help)
            if index != 15:
                self.clock_value += 3
                self.live.settle_due()
        admin.run()
        self.healthy(admin)
        self.assertFalse(admin.button(key='live_retry').disabled)
        self.assertTrue(any('유찰 16명' in caption.value for caption in admin.caption))
        self.assertTrue((progress()['상태'] == '유찰').all())
        self.assertTrue((progress()['낙찰 포인트'] == '—').all())
        self.click(admin, '일시 정지')
        self.assertFalse(admin.button(key='live_retry').disabled)
        self.click(admin, '유찰 재시작')
        state = self.state()
        self.assertEqual((state['status'], state['queued_count'], state['next_at']), ('WAITING', 16, self.clock_value + 3))
        self.assertTrue(admin.button(key='live_retry').disabled)
        self.assertFalse(any(button.key == 'live_resume' for button in admin.button))
        self.assertTrue((progress()['낙찰 포인트'] == '—').all())
        self.clock_value = state['next_at']
        self.live.settle_due()
        lot = self.state()['current_lot']
        receipt = self.live.place_bid(self.tokens[0], self.event_id, lot['id'], 0, str(uuid4()))
        admin.run()
        self.healthy(admin)
        self.assertTrue((progress()['낙찰 포인트'] == '—').all(), 'An open 0 P bid is not a completed sale')
        self.clock_value = receipt['closes_at']
        self.live.settle_due()
        admin.run()
        self.healthy(admin)
        rows = progress()
        sold = rows[rows['상태'] == '낙찰']
        self.assertEqual(sold['낙찰 포인트'].tolist(), ['0 P'])
        self.assertEqual(sold['선수'].tolist(), [lot['riot_id']])
        self.assertTrue((rows.loc[rows['상태'] != '낙찰', '낙찰 포인트'] == '—').all())
        self.assertEqual(len(rows[(rows['선수'] == lot['riot_id']) & (rows['상태'] == '유찰')]), 1)

    def test_approved_member_creator_controls_own_auction_and_other_roles_do_not(self):
        creator = self.core.session(self.tokens[0])
        self.assertEqual((creator['role'], creator['member_status']), ('member', 'APPROVED'))
        # Both events use the real creation and preparation API, with no
        # direct UPDATE of ownership or authorization fields in the fixture.
        self.event_id = self.prepare_event(self.tokens[0])
        other_event = self.prepare_event(self.tokens[1])
        self.assertEqual(self.comp.get_event(self.event_id)['created_by'], creator['id'])
        self.assertEqual(self.comp.get_event(other_event)['created_by'], self.core.session(self.tokens[1])['id'])
        self.live.configure(self.tokens[0], self.event_id, bid_seconds=10)
        self.live.start(self.tokens[0], self.event_id)
        host = self.app(self.tokens[0])
        self.assertTrue(any(caption.value == f"주최자 {creator['display_name']}" for caption in host.caption))
        self.assertFalse(host.button(key='live_pause').disabled)
        self.click(host, '일시 정지')
        self.assertEqual(self.state()['status'], 'PAUSED')
        self.click(host, '경매 재개')
        self.assertEqual(self.state()['status'], 'RUNNING')
        for token in (self.tokens[1], self.tokens[2]):
            viewer = self.app(token)
            self.assertTrue(any(caption.value == f"주최자 {creator['display_name']}" for caption in viewer.caption))
            self.assertFalse(any(button.key in ('live_pause', 'live_resume', 'live_retry') for button in viewer.button))
            for action in (self.live.pause, self.live.resume, self.live.retry_unsold):
                with self.assertRaises(PermissionError):
                    action(token, self.event_id)
        admin = self.app(self.admin)
        self.assertTrue(any(caption.value == f"주최자 {creator['display_name']}" for caption in admin.caption))
        self.assertFalse(admin.button(key='live_pause').disabled)
        for index in range(16):
            self.clock_value = self.state()['current_lot']['closes_at']
            self.live.settle_due()
            if index != 15:
                self.clock_value += 3
                self.live.settle_due()
        host.run()
        self.healthy(host)
        self.assertFalse(host.button(key='live_retry').disabled)
        self.click(host, '일시 정지')
        self.click(host, '유찰 재시작')
        state = self.state()
        self.assertEqual((state['status'], state['queued_count'], state['next_at']), ('WAITING', 16, self.clock_value + 3))

    def test_common_flash_keeps_live_fragment_component_delta_paths_stable(self):
        """Full reruns preserve page ancestors; browser queue races are separate QA."""
        from streamlit.testing.v1.element_tree import Block

        def component_paths(app):
            paths = {}

            def visit(node, path=()):
                if isinstance(node, Block):
                    for index, child in node.children.items():
                        visit(child, path + (index,))
                elif node.type == "bidi_component":
                    kind = json.loads(node.proto.json).get("kind")
                    if kind in ("stage", "sound"):
                        self.assertNotIn(kind, paths)
                        paths[kind] = path

            visit(app._tree)
            self.assertEqual(set(paths), {"stage", "sound"})
            return paths

        self.start()
        admin = self.app(self.admin)
        baseline = component_paths(admin)
        # Login/action notices are consumed outside page.run(). Their presence
        # must not move any ancestor of an already registered live fragment.
        for message in ("로그인했습니다.", "경매 설정을 저장했습니다."):
            admin.session_state["flash"] = message
            admin.run()
            self.healthy(admin)
            self.assertTrue(any(item.value == message for item in admin.success))
            self.assertEqual(component_paths(admin), baseline)
            admin.run()
            self.healthy(admin)
            self.assertFalse(any(item.value == message for item in admin.success))
            self.assertEqual(component_paths(admin), baseline)
        self.click(admin, "일시 정지")
        self.assertEqual(self.state()["status"], "PAUSED")
        self.assertEqual(component_paths(admin), baseline)
        self.click(admin, "경매 재개")
        self.assertEqual(self.state()["status"], "RUNNING")
        self.assertEqual(component_paths(admin), baseline)

    def test_admin_sale_correction_previews_refund_then_confirms_without_changing_bids(self):
        lot = self.start()["current_lot"]
        receipt = self.live.place_bid(self.tokens[0], self.event_id, lot["id"], 40, str(uuid4()))
        self.clock_value = receipt["closes_at"]
        self.live.settle_due()
        admin = self.app(self.admin)
        self.assertFalse(any(button.label == "낙찰 정정" for button in admin.button))
        self.click(admin, "일시 정지")
        before = self.state()
        self.click(admin, "낙찰 정정")
        self.assertTrue(admin.get("dialog"))
        self.widget(admin, "selectbox", "정정 후 팀").set_value(before["teams"][1]["id"])
        self.widget(admin, "number_input", "정정 후 낙찰 포인트").set_value(25)
        self.widget(admin, "text_input", "낙찰 정정 사유").set_value("팀 지정과 낙찰가 입력 정정")
        self.click(admin, "환불·팀 변경 미리보기")
        self.assertFalse(admin.error)
        self.assertEqual(self.state()["teams"], before["teams"])
        self.assertEqual(self.state()["lots"], before["lots"])
        self.assertIn("sale_preview", admin.session_state)
        self.assertFalse(self.widget(admin, "button", "정정 확정").disabled)
        # Changing the proposal requires another preview before mutation.
        self.widget(admin, "number_input", "정정 후 낙찰 포인트").set_value(30).run()
        self.healthy(admin)
        self.assertNotIn("sale_preview", admin.session_state)
        self.assertFalse(any(button.label == "정정 확정" for button in admin.button))
        self.click(admin, "환불·팀 변경 미리보기")
        request_id = admin.session_state["sale_preview"]["request_id"]
        correct_sale = self.live.correct_sale

        def committed_without_response(*args, **kwargs):
            correct_sale(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic-private-connection-detail")

        with patch.object(self.live, "correct_sale", side_effect=committed_without_response):
            self.click(admin, "정정 확정")
        self.assertTrue(any("저장소 응답을 확인하지 못했습니다" in error.value for error in admin.error))
        self.assertFalse(any("synthetic-private" in error.value for error in admin.error))
        self.assertEqual(admin.session_state["sale_preview"]["request_id"], request_id)
        with patch.object(self.live, "correct_sale", wraps=correct_sale) as retry:
            self.click(admin, "정정 확정")
        self.assertEqual(retry.call_args.kwargs["request_id"], request_id)
        after = self.state()
        self.assertFalse(admin.error)
        self.assertEqual(after["status"], "PAUSED")
        self.assertEqual(after["teams"][0]["remaining"], before["teams"][0]["remaining"] + 40)
        self.assertEqual(after["teams"][1]["remaining"], before["teams"][1]["remaining"] - 30)
        corrected = next(item for item in after["lots"] if item["id"] == lot["id"])
        self.assertEqual((corrected["status"], corrected["highest_team_id"], corrected["highest_bid"]),
                         ("SOLD", after["teams"][1]["id"], 30))
        self.assertEqual(after["bids"], before["bids"])
        self.assertEqual(sum(item["type"] == "SALE_CORRECTION" for item in after["events"]), 1)
        self.assertNotIn("sale_dialog_event", admin.session_state)
        self.assertNotIn("sale_preview", admin.session_state)
        self.assertFalse(admin.get("dialog"))

    def test_member_host_and_participant_watch_shared_sale_without_admin_correction(self):
        # An approved member really creates and prepares their own second event.
        self.event_id = self.prepare_event(self.tokens[0])
        self.live.configure(self.tokens[0], self.event_id, bid_seconds=10)
        lot = self.live.start(self.tokens[0], self.event_id)["current_lot"]
        receipt = self.live.place_bid(self.tokens[1], self.event_id, lot["id"], 10, str(uuid4()))
        self.clock_value = receipt["closes_at"]
        self.live.settle_due()
        host = self.app(self.tokens[0])
        self.click(host, "일시 정지")
        self.assertFalse(self.widget(host, "button", "경매 재개").disabled)
        self.assertFalse(any(button.label == "낙찰 정정" for button in host.button))
        password = secrets.token_urlsafe(24)
        self.core.create_account(self.admin, "saleviewer", password, "정정 관전자", role="member", member_id=self.ids[1])
        participant = self.app(self.core.login("saleviewer", password))
        self.assertFalse(any(button.label in ("낙찰 정정", "경매 재개", "일시 정지") for button in participant.button))
        self.assertFalse(any((button.key or "").startswith("live_bid_") for button in participant.button))
        for viewer in (host, participant):
            self.click(viewer, "전체 팀 보기")
            teams = self.presentation(viewer, "overview")[0]["teams"]
            self.assertEqual(len(teams), 4)
            self.assertEqual(len(teams[1]["players"]), 1)
            self.assertEqual(teams[1]["players"][0]["price"], "10P")
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 10)

    def test_completion_leads_to_visible_bracket_preparation(self):
        self.start()
        admin = self.app(self.admin)
        for _ in range(16):
            lot = self.state()["current_lot"]
            team_index = self.ids.index(lot["member_id"]) // 5
            receipt = self.live.place_bid(self.tokens[team_index], self.event_id, lot["id"], 0, str(uuid4()))
            self.clock_value = receipt["closes_at"]
            self.live.settle_due()
            state = self.state()
            if state["status"] != "COMPLETED":
                self.clock_value = state["next_at"]
                self.live.settle_due()
        self.assertEqual(self.state()["status"], "COMPLETED")
        self.assertEqual(self.comp.get_event(self.event_id)["status"], "BRACKET_SETUP")
        admin.run()
        self.healthy(admin)
        notices = [item.value for kind in ("success", "info", "caption", "subheader") for item in getattr(admin, kind)]
        self.assertTrue(any("경매" in value and ("완료" in value or "마쳤" in value) for value in notices), notices)
        self.assertFalse(any((button.key or "").startswith("live_bid_") for button in admin.button))
        self.assertFalse(self.widget(admin, "button", "낙찰 정정").disabled)
        self.assertFalse(self.widget(admin, "button", "대진표 생성").disabled)
        self.click(admin, "대진표 생성")
        event = self.comp.get_event(self.event_id)
        self.assertEqual(event["status"], "BRACKET_SETUP")
        self.assertEqual(len(event["games"]), 6)
        self.assertEqual(self.core.list_games(), [])
        self.assertFalse(any(button.label == "낙찰 정정" for button in admin.button))

    def test_reconnect_restores_saved_order_and_time_only_save_keeps_it(self):
        original = self.app(self.admin)
        self.assertFalse(any(widget.label == "경매 선수 순서" for widget in original.multiselect))
        self.click(original, "경매 설정하기")
        self.click(original, "경매 설정 저장")
        saved_order = [lot["member_id"] for lot in self.state()["lots"]]
        self.assertCountEqual(saved_order, [member_id for member_id in self.ids if member_id not in self.captains])
        reconnected = self.app(self.admin)
        self.assertEqual([lot["member_id"] for lot in self.state()["lots"]], saved_order)
        self.click(reconnected, "경매 설정하기")
        self.widget(reconnected, "selectbox", "선수별 입찰 시간 (초)").set_value(5)
        self.click(reconnected, "경매 설정 저장")
        self.assertEqual(self.state()["bid_seconds"], 5)
        self.assertEqual([lot["member_id"] for lot in self.state()["lots"]], saved_order)
        minimum_time = self.app(self.admin)
        self.click(minimum_time, "경매 설정하기")
        self.assertEqual(self.widget(minimum_time, "selectbox", "선수별 입찰 시간 (초)").value, 5)
        self.assertEqual([lot["member_id"] for lot in self.state()["lots"]], saved_order)
        self.click(minimum_time, "취소")
        self.click(minimum_time, "경매 시작")
        self.assertEqual(self.state()["current_lot"]["member_id"], saved_order[0])

    def test_unsold_explains_failed_settlement_and_does_not_reuse_previous_reason(self):
        state = self.start()
        lot = state["current_lot"]
        receipt = self.live.place_bid(self.tokens[0], self.event_id, lot["id"], 10, str(uuid4()))
        self.core.kick_member(self.admin, lot["member_id"], "참가자 승인 해제 검증")
        self.clock_value = receipt["closes_at"]
        self.live.settle_due()
        captain = self.app(self.tokens[0])
        self.assertTrue(any("낙찰 조건" in item.value for item in captain.warning))
        self.assertTrue(any("회원 승인이 해제" in item.value for item in captain.caption))
        self.assertFalse(any("입찰이 없어" in item.value for item in captain.info))
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 10)
        self.assertEqual(len(self.state()["teams"][0]["players"]), 1)
        self.clock_value = self.state()["next_at"]
        self.live.settle_due()
        self.clock_value = self.state()["current_lot"]["closes_at"]
        self.live.settle_due()
        captain.run()
        self.healthy(captain)
        self.assertTrue(any("입찰이 없어" in item.value for item in captain.info))
        self.assertFalse(any("회원 승인이 해제" in item.value for item in captain.caption))


if __name__ == "__main__":
    unittest.main()
