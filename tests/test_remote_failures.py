"""Ambiguous storage responses preserve immutable commands and hide details."""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from uuid import uuid4

from roly import auction_ui, ui
from roly.auction_commands import context_id


class RemoteFailureTests(unittest.TestCase):
    def setUp(self):
        self.event_id, self.lot_id = 12, 34
        self.component_key = "live_stage_12"
        self.token = "synthetic-captain-session"
        self.db_path = "synthetic-database"
        self.context = context_id(self.db_path, self.token, self.event_id)
        self.command = {"request_id": str(uuid4()), "lot_id": self.lot_id, "amount": 25}
        self.sequence = 0
        self.state = {"token": self.token, "db_path": self.db_path,
                      "unrelated_form_value": "keep this draft"}
        self.private_driver_detail = (
            "postgresql://synthetic-user:synthetic-password@synthetic-host/postgres "
            "synthetic-private-driver-detail"
        )
        state_patch = patch.object(ui.st, "session_state", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        error_patch = patch.object(ui.st, "error")
        self.error = error_patch.start()
        self.addCleanup(error_patch.stop)
        rerun_patch = patch.object(ui.st, "rerun")
        self.rerun = rerun_patch.start()
        self.addCleanup(rerun_patch.stop)

    def send(self, live, *, command=None, context=None):
        self.sequence += 1
        self.state[self.component_key] = {"event": {
            "context": context or self.context, "epoch": "synthetic-browser", "seq": self.sequence,
            "sent_ms": 100, "command": self.command if command is None else command}}
        auction_ui._on_live_event(live, self.token, self.event_id, self.component_key, self.context)

    def assert_sanitized(self):
        notice = self.state["live_notice_12"]
        self.assertNotIn("synthetic-password", notice["message"])
        self.assertNotIn("synthetic-private-driver-detail", notice["message"])
        self.assertEqual(self.state["unrelated_form_value"], "keep this draft")
        self.error.assert_not_called()
        self.rerun.assert_not_called()

    def test_lost_success_response_confirms_same_uuid_without_another_write(self):
        accepted = set()

        def accepted_then_response_lost(token, event_id, lot_id, amount, request_id):
            accepted.add(request_id)
            raise sqlite3.OperationalError(self.private_driver_detail)

        live = SimpleNamespace(
            core=SimpleNamespace(db_path=self.db_path),
            place_bid=Mock(side_effect=accepted_then_response_lost),
            resolve_bid=Mock(side_effect=[sqlite3.OperationalError(self.private_driver_detail),
                                          {"id": 1, "replayed": True}]),
        )
        self.send(live)
        pending = {"context": self.context, "command": self.command}
        self.assertEqual(self.state["live_command_pending_12"], pending)
        self.assertEqual(self.state["live_command_ack_12"]["status"], "pending")
        self.assert_sanitized()
        # Even another browser command cannot replace an uncertain receipt.
        different = {**self.command, "request_id": str(uuid4()), "amount": 50}
        self.send(live, command=different)
        self.assertEqual(self.state["live_command_pending_12"], pending)
        self.assertEqual(self.state["live_command_ack_12"]["request_id"], self.command["request_id"])
        self.assert_sanitized()
        self.send(live)
        self.assertNotIn("live_command_pending_12", self.state)
        self.assertEqual(self.state["live_command_ack_12"]["status"], "accepted")
        expected = call(self.token, self.event_id, self.lot_id, 25, self.command["request_id"])
        self.assertEqual(live.place_bid.call_args_list, [expected])
        self.assertEqual(live.resolve_bid.call_args_list, [expected, expected])
        self.assertEqual(accepted, {self.command["request_id"]})
        self.assertTrue(self.state["live_notice_12"]["success"])
        self.assert_sanitized()

    def test_success_acknowledges_exact_browser_uuid_and_amount_after_service_returns(self):
        def accept(token, event_id, lot_id, amount, request_id):
            self.assertNotIn("live_command_ack_12", self.state)
            return {"id": 2, "replayed": False}

        live = SimpleNamespace(core=SimpleNamespace(db_path=self.db_path),
                               place_bid=Mock(side_effect=accept), resolve_bid=Mock())
        self.send(live)
        live.place_bid.assert_called_once_with(
            self.token, self.event_id, self.lot_id, 25, self.command["request_id"])
        live.resolve_bid.assert_not_called()
        self.assertNotIn("live_command_pending_12", self.state)
        ack = self.state["live_command_ack_12"]
        self.assertEqual({key: ack[key] for key in self.command}, self.command)
        self.assertEqual(ack["status"], "accepted")
        self.assertEqual(ack["context"], self.context)
        self.assert_sanitized()

    def test_failed_write_is_confirmed_absent_before_accepting_a_new_intent(self):
        live = SimpleNamespace(core=SimpleNamespace(db_path=self.db_path),
            place_bid=Mock(side_effect=[sqlite3.OperationalError(self.private_driver_detail), {"id": 2}]),
            resolve_bid=Mock(return_value=None))
        self.send(live)
        self.send(live)
        self.assertEqual(live.place_bid.call_count, 1)
        self.assertEqual(self.state["live_command_ack_12"]["status"], "rejected")
        self.assertNotIn("live_command_pending_12", self.state)
        fresh = {**self.command, "request_id": str(uuid4())}
        self.send(live, command=fresh)
        self.assertEqual(live.place_bid.call_count, 2)
        self.assertEqual(self.state["live_command_ack_12"]["request_id"], fresh["request_id"])
        self.assertEqual(self.state["live_command_ack_12"]["status"], "accepted")
        self.assert_sanitized()

    def test_foreign_context_and_changed_login_never_call_bid_service(self):
        live = SimpleNamespace(core=SimpleNamespace(db_path=self.db_path),
                               place_bid=Mock(), resolve_bid=Mock())
        self.send(live, context="another-event-context")
        self.assertNotIn("live_command_ack_12", self.state)
        self.assertNotIn("live_transport_request_12", self.state)
        self.state["token"] = "new-login-session"
        self.send(live)
        self.assertNotIn("live_command_ack_12", self.state)
        live.place_bid.assert_not_called()
        live.resolve_bid.assert_not_called()
        self.assert_sanitized()

    def test_perform_storage_failure_preserves_form_state_and_sanitizes_error(self):
        before = deepcopy(self.state)
        action = Mock(side_effect=sqlite3.OperationalError(self.private_driver_detail))
        result = ui.perform(action, "This success message must not appear")
        self.assertIsNone(result)
        action.assert_called_once_with()
        self.assertEqual(self.state, before)
        self.assertNotIn("flash", self.state)
        self.error.assert_called_once_with(
            "저장소 응답을 확인하지 못했습니다. 현재 처리 내역을 확인한 뒤 다시 시도해 주세요.",
            icon=":material/error:",
        )
        self.assertNotIn("synthetic-password", str(self.error.call_args))
        self.assertNotIn("synthetic-private-driver-detail", str(self.error.call_args))
        self.rerun.assert_not_called()


if __name__ == "__main__":
    unittest.main()
