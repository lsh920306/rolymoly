"""Exercise CCv2 immutable envelopes against isolated application services.

The protocol driver models local edits. Separate Node tests execute the actual
browser draft, clock and delivery implementation.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_live_auction_ui
from tests.live_panel_client import panel_client


class BidDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_live_auction_ui.LiveAuctionUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No real API settings")))
        self.fixture.start()
        self.app = self.fixture.app(self.fixture.tokens[0])
        self.client = panel_client(self.app)
        self.lot_id = self.fixture.state()["current_lot"]["id"]

    def amount(self):
        return self.fixture.widget(self.app, "number_input", "입찰할 포인트")

    def receipt(self):
        return self.client.sync()["transport"]["ack"]

    def test_local_increment_and_queued_poll_keep_draft_until_explicit_command(self):
        before_frame = self.client.sync()["transport"]["frame_id"]
        request_id = self.client.request_id
        self.fixture.click(self.app, "+10")
        self.assertEqual(self.amount().value, 10)
        self.assertEqual(self.client.sync()["transport"]["frame_id"], before_frame)
        self.client.poll()
        self.fixture.healthy(self.app)
        self.assertEqual(self.amount().value, 10)
        self.assertEqual(self.fixture.state()["bids"], [])
        self.fixture.click(self.app, "입찰하기")
        bids = self.fixture.state()["bids"]
        self.assertEqual([(item["amount"], item["request_id"]) for item in bids], [(10, request_id)])
        self.assertEqual(self.receipt()["status"], "accepted")
        self.assertNotEqual(self.client.request_id, request_id)

    def test_direct_entry_reset_and_foreign_context_preserve_current_draft(self):
        self.amount().set_value(25).run()
        self.client.poll()
        self.assertEqual(self.amount().value, 25)
        self.fixture.click(self.app, "+10")
        self.client.poll()
        self.assertEqual(self.amount().value, 35)
        self.fixture.click(self.app, "금액 초기화")
        self.assertEqual(self.amount().value, 0)
        self.amount().set_value(15).run()
        self.client.send(envelope={"context": "different-login-context", "epoch": self.client.epoch,
            "seq": 99, "sent_ms": 100, "command": {"request_id": str(uuid4()),
            "lot_id": self.lot_id, "amount": 100}})
        self.fixture.healthy(self.app)
        self.assertEqual(self.amount().value, 15)
        self.assertEqual(self.fixture.state()["bids"], [])
        self.fixture.click(self.app, "입찰하기")
        self.assertEqual([bid["amount"] for bid in self.fixture.state()["bids"]], [15])
        self.assertEqual(self.receipt()["status"], "accepted")

    def test_repeated_command_has_one_receipt_and_changed_amount_cannot_reuse_uuid(self):
        self.amount().set_value(25).run()
        command = {"request_id": self.client.request_id, "lot_id": self.lot_id, "amount": 25}
        self.client.send(command)
        before = deepcopy(self.fixture.state())
        self.client.send(command)
        after = self.fixture.state()
        self.assertEqual(len(after["bids"]), 1)
        self.assertEqual(after["current_lot"]["closes_at"], before["current_lot"]["closes_at"])
        self.assertEqual([team["remaining"] for team in after["teams"]],
                         [team["remaining"] for team in before["teams"]])
        self.assertEqual(self.receipt()["request_id"], command["request_id"])
        self.assertEqual(self.receipt()["status"], "accepted")
        self.client.send({**command, "amount": 50})
        self.assertEqual(self.receipt()["status"], "rejected")
        self.assertEqual([bid["amount"] for bid in self.fixture.state()["bids"]], [25])

    def test_zero_point_command_remains_valid_and_duplicate_delivery_is_idempotent(self):
        self.fixture.click(self.app, "+10")
        self.fixture.click(self.app, "금액 초기화")
        self.client.poll()
        self.assertEqual(self.amount().value, 0)
        self.assertEqual(self.fixture.state()["bids"], [])
        command = {"request_id": self.client.request_id, "lot_id": self.lot_id, "amount": 0}
        self.fixture.click(self.app, "입찰하기")
        self.client.send(command)
        self.assertEqual(self.receipt()["status"], "accepted")
        self.assertEqual([(bid["amount"], bid["request_id"]) for bid in self.fixture.state()["bids"]],
                         [(0, command["request_id"])])


if __name__ == "__main__":
    unittest.main()
