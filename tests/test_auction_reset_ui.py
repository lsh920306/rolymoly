"""Reset confirmation and recovery use real isolated auction transactions."""
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4
from streamlit.testing.v1 import AppTest

from tests import test_live_auction_ui


class AuctionResetUITests(unittest.TestCase):
    def setUp(self):
        self.f = test_live_auction_ui.LiveAuctionUITests(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.start()

    def sale(self):
        f = self.f
        lot = f.state()["current_lot"]
        receipt = f.live.place_bid(f.tokens[0], f.event_id, lot["id"], 40, str(uuid4()))
        f.clock_value = receipt["closes_at"]
        f.live.settle_due()
        return lot

    def dialog(self):
        f = self.f
        app = AppTest.from_string('''
import streamlit as st
from roly.auction_ui import auction_reset_dialog
if st.session_state.get("live_reset_event"):
    auction_reset_dialog(st.session_state.live_reset_event, st.session_state.captured_token)
''', default_timeout=30)
        for key, value in {"db_path": f.core.db_path, "token": f.admin,
                           "captured_token": f.admin, "live_reset_event": f.event_id}.items():
            app.session_state[key] = value
        app.run()
        f.healthy(app)
        return app

    def test_open_and_cancel_are_read_only_and_captain_has_no_reset_action(self):
        f = self.f
        self.sale()
        app = f.app(f.admin)
        before = f.state()
        f.click(app, "경매 초기화")
        self.assertTrue(app.get("dialog"))
        self.assertEqual(f.state(), before)
        f.click(app, "취소")
        self.assertEqual(f.state(), before)
        self.assertNotIn("live_reset_review", app.session_state)
        captain = f.app(f.tokens[0])
        self.assertFalse(any(button.label == "경매 초기화" for button in captain.button))

    def test_confirm_refunds_and_returns_to_settings_and_start(self):
        f = self.f
        old_lot = self.sale()
        app = f.app(f.admin)
        notice_key = f"live_notice_{f.event_id}"
        other_notice_key = f"live_notice_{f.event_id + 1}"
        stale_notice = {"success": False, "message": "진행 중인 경매만 일시정지할 수 있습니다.",
                        "lot_id": None, "token": f.admin}
        app.session_state[notice_key] = stale_notice.copy()
        app.session_state[other_notice_key] = stale_notice.copy()
        app.run()
        self.assertTrue(any(item.value == stale_notice["message"] for item in app.error))
        before = f.state()
        f.click(app, "경매 초기화")
        f.click(app, "경매 초기화 확정")
        after = f.state()
        self.assertEqual((after["status"], after["event"]["status"]), ("READY", "AUCTION_READY"))
        self.assertNotIn(notice_key, app.session_state)
        self.assertEqual(app.session_state[other_notice_key], stale_notice)
        self.assertFalse(app.error)
        self.assertEqual(after["teams"][0]["remaining"], before["teams"][0]["remaining"] + 40)
        self.assertEqual(after["bids"], before["bids"])
        self.assertEqual(next(lot for lot in after["lots"] if lot["id"] == old_lot["id"])["status"], "CANCELLED")
        self.assertFalse(f.widget(app, "button", "경매 시작").disabled)
        f.click(app, "경매 시작")
        self.assertEqual(f.state()["status"], "RUNNING")
        self.assertFalse(app.error)
        self.assertNotIn(notice_key, app.session_state)
        self.assertNotEqual(f.state()["current_lot"]["id"], old_lot["id"])
        self.assertTrue(any(item.label == "초기화·취소 전 입찰 기록" for item in app.expander))
        self.assertEqual(f.presentation(app, "history")[0]["bids"], [])
        self.assertEqual(f.presentation(app, "sound")[0]["bid_id"], None)

    def test_bid_after_preview_rejects_old_confirmation_then_reload_can_reset(self):
        f = self.f
        app = f.app(f.admin)
        f.click(app, "경매 초기화")
        old_request = app.session_state["live_reset_review"]["request_id"]
        lot = f.state()["current_lot"]
        f.live.place_bid(f.tokens[0], f.event_id, lot["id"], 15, str(uuid4()))
        before = f.state()
        f.click(app, "경매 초기화 확정")
        self.assertTrue(app.error)
        self.assertEqual(f.state(), before)
        f.click(app, "최신 초기화 내용 확인")
        self.assertNotEqual(app.session_state["live_reset_review"]["request_id"], old_request)
        f.click(app, "경매 초기화 확정")
        self.assertEqual(f.state()["status"], "READY")

    def test_lost_reset_response_retries_same_request_without_second_reset(self):
        f = self.f
        self.sale()
        app = self.dialog()
        request_id = app.session_state["live_reset_review"]["request_id"]
        reset = f.live.reset

        def committed_without_response(*args, **kwargs):
            reset(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic-private-connection-detail")

        with patch.object(f.live, "reset", side_effect=committed_without_response):
            f.click(app, "경매 초기화 확정")
        self.assertEqual(f.state()["status"], "READY")
        # Invoke the actual dialog with captured arguments, as a dialog-only
        # rerun does, even though the outer page would now render READY.
        with patch.object(f.live, "reset", wraps=reset) as replay:
            f.click(app, "경매 초기화 확정")
            self.assertEqual(replay.call_args.kwargs["request_id"], request_id)
        self.assertEqual(sum(item["type"] == "RESET" for item in f.state()["events"]), 1)
        self.assertFalse(any("synthetic-private" in error.value for error in app.error))

    def test_captured_confirmation_cannot_reset_after_account_switch(self):
        f = self.f
        app = self.dialog()
        f.widget(app, "button", "경매 초기화 확정").click()
        payload = app._tree.get_widget_states()
        before = f.state()
        app.session_state["token"] = f.tokens[0]
        app._run(payload)
        f.healthy(app)
        self.assertTrue(app.warning)
        self.assertEqual(f.state(), before)
        self.assertFalse(any(button.label == "경매 초기화 확정" for button in app.button))

    def test_initial_dialog_database_failures_are_sanitized_and_keep_state(self):
        f = self.f
        for target in ("service", "session", "event"):
            with self.subTest(target=target):
                app = self.dialog()
                before = f.state()
                if target == "service":
                    guard = patch("roly.auction_ui.live_service", side_effect=sqlite3.OperationalError("synthetic-private-db-host-password"))
                else:
                    owner, name = (f.core, "session") if target == "session" else (f.comp, "get_event")
                    guard = patch.object(owner, name, side_effect=sqlite3.OperationalError("synthetic-private-db-host-password"))
                with guard:
                    app.run()
                f.healthy(app)
                self.assertTrue(app.error)
                self.assertTrue(all("synthetic-private" not in item.value for item in app.error))
                self.assertEqual(f.state(), before)
                self.assertFalse(any(button.label == "경매 초기화 확정" for button in app.button))

    def test_other_client_observes_reset_and_loses_old_bid_controls(self):
        f = self.f
        captain = f.app(f.tokens[0])
        lot = f.state()["current_lot"]
        f.click(captain, "+10")
        preview = f.live.preview_reset(f.admin, f.event_id)
        f.live.reset(f.admin, f.event_id, reason="외부 주최자 초기화", request_id=str(uuid4()), expected_fingerprint=preview["fingerprint"])
        f.click(captain, "입찰하기")
        self.assertEqual(f.state()["status"], "READY")
        self.assertFalse(any(bid["lot_id"] == lot["id"] for bid in f.state()["bids"]))
        self.assertEqual(f.presentation(captain, "stage"), [])
        self.assertEqual(len(f.presentation(captain, "auction_sync")), 1)


if __name__ == "__main__":
    unittest.main()
