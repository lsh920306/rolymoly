"""Bid finalization: atomic commit evidence, uncertainty and connection reuse."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import logging
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psycopg

from roly.auction_commands import execute_command
from roly.auction_metrics import measure_operation
from roly import postgres
from roly.postgres import PostgresConnection
from tests import test_live_fast_delivery as live_delivery
from tests import test_postgres_pool as pool_tests
from tests.test_live_fast_delivery import DeliveryDriver
from tests.test_postgres_adapter import RecordingCursor


class CommitDriver(DeliveryDriver):
    commit_tag = "COMMIT"
    commit_error = None

    def execute(self, statement, parameters=None):
        cursor = super().execute(statement, parameters)
        if statement == "COMMIT":
            self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
            if self.commit_error:
                raise self.commit_error
            cursor.statusmessage = self.commit_tag
        return cursor


class BidCommitBatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(psycopg.capabilities, "has_pipeline", return_value=True))
        self.raw = CommitDriver()
        self.db = PostgresConnection("rolymoly_qa_0123456789abcdef", self.raw)
        self.db.begin_writer_batches([("SELECT 1", None)])

    @staticmethod
    def writes():
        return live_delivery.LiveFastDeliveryTests.bid_writes()

    def pending_ack(self):
        live = SimpleNamespace(place_bid=lambda *args: self.db.commit_bid_batch(self.writes()))
        return execute_command(live, "synthetic", 7,
                               {"request_id": "synthetic", "lot_id": 19, "amount": 10})

    def test_commit_result_and_generated_ids_confirm_before_success_in_two_syncs(self):
        with measure_operation() as metrics:
            results = self.db.commit_bid_batch(self.writes())
        self.assertEqual(set(metrics.as_dict()), {"bid_commit_pipeline"})
        self.assertEqual(metrics.as_dict()["bid_commit_pipeline"]["count"], 1)
        self.assertEqual(metrics.as_dict()["bid_commit_pipeline"]["errors"], 0)
        self.assertEqual(len(self.raw.pipeline_batches), 2)
        final = self.raw.pipeline_batches[-1]
        self.assertEqual(len(final), 5)
        self.assertEqual(final[-1], ("COMMIT", None))
        self.assertEqual([r.lastrowid for r in results], [None, 417, None, 417])
        self.assertFalse(self.db.in_transaction)
        self.assertFalse(self.raw.closed)
        self.db.begin_writer_batches([("SELECT 1", None)])
        self.assertEqual(self.db.commit_bid_batch(self.writes())[1].lastrowid, 417)

    def test_every_write_failure_before_commit_leaves_atomic_rollback_to_owner(self):
        for index, (query, _) in enumerate(self.writes()):
            with self.subTest(index=index):
                raw = CommitDriver()
                db = PostgresConnection("rolymoly", raw)
                db.begin_writer_batches([("SELECT 1", None)])
                raw.fail_on = query.split("(")[0].split(" SET")[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    db.commit_bid_batch(self.writes())
                self.assertNotIn(("COMMIT", None), raw.calls)
                self.assertTrue(db.in_transaction)
                db.rollback()
                self.assertFalse(db.in_transaction)

    def test_commit_submission_sync_and_result_errors_stay_pending_and_discard_connection(self):
        for phase in ("submit", "sync", "fetch-value", "fetch-integrity", "fetch-permission"):
            with self.subTest(phase=phase):
                self.raw = CommitDriver()
                self.db = PostgresConnection("rolymoly", self.raw)
                self.db.begin_writer_batches([("SELECT 1", None)])
                if phase == "submit":
                    self.raw.commit_error = psycopg.OperationalError("synthetic-private")
                elif phase == "sync":
                    self.raw.sync_error = psycopg.errors.UniqueViolation("synthetic-private")
                else:
                    self.raw.fetch_error = {"fetch-value": ValueError, "fetch-integrity": psycopg.errors.UniqueViolation,
                                            "fetch-permission": PermissionError}[phase]("synthetic-private")
                ack = self.pending_ack()
                self.assertEqual(ack["status"], "pending")
                self.assertNotIn("synthetic-private", ack["message"])
                self.assertTrue(self.raw.closed)
                self.assertFalse(self.db.in_transaction)

    def test_rollback_missing_tag_and_missing_generated_id_are_never_accepted(self):
        for tag in ("ROLLBACK", None, ""):
            with self.subTest(tag=tag):
                self.raw = CommitDriver()
                self.db = PostgresConnection("rolymoly", self.raw)
                self.db.begin_writer_batches([("SELECT 1", None)])
                self.raw.commit_tag = tag
                self.assertEqual(self.pending_ack()["status"], "pending")
                self.assertTrue(self.raw.closed)
        self.raw = CommitDriver()
        self.db = PostgresConnection("rolymoly", self.raw)
        self.db.begin_writer_batches([("SELECT 1", None)])
        with patch.object(RecordingCursor, "fetchone", return_value=None):
            self.assertEqual(self.pending_ack()["status"], "pending")
        self.assertTrue(self.raw.closed)

    def test_commit_tag_without_usable_idle_connection_stays_pending(self):
        for final_state in ("closed", psycopg.pq.TransactionStatus.INTRANS,
                            psycopg.pq.TransactionStatus.INERROR,
                            psycopg.pq.TransactionStatus.UNKNOWN):
            with self.subTest(final_state=final_state):
                class UnfinishedDriver(CommitDriver):
                    def execute(driver, statement, parameters=None):
                        cursor = super().execute(statement, parameters)
                        if statement == "COMMIT":
                            if final_state == "closed":
                                driver.close()
                            else:
                                driver.info.transaction_status = final_state
                        return cursor

                self.raw = UnfinishedDriver()
                self.db = PostgresConnection("rolymoly", self.raw)
                self.db.begin_writer_batches([("SELECT 1", None)])
                self.assertEqual(self.pending_ack()["status"], "pending")
                self.assertTrue(self.raw.closed)

    def test_ack_waits_until_final_pipeline_synchronization_finishes(self):
        queued, release = threading.Event(), threading.Event()

        class BlockingDriver(CommitDriver):
            @contextmanager
            def pipeline(driver):
                with super().pipeline():
                    first = len(driver.calls)
                    yield
                    if ("COMMIT", None) in driver.calls[first:]:
                        queued.set()
                        if not release.wait(3):
                            raise AssertionError("Final pipeline was not released")

        self.raw = BlockingDriver()
        self.db = PostgresConnection("rolymoly", self.raw)
        self.db.begin_writer_batches([("SELECT 1", None)])
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(self.pending_ack)
            try:
                self.assertTrue(queued.wait(3))
                self.assertFalse(result.done(), "ACK escaped before final synchronization")
            finally:
                release.set()
            self.assertEqual(result.result(timeout=3)["status"], "accepted")
        self.assertEqual(len(self.raw.pipeline_batches), 2)

    def test_validation_rejects_empty_unsafe_or_unowned_batch_before_sending(self):
        before = list(self.raw.calls)
        for statements in ([], [("COMMIT", None)], [self.writes()[0], ("DELETE FROM live_bids; COMMIT", None)]):
            with self.subTest(statements=statements), self.assertRaises(sqlite3.ProgrammingError):
                self.db.commit_bid_batch(statements)
        self.assertEqual(self.raw.calls, before)
        self.db.rollback()
        before = list(self.raw.calls)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.db.commit_bid_batch(self.writes())
        self.assertEqual(self.raw.calls, before)

    def test_sequential_fallback_still_waits_for_commit_evidence(self):
        self.raw.pipeline_batches.clear()
        with patch.object(psycopg, "capabilities", SimpleNamespace()):
            self.assertEqual(self.db.commit_bid_batch(self.writes())[1].lastrowid, 417)
        self.assertEqual(self.raw.pipeline_batches, [])
        self.assertEqual(self.raw.calls[-1], ("COMMIT", None))
        self.assertFalse(self.db.in_transaction)


class CommitPoolDriver(pool_tests.PoolDriver):
    commit_error = None

    def execute(self, statement, parameters=None):
        cursor = super().execute(statement, parameters)
        if statement == "COMMIT":
            self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
            if self.commit_error:
                raise self.commit_error
            cursor.statusmessage = "COMMIT"
        return cursor


class BidCommitPoolTests(unittest.TestCase):
    def setUp(self):
        pool_tests.PostgresPoolTests.setUp(self)
        self.enterContext(patch("roly.postgres._pooled_connection_class", return_value=CommitPoolDriver))

    def test_uncertain_commit_returns_closed_lease_once_and_replaces_it(self):
        first = postgres.connect()
        self.addCleanup(first.close)
        raw, pool = first._raw, first._pool
        first.begin_writer_batches([("SELECT 1", None)])
        raw.commit_error = psycopg.OperationalError("synthetic-private-commit-response")
        with self.assertLogs("psycopg.pool", level=logging.INFO) as captured:
            with self.assertRaises(sqlite3.OperationalError) as failure:
                first.commit_bid_batch(BidCommitBatchTests.writes())
            self.assertTrue(raw.closed)
            first.close()
            pool_tests.PostgresPoolTests.ready(pool)
        self.assertNotIn("synthetic-private", str(failure.exception))
        self.assertNotIn("synthetic-private", "\n".join(captured.output))
        self.assertNotIn(("ROLLBACK", None), raw.calls)
        later = postgres.connect()
        self.addCleanup(later.close)
        self.assertIsNot(later._raw, raw)
        self.assertFalse(later.in_transaction)
        first.close()
        self.assertEqual(pool.get_stats()["pool_available"], 0)
        later.begin_writer_batches([("SELECT 1", None)])
        self.assertEqual(later.commit_bid_batch(BidCommitBatchTests.writes())[1].lastrowid, 417)


if __name__ == "__main__":
    unittest.main()
