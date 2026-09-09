"""Terminal bid replies stay terminal when delayed browser retries arrive."""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from roly import auction_ui, browser_session
from roly.auction_commands import context_id
from tests import test_live_auction as fixtures


class AttributeState(dict):
    def __setattr__(self, key, value):
        self[key] = value


class TerminalCommandTests(unittest.TestCase):
    def setUp(self):
        self.event_id, self.lot_id = 12, 34
        self.token, self.db_path = "synthetic-session", "synthetic-database"
        self.context = context_id(self.db_path, self.token, self.event_id)
        self.command = self.new_command()
        self.sequence = 0
        self.state = AttributeState(token=self.token, db_path=self.db_path, unrelated_draft="keep")
        state_patch = patch.object(auction_ui.st, "session_state", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.live = SimpleNamespace(core=SimpleNamespace(db_path=self.db_path),
                                    place_bid=Mock(return_value={"id": 1}),
                                    resolve_bid=Mock(return_value=None))

    def new_command(self, amount=25):
        return {"request_id": str(uuid4()), "lot_id": self.lot_id, "amount": amount}

    def send(self, command, *, context=None):
        self.sequence += 1
        self.state["live_stage_12"] = {"event": {
            "context": context or self.context, "epoch": "synthetic-browser",
            "seq": self.sequence, "sent_ms": self.sequence * 100, "command": command}}
        auction_ui._on_live_event(self.live, self.token, self.event_id, "live_stage_12", self.context)
        return deepcopy(self.state.get("live_command_ack_12"))

    def test_confirmed_absent_retry_never_becomes_a_new_write(self):
        self.live.place_bid.side_effect = [sqlite3.OperationalError("synthetic failure"), {"id": 2}]
        self.assertEqual(self.send(self.command)["status"], "pending")
        rejected = self.send(self.command)
        self.assertEqual(rejected["status"], "rejected")
        # This retry was already in flight before the rejection reached the UI.
        self.assertEqual(self.send(self.command), rejected)
        self.assertEqual(self.live.place_bid.call_count, 1)
        self.assertEqual(self.live.resolve_bid.call_count, 1)
        fresh = self.new_command()
        self.assertEqual(self.send(fresh)["status"], "accepted")
        self.assertEqual(self.live.place_bid.call_count, 2)
        # An even later old retry must survive another request's terminal ACK.
        self.assertEqual(self.send(self.command), rejected)
        self.assertEqual(self.live.place_bid.call_count, 2)

    def test_accepted_replay_uses_original_ack_after_other_commands(self):
        accepted = self.send(self.command)
        self.assertEqual(accepted["status"], "accepted")
        self.send(self.new_command(30))
        self.assertEqual(self.send(self.command), accepted)
        self.assertEqual(self.live.place_bid.call_count, 2)
        self.live.resolve_bid.assert_not_called()

    def test_same_uuid_with_changed_amount_or_lot_is_rejected_without_overwriting_original(self):
        original = self.send(self.command)
        for changed in ({**self.command, "amount": 50}, {**self.command, "lot_id": 35}):
            with self.subTest(changed=changed):
                reply = self.send(changed)
                self.assertEqual(reply["status"], "rejected")
                self.assertEqual(reply["amount"], changed["amount"])
                self.assertEqual(reply["lot_id"], changed["lot_id"])
        self.assertEqual(self.send(self.command), original)
        self.live.place_bid.assert_called_once()

    def test_equivalent_uuid_spellings_share_terminal_fingerprint(self):
        self.live.place_bid.side_effect = ValueError("synthetic rejection")
        self.send(self.command)
        alias = {**self.command, "request_id": self.command["request_id"].replace("-", "").upper()}
        reply = self.send(alias)
        self.assertEqual(reply["status"], "rejected")
        self.assertEqual(reply["request_id"], alias["request_id"])
        self.assertEqual(self.send({**alias, "amount": 50})["status"], "rejected")
        self.live.place_bid.assert_called_once()
        self.assertEqual(len(self.state["live_command_terminal_12"]["acks"]), 1)

    def test_unresolved_command_takes_priority_over_new_uuid_and_poll(self):
        self.live.place_bid.side_effect = [sqlite3.OperationalError("synthetic failure"), {"id": 2}]
        self.live.resolve_bid.side_effect = [sqlite3.OperationalError("synthetic failure"), None]
        pending = self.send(self.command)
        another = self.new_command(50)
        self.assertEqual(self.send(another)["request_id"], pending["request_id"])
        self.assertEqual(self.live.place_bid.call_count, 1)
        rejected = self.send(None)
        self.assertEqual(rejected["request_id"], pending["request_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.live.resolve_bid.call_count, 2)
        self.assertEqual(self.send(another)["status"], "accepted")
        self.assertEqual(self.send(self.command), rejected)
        self.assertEqual(self.live.place_bid.call_count, 2)

    def test_full_ledger_keeps_old_rejections_and_blocks_new_writes_without_eviction(self):
        with patch.object(auction_ui, "TERMINAL_COMMAND_LIMIT", 2):
            self.live.place_bid.side_effect = [ValueError("synthetic rejection"), {"id": 2}]
            rejected = self.send(self.command)
            another = self.new_command(30)
            accepted = self.send(another)
            for command in (self.new_command(40), self.new_command(50)):
                self.assertEqual(self.send(command)["status"], "rejected")
                self.assertEqual(self.send(command)["status"], "rejected")
            self.assertEqual(self.send(self.command), rejected)
            self.assertEqual(self.send(another), accepted)
            self.assertEqual(len(self.state["live_command_terminal_12"]["acks"]), 2)
            self.assertEqual(self.live.place_bid.call_count, 2)
            self.live.resolve_bid.assert_not_called()

    def test_last_available_slot_still_resolves_pending_before_full_ledger_guard(self):
        with patch.object(auction_ui, "TERMINAL_COMMAND_LIMIT", 1):
            self.live.place_bid.side_effect = sqlite3.OperationalError("synthetic failure")
            self.assertEqual(self.send(self.command)["status"], "pending")
            self.assertEqual(self.send(self.new_command())["status"], "rejected")
            self.live.resolve_bid.assert_called_once()
            self.assertEqual(len(self.state["live_command_terminal_12"]["acks"]), 1)
            self.assertEqual(self.send(self.new_command())["status"], "rejected")
            self.live.place_bid.assert_called_once()

    def test_context_change_discards_old_ledger_and_foreign_context_cannot_read_it(self):
        accepted = self.send(self.command)
        self.send(self.command, context="foreign-context")
        self.assertEqual(self.state["live_command_ack_12"], accepted)
        self.live.place_bid.assert_called_once()
        self.token = "new-synthetic-session"
        self.state.token = self.token
        self.context = context_id(self.db_path, self.token, self.event_id)
        reply = self.send(self.command)
        self.assertEqual(reply["context"], self.context)
        self.assertEqual(self.state["live_command_terminal_12"]["context"], self.context)
        self.assertEqual(self.live.place_bid.call_count, 2)

    def test_logout_removes_terminal_and_pending_transport_but_preserves_other_drafts(self):
        self.send(self.command)
        browser_session.clear_login()
        self.assertNotIn("live_command_terminal_12", self.state)
        self.assertNotIn("live_command_ack_12", self.state)
        self.assertNotIn("live_transport_request_12", self.state)
        self.assertIsNone(self.state["token"])
        self.assertEqual(self.state["unrelated_draft"], "keep")


class TerminalCommandDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LiveAuctionTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.LiveAuctionTests.tearDownClass()

    def setUp(self):
        self.fx = fixtures.LiveAuctionTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.lot = self.fx.start(bid_seconds=30)
        self.token = self.fx.tokens[0]
        self.context = context_id(self.fx.core.db_path, self.token, self.fx.event)
        self.command = {"request_id": str(uuid4()), "lot_id": self.lot["id"], "amount": 25}
        self.sequence = 0
        self.state = AttributeState(token=self.token, db_path=self.fx.core.db_path)
        state_patch = patch.object(auction_ui.st, "session_state", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def send(self, command):
        self.sequence += 1
        key = f"live_stage_{self.fx.event}"
        self.state[key] = {"event": {"context": self.context, "epoch": "database-browser",
                                     "seq": self.sequence, "sent_ms": self.sequence, "command": command}}
        auction_ui._on_live_event(self.fx.live, self.token, self.fx.event, key, self.context)
        return deepcopy(self.state[f"live_command_ack_{self.fx.event}"])

    def test_paused_rejection_cannot_accept_delayed_retry_after_resume(self):
        self.fx.live.pause(self.fx.admin, self.fx.event)
        rejected = self.send(self.command)
        self.assertEqual(rejected["status"], "rejected")
        self.fx.live.resume(self.fx.admin, self.fx.event)
        before = self.fx.live.get_state(self.fx.event)
        self.assertEqual(self.send(self.command), rejected)
        unchanged = self.fx.live.get_state(self.fx.event)
        self.assertEqual(unchanged["bids"], [])
        self.assertEqual(unchanged["current_lot"]["closes_at"], before["current_lot"]["closes_at"])
        fresh = {**self.command, "request_id": str(uuid4())}
        self.assertEqual(self.send(fresh)["status"], "accepted")
        self.assertEqual(len(self.fx.live.get_state(self.fx.event)["bids"]), 1)

    def test_accepted_retry_after_reset_never_changes_new_lot_or_refunded_points(self):
        accepted = self.send(self.command)
        self.fx.close()
        preview = self.fx.live.preview_reset(self.fx.admin, self.fx.event)
        self.fx.live.reset(self.fx.admin, self.fx.event, reason="isolated retry verification",
                           request_id=uuid4().hex, expected_fingerprint=preview["fingerprint"])
        self.fx.live.start(self.fx.admin, self.fx.event)
        before = self.fx.live.get_state(self.fx.event)
        self.assertNotEqual(before["current_lot"]["id"], self.command["lot_id"])
        self.assertEqual(self.send(self.command), accepted)
        after = self.fx.live.get_state(self.fx.event)
        self.assertEqual(after["bids"], before["bids"])
        self.assertEqual(after["current_lot"], before["current_lot"])
        self.assertEqual(after["teams"], before["teams"])


if __name__ == "__main__":
    unittest.main()
