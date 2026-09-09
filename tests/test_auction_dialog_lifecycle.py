"""A timed auction update must never send another dialog-open instruction."""
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from tests import test_live_auction_ui


class AuctionDialogLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_live_auction_ui.LiveAuctionUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("Unexpected Riot request")))

    def assert_page_owned_dialog(self, app, title):
        self.fixture.healthy(app)
        dialogs = app.get("dialog")
        self.assertEqual(len(dialogs), 1)
        self.assertEqual(dialogs[0].proto.dialog.title, title)
        metadata = app.session_state._state._new_widget_state.widget_metadata[dialogs[0].proto.id]
        # The native close event must target a full-page rerun, not the
        # half-second auction poll that can already have an update in flight.
        self.assertIsNone(metadata.fragment_id)

    def dismiss(self, app):
        dialog = app.get("dialog")[0]
        states = app._tree.get_widget_states()
        trigger = next((item for item in states.widgets if item.id == dialog.proto.id), None)
        if trigger is None:
            trigger = states.widgets.add()
            trigger.id = dialog.proto.id
        trigger.trigger_value = True
        app._run(states)
        self.fixture.healthy(app)
        self.assertFalse(app.get("dialog"))

    def assert_poll_does_not_open_dialog(self, flag):
        f = self.fixture
        poll = AppTest.from_string("""
import streamlit as st
from roly.auction_ui import _render_live_auction
_render_live_auction(st.session_state.event_id)
""", default_timeout=30)
        poll.session_state.db_path = f.core.db_path
        poll.session_state.token = f.admin
        poll.session_state.event_id = f.event_id
        poll.session_state[flag] = f.event_id
        with patch("roly.auction_ui.team_overview", side_effect=AssertionError("Poll reopened overview")), \
                patch("roly.auction_ui.auction_reset_dialog", side_effect=AssertionError("Poll reopened reset")), \
                patch("roly.auction_ui.sale_correction_dialog", side_effect=AssertionError("Poll reopened correction")):
            for _ in range(2):
                poll.run()
                f.healthy(poll)
                self.assertFalse(poll.get("dialog"))
                self.assertEqual(poll.session_state[flag], f.event_id)

    def test_overview_close_survives_polling_and_next_bid_is_accepted_once(self):
        f = self.fixture
        f.start()
        app = f.app(f.tokens[0])
        f.click(app, "전체 팀 보기")
        self.assert_page_owned_dialog(app, "전체 경매 현황")
        self.assert_poll_does_not_open_dialog("live_overview_event")
        self.dismiss(app)
        self.assertNotIn("live_overview_event", app.session_state)
        for _ in range(2):
            app.run()
            f.healthy(app)
            self.assertFalse(app.get("dialog"))
        f.click(app, "+10")
        f.click(app, "입찰하기")
        self.assertEqual([row["amount"] for row in f.state()["bids"]], [10])
        f.click(app, "전체 팀 보기")
        self.assert_page_owned_dialog(app, "전체 경매 현황")

    def test_reset_close_survives_polling_without_initializing_auction(self):
        f = self.fixture
        f.start()
        app = f.app(f.admin)
        before = f.state()
        f.click(app, "경매 초기화")
        self.assert_page_owned_dialog(app, "경매 초기화")
        self.assert_poll_does_not_open_dialog("live_reset_event")
        self.dismiss(app)
        self.assertNotIn("live_reset_event", app.session_state)
        self.assertNotIn("live_reset_review", app.session_state)
        self.assertEqual(f.state()["events"], before["events"])
        f.click(app, "일시 정지")
        self.assertEqual(f.state()["status"], "PAUSED")
        self.assertFalse(app.get("dialog"))

    def test_sale_correction_close_survives_polling_without_changing_sale(self):
        f = self.fixture
        lot = f.start()["current_lot"]
        receipt = f.live.place_bid(f.tokens[0], f.event_id, lot["id"], 10, str(uuid4()))
        f.clock_value = receipt["closes_at"]
        f.live.settle_due()
        f.live.pause(f.admin, f.event_id)
        app = f.app(f.admin)
        before = f.state()
        f.click(app, "낙찰 정정")
        self.assert_page_owned_dialog(app, "낙찰 정정")
        self.assert_poll_does_not_open_dialog("sale_dialog_event")
        self.dismiss(app)
        self.assertNotIn("sale_dialog_event", app.session_state)
        self.assertNotIn("sale_preview", app.session_state)
        f.click(app, "경매 재개")
        after = f.state()
        self.assertEqual(after["status"], "WAITING")
        self.assertEqual(after["lots"], before["lots"])
        self.assertEqual(after["bids"], before["bids"])
        self.assertFalse(app.get("dialog"))


if __name__ == "__main__":
    unittest.main()
