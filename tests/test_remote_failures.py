"""Ambiguous storage responses preserve retry identity and hide driver details."""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from roly import auction_ui, ui


class RemoteFailureTests(unittest.TestCase):
    def setUp(self):
        self.event_id, self.lot_id = 12, 34
        self.amount_key = "live_amount_34"
        self.request_key = "live_request_34"
        self.original_request = "a" * 32
        self.token = "synthetic-captain-session"
        self.state = {
            "token": self.token,
            self.amount_key: 25,
            self.request_key: self.original_request,
            "unrelated_form_value": "keep this draft",
        }
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

    def submit(self, live):
        auction_ui._submit_bid(live, self.token, self.event_id, self.lot_id,
                               self.amount_key, self.request_key, 25)

    def test_lost_success_response_retries_same_uuid_before_rotating(self):
        accepted = set()

        def accepted_then_response_lost(token, event_id, lot_id, amount, request_id):
            if request_id not in accepted:
                accepted.add(request_id)
                # The server accepted the bid, but its response did not arrive.
                raise sqlite3.OperationalError(self.private_driver_detail)
            return {"id": 1, "replayed": True}

        live = SimpleNamespace(
            core=SimpleNamespace(db_path="synthetic-database"),
            place_bid=Mock(side_effect=accepted_then_response_lost),
            resolve_bid=Mock(side_effect=[sqlite3.OperationalError(self.private_driver_detail),
                                          {"id": 1, "replayed": True}]),
        )
        with patch.object(auction_ui, "uuid4", return_value=SimpleNamespace(hex="b" * 32)) as uuid4:
            self.submit(live)
            self.assertEqual(self.state[self.request_key], self.original_request)
            self.assertEqual(self.state[self.amount_key], 25)
            self.assertEqual(self.state["unrelated_form_value"], "keep this draft")
            notice = deepcopy(self.state["live_notice_12"])
            self.assertFalse(notice["success"])
            self.assertEqual(notice["lot_id"], self.lot_id)
            self.assertEqual(notice["token"], self.token)
            self.assertEqual(
                notice["message"],
                "입찰 처리 결과를 확인하지 못했습니다. 현재 최고가를 확인한 뒤 다시 시도해 주세요.",
            )
            self.assertNotIn("synthetic-password", notice["message"])
            self.assertNotIn("synthetic-private-driver-detail", notice["message"])
            uuid4.assert_not_called()

            pending = deepcopy(self.state["live_pending_12"])
            self.assertEqual(pending, {
                "token": self.token, "db_path": "synthetic-database",
                "event_id": self.event_id, "lot_id": self.lot_id, "amount": 25,
                "request_id": self.original_request, "request_key": self.request_key,
            })
            # An extra callback while receipt lookup is unavailable must not
            # submit another bid or change the immutable original request.
            self.submit(live)
            self.assertEqual(self.state["live_pending_12"], pending)
            self.assertEqual(self.state[self.request_key], self.original_request)
            self.assertEqual(live.place_bid.call_count, 1)
            self.assertEqual(self.state["unrelated_form_value"], "keep this draft")
            self.assertNotIn("synthetic-password", self.state["live_notice_12"]["message"])
            self.assertNotIn("synthetic-private-driver-detail", self.state["live_notice_12"]["message"])
            uuid4.assert_not_called()

            # The live fragment resolves the old receipt without bidding again.
            self.assertTrue(auction_ui._resolve_pending_bid(live, self.token, self.event_id))
            self.assertNotIn("live_pending_12", self.state)
            self.assertEqual(self.state[self.request_key], "b" * 32)
            uuid4.assert_called_once_with()
        expected = call(self.token, self.event_id, self.lot_id, 25, self.original_request)
        self.assertEqual(live.place_bid.call_args_list, [expected])
        self.assertEqual(live.resolve_bid.call_args_list, [expected, expected])
        self.assertEqual(len(accepted), 1)
        self.assertTrue(self.state["live_notice_12"]["success"])
        self.error.assert_not_called()
        self.rerun.assert_not_called()

    def test_successful_bid_rotates_uuid_only_after_service_returns(self):
        def accept(token, event_id, lot_id, amount, request_id):
            self.assertEqual(self.state[self.request_key], self.original_request)
            return {"id": 2, "replayed": False}

        live = SimpleNamespace(core=SimpleNamespace(db_path="synthetic-database"),
                               place_bid=Mock(side_effect=accept), resolve_bid=Mock())
        with patch.object(auction_ui, "uuid4", return_value=SimpleNamespace(hex="c" * 32)) as uuid4:
            self.submit(live)
        live.place_bid.assert_called_once_with(
            self.token, self.event_id, self.lot_id, 25, self.original_request
        )
        uuid4.assert_called_once_with()
        live.resolve_bid.assert_not_called()
        self.assertNotIn("live_pending_12", self.state)
        self.assertEqual(self.state[self.request_key], "c" * 32)
        self.assertEqual(self.state[self.amount_key], 25)
        self.assertEqual(self.state["live_notice_12"], {
            "success": True, "message": "입찰을 접수했습니다.",
            "lot_id": self.lot_id, "token": self.token,
        })
        self.error.assert_not_called()
        self.rerun.assert_not_called()

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
