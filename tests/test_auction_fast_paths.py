"""Short worker locks and batched PG bid validation, using isolated SQLite."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from roly.live_auction import LiveAuction
from roly.postgres import _batch_select
from tests import test_live_auction as fixtures


CLOCK_QUERY = "SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision"


class BatchedConnection:
    def __init__(self, db, clock, calls, batches, before_clock=None):
        self.db, self.clock, self.calls, self.batches = db, clock, calls, batches
        self.before_clock = before_clock

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, query, parameters=None):
        self.calls.append(query)
        return self.db.execute(query, parameters or ())

    def execute_batch(self, statements):
        return [self.execute(query, params) for query, params in statements]

    def begin_writer_batches(self, statements):
        self.execute("BEGIN IMMEDIATE")
        return self.fetch_batches(statements)

    def fetch_batches(self, statements):
        # Apply the production batch guard, then exercise every SELECT against
        # the actual domain schema. Only PG's clock spelling needs translation.
        for query, params in statements:
            _batch_select(query, params)
        self.batches.append(statements)
        rows = []
        for query, params in statements:
            if query == CLOCK_QUERY:
                if self.before_clock:
                    self.before_clock()
                rows.append([(self.clock(),)])
            else:
                rows.append(self.db.execute(query, params or ()).fetchall())
        return rows


class AuctionFastPathTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    setUp = fixtures.LiveAuctionTests.setUp
    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start

    def pg_bidder(self, before_clock=None):
        calls, batches = [], []

        def connect():
            return BatchedConnection(self.core.connect(), self.clock, calls, batches, before_clock)

        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path,
                                 connect=connect, session=self.core.session)
        return LiveAuction(facade, self.comp), calls, batches

    def test_idle_ready_running_paused_and_unsold_workers_never_take_writer_lock(self):
        self.live.configure(self.admin, self.event, order=self.pool)
        with patch.object(self.core, "transaction", side_effect=AssertionError("idle writer lock")):
            self.assertEqual(self.live.settle_due(), [])
        self.live.start(self.admin, self.event)
        with patch.object(self.core, "transaction", side_effect=AssertionError("early writer lock")):
            self.assertEqual(self.live.settle_due(), [])
        self.live.pause(self.admin, self.event)
        self.clock.advance(100)
        with patch.object(self.core, "transaction", side_effect=AssertionError("paused writer lock")):
            self.assertEqual(self.live.settle_due(), [])
        with self.core.transaction() as db:
            db.execute("UPDATE live_sessions SET status='WAITING',next_at=NULL WHERE event_id=?", (self.event,))
        with patch.object(self.core, "transaction", side_effect=AssertionError("unsold writer lock")):
            self.assertEqual(self.live.settle_due(), [])

    def test_stale_due_hint_rechecks_extended_deadline_and_pause_under_writer_lock(self):
        lot = self.start(bid_seconds=30)
        self.clock.value = lot["closes_at"] - 1
        self.live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid.uuid4()))
        self.clock.value = lot["closes_at"] + 1
        with patch.object(self.live, "_due_candidates", return_value=[self.event]):
            self.assertEqual(self.live.settle_due(), [])
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["status"], "OPEN")
        self.live.pause(self.admin, self.event)
        self.clock.advance(100)
        with patch.object(self.live, "_due_candidates", return_value=[self.event]):
            self.assertEqual(self.live.settle_due(), [])
        self.assertEqual(self.live.get_state(self.event)["status"], "PAUSED")

    def test_cancelled_event_is_a_candidate_even_before_deadline(self):
        self.start(bid_seconds=30)
        self.comp.cancel_event(self.admin, self.event, "cancel active synthetic auction")
        self.assertEqual(self.live.settle_due(), [self.event])
        state = self.live.get_state(self.event)
        self.assertEqual(state["status"], "CANCELLED")
        self.assertTrue(all(lot["status"] == "CANCELLED" for lot in state["lots"]))
        with patch.object(self.core, "transaction", side_effect=AssertionError("completed cleanup lock")):
            self.assertEqual(self.live.settle_due(), [])

    def test_false_negative_hint_recovers_next_tick_and_two_workers_settle_once(self):
        lot = self.start()
        self.live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid.uuid4()))
        self.clock.value = lot["closes_at"]
        with patch.object(self.live, "_due_candidates", return_value=[]):
            self.assertEqual(self.live.settle_due(), [])
        second = LiveAuction(self.core, self.comp, clock=self.clock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda service: service.settle_due(), (self.live, second)))
        self.assertEqual(sum(self.event in result for result in results), 1)
        state = self.live.get_state(self.event)
        self.assertEqual(state["current_lot"]["status"], "SOLD")
        self.assertEqual(sum(item["type"] == "SOLD" for item in state["events"]), 1)
        self.assertEqual(sum(team["budget"] - team["remaining"] for team in state["teams"]), 10)

    def test_pg_bid_batches_validation_then_preserves_receipt_and_five_second_extension(self):
        lot = self.start(bid_seconds=30)
        self.clock.advance(20)
        live, calls, batches = self.pg_bidder()
        request_id = str(uuid.uuid4())
        with patch("roly.live_auction.time.time", side_effect=AssertionError("host deadline clock")):
            receipt = live.place_bid(self.tokens[0], self.event, lot["id"], 10, request_id)
        self.assertEqual(receipt["closes_at"], lot["closes_at"] + 5)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 9)
        self.assertEqual(batches[0][-1][0], CLOCK_QUERY)
        self.assertEqual(sum(query.lstrip().upper().startswith("SELECT") for query in calls), 0)
        self.assertEqual(sum(query.lstrip().upper().startswith(("UPDATE", "INSERT")) for query in calls), 4)
        self.live.pause(self.admin, self.event)
        self.clock.advance(100)
        replay = live.place_bid(self.tokens[0], self.event, lot["id"], 10, request_id)
        self.assertEqual(replay["id"], receipt["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaisesRegex(ValueError, "같은 요청 번호"):
            live.place_bid(self.tokens[0], self.event, lot["id"], 20, request_id)
        self.assertEqual(len(self.live.get_state(self.event)["bids"]), 1)

    def test_pg_final_clock_rejects_request_that_expires_during_validation(self):
        lot = self.start()
        self.clock.value = lot["closes_at"] - 0.001
        live, calls, batches = self.pg_bidder(lambda: setattr(self.clock, "value", lot["closes_at"]))
        with self.assertRaisesRegex(ValueError, "마감"):
            live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid.uuid4()))
        self.assertEqual(len(batches), 1)
        self.assertFalse(any(query.lstrip().upper().startswith(("UPDATE", "INSERT")) for query in calls))
        self.assertFalse(self.live.get_state(self.event)["bids"])

    def test_pg_permission_event_budget_and_player_checks_remain_authoritative(self):
        lot = self.start()
        live, calls, batches = self.pg_bidder()
        team = next(team for team in self.live.get_state(self.event)["teams"] if team["captain_id"] == self.captains[0])
        cases = [(self.player_token, self.event, lot["id"], 10, PermissionError),
                 (self.tokens[0], self.event + 100, lot["id"], 10, PermissionError),
                 (self.tokens[0], self.event, lot["id"] + 100, 10, ValueError),
                 (self.tokens[0], self.event, lot["id"], team["remaining"] + 1, ValueError)]
        for token, event_id, lot_id, amount, error in cases:
            with self.subTest(amount=amount, event=event_id, lot=lot_id), self.assertRaises(error):
                live.place_bid(token, event_id, lot_id, amount, str(uuid.uuid4()))
        self.core.kick_member(self.admin, lot["member_id"], "inactive auction player")
        with self.assertRaisesRegex(ValueError, "참가 상태"):
            live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid.uuid4()))
        self.core.set_account_role(self.admin, self.account_ids[0], "member", active=False)
        batch_count = len(batches)
        with self.assertRaises(PermissionError):
            live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid.uuid4()))
        self.assertEqual(len(batches), batch_count + 1)
        self.assertFalse(any(query.lstrip().upper().startswith(("UPDATE", "INSERT")) for query in calls))
        self.assertFalse(self.live.get_state(self.event)["bids"])

    def test_pg_same_price_race_accepts_once_and_audit_failure_rolls_back(self):
        lot = self.start()
        live, _, _ = self.pg_bidder()
        execute = BatchedConnection.execute

        def fail_audit(connection, query, params=None):
            if query.startswith("INSERT INTO live_events"):
                raise sqlite3.OperationalError("synthetic audit failure")
            return execute(connection, query, params)

        with patch.object(BatchedConnection, "execute", fail_audit):
            with self.assertRaises(sqlite3.OperationalError):
                live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid.uuid4()))
        state = self.live.get_state(self.event)
        self.assertIsNone(state["current_lot"]["highest_bid"])
        self.assertFalse(state["bids"])

        def bid(token):
            try:
                return live.place_bid(token, self.event, lot["id"], 10, str(uuid.uuid4()))
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(bid, self.tokens))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.live.get_state(self.event)["bids"]), 1)


if __name__ == "__main__":
    unittest.main()
