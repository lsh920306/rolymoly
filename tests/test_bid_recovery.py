"""Uncertain bid acknowledgements must not block the next valid bid."""
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_live_auction_ui as live_fixture


class BidRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = live_fixture.LiveAuctionUITests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.fx.start()

    def test_committed_response_loss_resolves_before_a_higher_bid(self):
        fx = self.fx
        app = fx.app(fx.tokens[0])
        fx.click(app, "+10")
        lot = fx.state()["current_lot"]
        request_key = f"live_request_{lot['id']}"
        request = app.session_state[request_key]
        submit = fx.live.place_bid

        def commit_then_disconnect(*args, **kwargs):
            submit(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic private driver detail")

        with patch.object(fx.live, "place_bid", side_effect=commit_then_disconnect), patch.object(
            fx.live, "resolve_bid", side_effect=sqlite3.OperationalError("still disconnected")
        ):
            fx.click(app, "입찰하기")
            self.assertIn(f"live_pending_{fx.event_id}", app.session_state)
            self.assertEqual(app.session_state[request_key], request)
            self.assertTrue(fx.widget(app, "button", "입찰하기").disabled)
            self.assertNotIn("synthetic", " ".join(e.value for e in app.error))

        fx.live.place_bid(fx.tokens[1], fx.event_id, lot["id"], 20, str(uuid4()))
        before = deepcopy(fx.state())
        app.run()
        fx.healthy(app)
        self.assertNotIn(f"live_pending_{fx.event_id}", app.session_state)
        self.assertNotEqual(app.session_state[request_key], request)
        after = fx.state()
        self.assertEqual(before["bids"], after["bids"])
        self.assertEqual(before["current_lot"]["closes_at"], after["current_lot"]["closes_at"])
        fx.click(app, "+10")
        self.assertEqual(fx.widget(app, "number_input", "입찰할 포인트").value, 30)
        fx.click(app, "입찰하기")
        self.assertEqual(fx.state()["current_lot"]["highest_bid"], 30)
        self.assertEqual(len(fx.state()["bids"]), 3)

    def test_uncommitted_failure_is_confirmed_without_automatically_bidding(self):
        fx = self.fx
        app = fx.app(fx.tokens[0])
        fx.click(app, "+10")
        before = fx.state()
        with patch.object(fx.live, "place_bid", side_effect=sqlite3.OperationalError("before write")), patch.object(
            fx.live, "resolve_bid", side_effect=sqlite3.OperationalError("offline")
        ):
            fx.click(app, "입찰하기")
        app.run()
        fx.healthy(app)
        self.assertNotIn(f"live_pending_{fx.event_id}", app.session_state)
        self.assertTrue(any("접수되지 않았습니다" in e.value for e in app.error))
        self.assertEqual(fx.state()["bids"], [])
        self.assertEqual(fx.state()["current_lot"]["closes_at"], before["current_lot"]["closes_at"])
        fx.click(app, "입찰하기")
        self.assertEqual(len(fx.state()["bids"]), 1)

    def test_receipt_resolution_after_settlement_is_read_only_and_account_bound(self):
        fx = self.fx
        lot = fx.state()["current_lot"]
        request = str(uuid4())
        receipt = fx.live.place_bid(fx.tokens[0], fx.event_id, lot["id"], 10, request)
        fx.clock_value = receipt["closes_at"]
        fx.live.settle_due()
        before = fx.state()
        for _ in range(2):
            resolved = fx.live.resolve_bid(fx.tokens[0], fx.event_id, lot["id"], 10, request)
            self.assertEqual(resolved["id"], receipt["id"])
        with self.assertRaises(ValueError):
            fx.live.resolve_bid(fx.tokens[1], fx.event_id, lot["id"], 10, request)
        with self.assertRaises(ValueError):
            fx.live.resolve_bid(fx.tokens[0], fx.event_id, lot["id"], 15, request)
        self.assertEqual(fx.state(), before)

    def test_auction_view_uses_one_connection_and_constant_read_count(self):
        fx = self.fx
        reads, connections = [], []
        connect = fx.core.connect

        def trace(sql):
            reads.append(sql.split(None, 1)[0])
            if "FROM competition_players WHERE event_id" in sql and "participation_status='SELECTED'" in sql:
                # A slow final list fetch must reduce the displayed remaining
                # time instead of becoming extra seconds on every refresh.
                fx.clock_value += 0.8

        def count_connect():
            db = connect()
            connections.append(db)
            db.set_trace_callback(trace)
            return db

        with patch.object(fx.core, "connect", side_effect=count_connect):
            actor, state = fx.live.get_view(fx.tokens[0], fx.event_id)
        self.assertEqual(len(connections), 1)
        self.assertEqual(reads.count("SELECT"), 7)
        self.assertEqual(actor["member_id"], fx.captains[0])
        self.assertEqual(len(state["event"]["players"]), 20)
        self.assertEqual(state["event"]["status"], "AUCTION")
        self.assertFalse(state["event"]["has_games"])
        self.assertEqual(state["server_now"], fx.clock_value)
        self.assertAlmostEqual(state["current_lot"]["remaining_seconds"],
                               state["current_lot"]["closes_at"] - fx.clock_value)
        with patch.object(fx.core, "get_member", side_effect=AssertionError("Bidding must not aggregate member scores or history")):
            receipt = fx.live.place_bid(fx.tokens[0], fx.event_id, state["current_lot"]["id"], 5, str(uuid4()))
        _, fresh = fx.live.get_view(fx.tokens[0], fx.event_id)
        self.assertEqual(fresh["bids"][0]["id"], receipt["id"])
        self.assertEqual(fresh["current_lot"]["highest_bid"], 5)


if __name__ == "__main__":
    unittest.main()
