"""Pipeline boundaries for live delivery; no credentials or network access."""
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psycopg

from roly.core import session_query
from roly.live_auction import LiveAuction
from roly.postgres import PostgresConnection
from tests.test_postgres_adapter import RecordingDriver


class DeliveryDriver(RecordingDriver):
    def execute(self, statement, parameters=None):
        cursor = super().execute(statement, parameters)
        if statement == "ROLLBACK":
            self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
        return cursor


class LiveFastDeliveryTests(unittest.TestCase):
    def setUp(self):
        capability = patch.object(psycopg.capabilities, "has_pipeline", return_value=True)
        capability.start()
        self.addCleanup(capability.stop)
        self.raw = DeliveryDriver()
        self.db = PostgresConnection("rolymoly_qa_0123456789abcdef", self.raw)

    @staticmethod
    def view_queries():
        return [session_query("synthetic-session"), *LiveAuction._view_statements(19),
                ("SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision", None)]

    @staticmethod
    def bid_writes():
        return [
            ("UPDATE live_lots SET highest_bid=? WHERE id=?", (10, 19)),
            ("INSERT INTO live_bids(request_id) VALUES(?)", ("synthetic-request",)),
            ("UPDATE live_sessions SET updated_at=? WHERE event_id=?", ("synthetic-time", 7)),
            ("INSERT INTO live_events(type) VALUES(?)", ("BID",)),
        ]

    def test_owned_public_snapshot_begins_reads_and_rolls_back_in_one_pipeline(self):
        result = self.db.fetch_snapshot_batches(self.view_queries())
        self.assertEqual(len(result), 9)
        self.assertEqual(len(self.raw.pipeline_batches), 1)
        self.assertEqual(self.raw.pipeline_batches[0], self.raw.calls)
        statements = [query for query, _ in self.raw.calls]
        self.assertEqual(statements[0], "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertIn('"rolymoly_qa_0123456789abcdef"', statements[1])
        self.assertEqual(statements[-1], "ROLLBACK")
        self.assertIn("clock_timestamp", statements[-2])
        self.assertFalse(any("advisory" in query or query == "COMMIT" for query in statements))
        self.assertFalse(self.db.in_transaction)
        self.assertFalse(self.raw.closed)

    def test_owned_snapshot_rejects_every_write_before_sending_begin(self):
        for bad in ("UPDATE live_lots SET highest_bid=0", "SELECT 1; DELETE FROM live_bids",
                    "SELECT pg_advisory_xact_lock(1)", "WITH changed AS (DELETE FROM live_bids) SELECT 1"):
            with self.subTest(query=bad), self.assertRaises(sqlite3.ProgrammingError):
                self.db.fetch_snapshot_batches([("SELECT 1", None), (bad, None)])
        self.assertEqual(self.raw.calls, [])

    def test_owned_snapshot_never_ends_an_external_transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        calls = list(self.raw.calls)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.db.fetch_snapshot_batches(self.view_queries())
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.raw.calls, calls)
        self.db.fetch_batches([("SELECT 1", None)])
        self.assertTrue(self.db.in_transaction)
        self.assertFalse(any(query in ("ROLLBACK", "COMMIT") for query, _ in self.raw.calls))

    def test_failed_snapshot_rolls_back_outside_pipeline_and_can_be_reused(self):
        for failure in ("sync", "statement", "fetch"):
            with self.subTest(failure=failure):
                self.raw = DeliveryDriver()
                self.db = PostgresConnection("rolymoly", self.raw)
                error = psycopg.errors.UniqueViolation("synthetic-private-value")
                if failure == "sync":
                    self.raw.sync_error = error
                elif failure == "statement":
                    self.raw.fail_on = "SELECT"
                else:
                    self.raw.fetch_error = error
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.db.fetch_snapshot_batches([("SELECT 1", None)])
                self.assertNotIn("synthetic-private", str(caught.exception))
                self.assertEqual(caught.exception.sqlstate, "23505")
                self.assertFalse(self.db.in_transaction)
                self.assertEqual(self.raw.calls[-1][0], "ROLLBACK")
                self.raw.sync_error = self.raw.fetch_error = self.raw.fail_on = None
                self.assertEqual(self.db.fetch_snapshot_batches([("SELECT 1", None)])[0][0][0], 7)

    def test_four_bid_writes_and_returning_ids_share_one_pipeline_without_commit(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.raw.calls.clear()
        self.raw.pipeline_batches.clear()
        results = self.db.execute_batch(self.bid_writes())
        self.assertEqual(len(self.raw.pipeline_batches), 1)
        self.assertEqual(len(self.raw.calls), 4)
        self.assertEqual([result.lastrowid for result in results], [None, 417, None, 417])
        self.assertTrue(self.db.in_transaction)
        self.assertFalse(any(query in ("COMMIT", "ROLLBACK") for query, _ in self.raw.calls))
        self.db.commit()
        self.assertFalse(self.db.in_transaction)

    def test_failed_write_batch_keeps_transaction_for_callers_atomic_rollback(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.raw.calls.clear()
        self.raw.fail_on = "INSERT INTO live_events"
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.db.execute_batch(self.bid_writes())
        self.assertNotIn("synthetic-private", str(caught.exception))
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(len(self.raw.calls), 4)
        self.assertFalse(any(query in ("COMMIT", "ROLLBACK") for query, _ in self.raw.calls))
        self.db.rollback()
        self.assertFalse(self.db.in_transaction)

    def test_batch_rejects_missing_transaction_and_embedded_controls_before_writing(self):
        with self.assertRaises(sqlite3.ProgrammingError):
            self.db.execute_batch(self.bid_writes())
        self.assertEqual(self.raw.calls, [])
        self.db.execute("BEGIN IMMEDIATE")
        calls = list(self.raw.calls)
        for bad in ("COMMIT", "ROLLBACK", "CREATE TABLE bad(x int)",
                    "UPDATE live_lots SET highest_bid=0; COMMIT", "SELECT 1"):
            with self.subTest(query=bad), self.assertRaises(sqlite3.ProgrammingError):
                self.db.execute_batch([self.bid_writes()[0], (bad, None)])
        self.assertEqual(self.raw.calls, calls)

    def test_no_pipeline_has_identical_read_cleanup_and_write_ownership(self):
        with patch.object(psycopg, "capabilities", SimpleNamespace()):
            self.assertEqual(len(self.db.fetch_snapshot_batches(self.view_queries())), 9)
            self.assertFalse(self.db.in_transaction)
            self.db.execute("BEGIN IMMEDIATE")
            self.assertEqual(self.db.execute_batch(self.bid_writes())[1].lastrowid, 417)
            self.assertTrue(self.db.in_transaction)
            self.db.rollback()
        self.assertEqual(self.raw.pipeline_batches, [])


if __name__ == "__main__":
    unittest.main()
