"""Replay queued browser widget snapshots without touching operating storage."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import UUID

from tests import test_live_auction_ui


class BidDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_live_auction_ui.LiveAuctionUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No real API settings")))
        self.fixture.start()
        self.app = self.fixture.app(self.fixture.tokens[0])
        self.lot_id = self.fixture.state()["current_lot"]["id"]
        self.request_key = f"live_request_{self.lot_id}"

    def amount(self):
        return self.fixture.widget(self.app, "number_input", "입찰할 포인트")

    def bid(self):
        return self.fixture.widget(self.app, "button", "입찰하기")

    def replay(self, snapshot, button_id=None):
        incoming = deepcopy(snapshot)
        if button_id:
            item = next((row for row in incoming.widgets if row.id == button_id), None)
            if item is None:
                item = incoming.widgets.add(id=button_id)
            item.trigger_value = True
        self.app._run(incoming)
        self.fixture.healthy(self.app)

    def test_increment_survives_queued_old_snapshot_and_only_current_bid_submits(self):
        old_snapshot = deepcopy(self.app._tree.get_widget_states())
        old_bid = self.bid().proto.id
        original_request = self.app.session_state[self.request_key]
        old_input_key = self.amount().key
        self.fixture.click(self.app, "+10")
        self.assertEqual(self.amount().value, 10)
        self.assertNotEqual(self.amount().key, old_input_key)
        self.replay(old_snapshot)
        self.replay(old_snapshot, old_bid)
        self.assertEqual(self.amount().value, 10)
        self.assertEqual(self.fixture.state()["bids"], [])
        self.assertEqual(self.app.session_state[self.request_key], original_request)
        self.fixture.click(self.app, "입찰하기")
        bids = self.fixture.state()["bids"]
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0]["amount"], 10)
        self.assertEqual(UUID(bids[0]["request_id"]), UUID(original_request))
        self.assertNotEqual(self.app.session_state[self.request_key], original_request)

    def test_direct_entry_increment_and_reset_preserve_latest_user_intent(self):
        old_snapshot = deepcopy(self.app._tree.get_widget_states())
        self.amount().set_value(25).run()
        self.fixture.healthy(self.app)
        typed_snapshot = deepcopy(self.app._tree.get_widget_states())
        self.replay(old_snapshot)
        self.assertEqual(self.amount().value, 25)
        self.fixture.click(self.app, "+10")
        self.replay(typed_snapshot)
        self.assertEqual(self.amount().value, 35)
        self.fixture.click(self.app, "금액 초기화")
        self.replay(typed_snapshot)
        self.assertEqual(self.amount().value, 0)
        self.amount().set_value(15).run()
        self.fixture.healthy(self.app)
        self.replay(old_snapshot)
        self.assertEqual(self.amount().value, 15)
        self.fixture.click(self.app, "입찰하기")
        self.assertEqual([bid["amount"] for bid in self.fixture.state()["bids"]], [15])

    def test_simultaneous_edit_and_old_submit_rejects_screen_amount_mismatch(self):
        request = self.app.session_state[self.request_key]
        self.amount().set_value(25)
        self.bid().click().run()
        self.fixture.healthy(self.app)
        self.assertEqual(self.amount().value, 25)
        self.assertEqual(self.fixture.state()["bids"], [])
        self.assertEqual(self.app.session_state[self.request_key], request)
        self.assertTrue(any("화면의 금액과 달라" in error.value for error in self.app.error))
        self.fixture.click(self.app, "입찰하기")
        self.assertEqual([bid["amount"] for bid in self.fixture.state()["bids"]], [25])

    def test_return_to_same_amount_does_not_revive_old_zero_bid(self):
        original = deepcopy(self.app._tree.get_widget_states())
        old_bid = self.bid().proto.id
        request = self.app.session_state[self.request_key]
        self.fixture.click(self.app, "+10")
        self.fixture.click(self.app, "금액 초기화")
        self.assertEqual(self.amount().value, 0)
        self.assertNotEqual(self.bid().proto.id, old_bid)
        self.replay(original, old_bid)
        self.assertEqual(self.fixture.state()["bids"], [])
        self.assertEqual(self.app.session_state[self.request_key], request)
        self.fixture.click(self.app, "입찰하기")
        self.assertEqual([bid["amount"] for bid in self.fixture.state()["bids"]], [0])


if __name__ == "__main__":
    unittest.main()
