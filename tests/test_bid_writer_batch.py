"""Writer setup/read batching and worker snapshots without external services."""
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg

from roly.core import session_query
from roly.live_auction import LiveAuction
from roly.postgres import PostgresConnection
from tests import test_auction_fast_paths as fast
from tests import test_live_auction as fixtures
from tests.test_live_fast_delivery import DeliveryDriver
from tests.test_postgres_adapter import RecordingCursor


class WriterBatchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(psycopg.capabilities, "has_pipeline", return_value=True))
        self.raw = DeliveryDriver()
        self.db = PostgresConnection("rolymoly_qa_0123456789abcdef", self.raw)

    def test_lock_precedes_fresh_auth_and_final_clock_in_one_sync(self):
        queries = [session_query("synthetic-session"), ("SELECT * FROM live_bids WHERE request_id=?", ("synthetic",)),
                   (fast.CLOCK_QUERY, None)]
        rows = self.db.begin_writer_batches(queries)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(self.raw.pipeline_batches), 1)
        statements = [query for query, _ in self.raw.calls]
        self.assertEqual(statements[0], "BEGIN ISOLATION LEVEL READ COMMITTED")
        self.assertIn("SET LOCAL search_path", statements[1])
        self.assertIn("SET LOCAL lock_timeout", statements[2])
        self.assertEqual(statements[3], "SELECT pg_advisory_xact_lock(%s)")
        self.assertIn("FROM sessions", statements[4])
        self.assertEqual(statements[-1], fast.CLOCK_QUERY)
        self.assertTrue(self.db.in_transaction)
        self.assertFalse(any(query in ("COMMIT", "ROLLBACK") for query in statements))
        self.db.rollback()

    def test_external_transaction_and_unsafe_sql_are_rejected_before_changes(self):
        for queries in ([], [("UPDATE members SET status='APPROVED'", None)],
                        [("SELECT 1; COMMIT", None)], [("SELECT pg_advisory_xact_lock(1)", None)]):
            with self.subTest(queries=queries), self.assertRaises(sqlite3.ProgrammingError):
                self.db.begin_writer_batches(queries)
        self.assertFalse(self.raw.calls)
        self.db.execute("BEGIN")
        before = list(self.raw.calls)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.db.begin_writer_batches([("SELECT 1", None)])
        self.assertEqual(self.raw.calls, before)
        self.assertTrue(self.db.in_transaction)

    def test_setup_read_sync_and_fetch_failure_rollback_and_sanitize(self):
        for failure in ("lock", "read", "sync", "fetch"):
            with self.subTest(failure=failure):
                raw = DeliveryDriver()
                db = PostgresConnection("rolymoly", raw)
                error = psycopg.errors.UniqueViolation("synthetic-private-value")
                if failure == "lock":
                    raw.fail_on = "pg_advisory_xact_lock"
                elif failure == "read":
                    raw.fail_on = "FROM sessions"
                elif failure == "sync":
                    raw.sync_error = error
                else:
                    raw.fetch_error = error
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    db.begin_writer_batches([session_query("synthetic-session")])
                self.assertNotIn("synthetic-private", str(caught.exception))
                self.assertFalse(db.in_transaction)
                self.assertEqual(raw.calls[-1][0], "ROLLBACK")
                raw.fail_on = raw.sync_error = raw.fetch_error = None
                self.assertEqual(len(db.begin_writer_batches([("SELECT 1", None)])), 1)
                self.assertTrue(db.in_transaction)
                db.rollback()

    def test_sequential_fallback_keeps_transaction_and_lock_order(self):
        with patch.object(psycopg, "capabilities", SimpleNamespace()):
            self.db.begin_writer_batches([("SELECT 1", None)])
        self.assertFalse(self.raw.pipeline_batches)
        self.assertEqual(self.raw.calls[-2][0], "SELECT pg_advisory_xact_lock(%s)")
        self.assertEqual(self.raw.calls[-1][0], "SELECT 1")
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        self.assertEqual(self.raw.calls[-1][0], "COMMIT")

    def test_worker_probe_and_retirement_each_use_one_read_only_pipeline(self):
        class WorkerDriver(DeliveryDriver):
            def execute(self, statement, parameters=None):
                cursor = super().execute(statement, parameters)
                if isinstance(statement, str) and statement.startswith("SELECT s.event_id,s.status"):
                    return RecordingCursor([
                        {"event_id": 1, "status": "RUNNING", "event_status": "AUCTION", "closes_at": 7, "next_at": None},
                        {"event_id": 2, "status": "RUNNING", "event_status": "AUCTION", "closes_at": 8, "next_at": None},
                        {"event_id": 3, "status": "WAITING", "event_status": "AUCTION", "closes_at": None, "next_at": 6},
                        {"event_id": 4, "status": "PAUSED", "event_status": "CANCELLED", "closes_at": None, "next_at": None},
                    ], self)
                return cursor

        drivers = []

        def connect():
            drivers.append(WorkerDriver())
            return PostgresConnection("rolymoly", drivers[-1])

        live = LiveAuction(SimpleNamespace(is_postgres=True, connect=connect), None)
        self.assertEqual(live._due_candidates(), [1, 3, 4])
        self.assertTrue(live.has_active_sessions())
        self.assertEqual(len(drivers), 2)
        for raw in drivers:
            self.assertEqual(len(raw.pipeline_batches), 1)
            self.assertTrue(raw.closed)
            self.assertFalse(any("advisory" in query or query == "COMMIT" for query, _ in raw.calls))
            self.assertEqual(raw.calls[-1][0], "ROLLBACK")


class WriterBatchDomainTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    setUp = fixtures.LiveAuctionTests.setUp
    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start
    pg_bidder = fast.AuctionFastPathTests.pg_bidder

    def test_normal_bid_uses_three_boundaries_and_receipt_paths_use_two(self):
        lot = self.start()
        boundaries = []

        class Connection(fast.BatchedConnection):
            def begin_writer_batches(self, statements):
                boundaries.append("begin-lock-read")
                return super().begin_writer_batches(statements)

            def execute_batch(self, statements):
                boundaries.append("write")
                return super().execute_batch(statements)

            def commit(self):
                boundaries.append("commit")
                self.db.commit()

        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path,
                                 connect=lambda: Connection(self.core.connect(), self.clock, [], []))
        live = LiveAuction(facade, self.comp)
        key = str(uuid4())
        receipt = live.place_bid(self.tokens[0], self.event, lot["id"], 10, key)
        self.assertEqual(boundaries, ["begin-lock-read", "write", "commit"])
        for method in (live.place_bid, live.resolve_bid):
            boundaries.clear()
            repeated = method(self.tokens[0], self.event, lot["id"], 10, key)
            self.assertEqual(repeated["id"], receipt["id"])
            self.assertEqual(boundaries, ["begin-lock-read", "commit"])

    def test_receipt_resolution_batches_auth_and_is_read_only_after_settlement(self):
        lot = self.start()
        live, calls, batches = self.pg_bidder()
        key = str(uuid4())
        receipt = live.place_bid(self.tokens[0], self.event, lot["id"], 10, key)
        self.clock.value = receipt["closes_at"]
        self.live.settle_due()
        before = self.live.get_state(self.event)
        calls.clear()
        batches.clear()
        resolved = live.resolve_bid(self.tokens[0], self.event, lot["id"], 10, key)
        self.assertEqual(resolved["id"], receipt["id"])
        self.assertTrue(resolved["replayed"])
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 3)
        self.assertFalse(any(query.startswith(("INSERT", "UPDATE", "DELETE", "SELECT")) for query in calls))
        with self.assertRaisesRegex(ValueError, "일치"):
            live.resolve_bid(self.tokens[0], self.event, lot["id"], 20, key)
        with self.assertRaisesRegex(ValueError, "일치"):
            live.resolve_bid(self.tokens[1], self.event, lot["id"], 10, key)
        self.assertIsNone(live.resolve_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid4())))
        after = self.live.get_state(self.event)
        self.assertEqual(after["bids"], before["bids"])
        self.assertEqual(after["lots"], before["lots"])
        self.assertEqual(after["teams"], before["teams"])
        self.core.set_account_role(self.admin, self.account_ids[0], "member", active=False)
        with self.assertRaises(PermissionError):
            live.resolve_bid(self.tokens[0], self.event, lot["id"], 10, key)

    def test_session_expiring_after_query_preparation_cannot_bid_or_resolve(self):
        lot = self.start()
        live, calls, _ = self.pg_bidder()
        clock = datetime.now(timezone.utc)
        with self.core.transaction() as db:
            db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?",
                       ((clock + timedelta(minutes=5)).isoformat(), hashlib.sha256(self.tokens[0].encode()).hexdigest()))
        with patch("roly.live_auction.datetime", wraps=datetime) as wall_clock:
            wall_clock.now.return_value = clock + timedelta(minutes=10)
            for method in (live.place_bid, live.resolve_bid):
                with self.subTest(method=method.__name__), self.assertRaises(PermissionError):
                    method(self.tokens[0], self.event, lot["id"], 10, str(uuid4()))
        self.assertFalse(any(query.startswith(("INSERT", "UPDATE", "DELETE")) for query in calls))
        self.assertFalse(self.live.get_state(self.event)["bids"])

    def test_commit_response_loss_resolves_existing_receipt_without_second_write(self):
        lot = self.start()
        live, _, batches = self.pg_bidder()
        key = str(uuid4())
        original_connect = live.core.connect
        lost = False

        def connect():
            nonlocal lost
            connection = original_connect()
            if not lost:
                original_commit = connection.commit

                def commit():
                    nonlocal lost
                    original_commit()
                    lost = True
                    raise sqlite3.OperationalError("Synthetic lost commit response")

                connection.commit = commit
            return connection

        live.core.connect = connect
        with self.assertRaises(sqlite3.OperationalError):
            live.place_bid(self.tokens[0], self.event, lot["id"], 10, key)
        receipt = live.resolve_bid(self.tokens[0], self.event, lot["id"], 10, key)
        self.assertTrue(receipt["replayed"])
        self.assertEqual(len(batches[-1]), 3)
        self.assertEqual([bid["amount"] for bid in self.live.get_state(self.event)["bids"]], [10])


if __name__ == "__main__":
    unittest.main()
