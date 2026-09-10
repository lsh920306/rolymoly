"""Request timing isolation and real adapter boundaries without network access."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import anyio
import psycopg

from roly import auction_metrics as timing, postgres
from tests.test_postgres_adapter import RecordingDriver


class AuctionMetricsTests(unittest.TestCase):
    def test_inactive_scope_is_free_and_nested_scopes_restore_parent(self):
        with patch.object(timing, "perf_counter", side_effect=AssertionError("inactive clock")):
            with timing.measure_stage("statement"):
                pass
        with timing.measure_operation() as outer:
            outer.record("dispatch", .003)
            with timing.measure_operation() as inner:
                inner.record("commit", .005)
            with patch.object(timing, "perf_counter", side_effect=[1.0, 1.007]):
                with timing.measure_stage("statement"):
                    pass
        self.assertEqual(outer.as_dict(), {
            "dispatch": {"count": 1, "elapsed_ms": 3.0, "errors": 0},
            "statement": {"count": 1, "elapsed_ms": 7.0, "errors": 0}})
        self.assertEqual(inner.as_dict(), {"commit": {"count": 1, "elapsed_ms": 5.0, "errors": 0}})
        outer.record("statement", 9)
        self.assertEqual(outer.as_dict()["statement"]["count"], 1)

    def test_worker_context_follows_anyio_without_cross_request_leakage(self):
        async def run():
            results = []
            async def request(stage):
                with timing.measure_operation() as metrics:
                    await anyio.to_thread.run_sync(lambda: metrics.record(stage, .002))
                    await anyio.to_thread.run_sync(lambda: self.record_stage())
                results.append(metrics.as_dict())
            async with anyio.create_task_group() as group:
                group.start_soon(request, "commit")
                group.start_soon(request, "rollback")
            return results
        results = anyio.run(run)
        self.assertEqual({tuple(sorted(row)) for row in results},
                         {("commit", "statement"), ("rollback", "statement")})
        self.assertTrue(all(row["statement"]["count"] == 1 for row in results))

    @staticmethod
    def record_stage():
        with timing.measure_stage("statement"):
            pass

    def test_failures_count_without_retaining_exception_or_payload(self):
        error = RuntimeError("private-token-and-query")
        with timing.measure_operation() as metrics:
            with self.assertRaises(RuntimeError) as raised:
                with timing.measure_stage("commit"):
                    raise error
        self.assertIs(raised.exception, error)
        self.assertEqual(metrics.as_dict()["commit"]["errors"], 1)
        self.assertNotIn("private", json.dumps(metrics.as_dict()))
        with self.assertRaises(ValueError):
            timing.OperationMetrics().record("private-token-as-label", 1)

    def test_pipeline_duration_includes_synchronization_and_keeps_transactions(self):
        clock = [0.0]
        class TimedDriver(RecordingDriver):
            @contextmanager
            def pipeline(self):
                with super().pipeline():
                    yield
                    clock[0] += .013
            def execute(self, query, parameters=None):
                result = super().execute(query, parameters)
                if query == "ROLLBACK":
                    self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
                return result
        raw = TimedDriver()
        db = postgres.PostgresConnection("rolymoly", raw)
        with patch.object(psycopg.capabilities, "has_pipeline", return_value=True), \
             patch.object(timing, "perf_counter", side_effect=lambda: clock[0]), \
             timing.measure_operation() as metrics:
            self.assertEqual(db.fetch_snapshot_batches([("SELECT 1", None)])[0][0][0], 7)
            db.begin_writer_batches([("SELECT 2", None)])
            db.execute_batch([("UPDATE members SET score=? WHERE id=?", (1, 2))])
            self.assertTrue(db.in_transaction)
            db.commit()
            self.assertFalse(db.in_transaction)
        for name in ("read_pipeline", "writer_begin", "write_pipeline"):
            self.assertEqual(metrics.as_dict()[name], {"count": 1, "elapsed_ms": 13.0, "errors": 0})
        self.assertEqual(metrics.as_dict()["commit"]["count"], 1)
        self.assertEqual(sum(call[0] == "COMMIT" for call in raw.calls), 1)

    def test_failed_pipeline_records_error_and_preserves_rollback(self):
        raw = RecordingDriver()
        raw.sync_error = psycopg.OperationalError("private-db-host")
        db = postgres.PostgresConnection("rolymoly", raw)
        with patch.object(psycopg.capabilities, "has_pipeline", return_value=True), timing.measure_operation() as metrics:
            with self.assertRaises(sqlite3.OperationalError):
                db.begin_writer_batches([("SELECT 1", None)])
        self.assertEqual(metrics.as_dict()["writer_begin"]["errors"], 1)
        self.assertEqual(metrics.as_dict()["rollback"]["count"], 1)
        self.assertEqual(raw.calls[-1], ("ROLLBACK", None))
        self.assertNotIn("private", json.dumps(metrics.as_dict()))

    def test_checkout_success_failure_and_background_reset_are_separate(self):
        raw = RecordingDriver()
        pool = SimpleNamespace(getconn=lambda: raw, putconn=lambda connection: None)
        before = timing.pool_background_stats().get("pool_reset", {}).get("count", 0)
        with patch("roly.storage_config.postgres_kwargs", return_value={}), \
             patch.object(postgres, "_connection_pool", return_value=pool), timing.measure_operation() as metrics:
            connection = postgres.connect()
            postgres._reset_pool_connection(raw)
            connection.close()
        self.assertEqual(set(metrics.as_dict()), {"pool_checkout"})
        self.assertEqual(timing.pool_background_stats()["pool_reset"]["count"], before + 1)
        with patch("roly.storage_config.postgres_kwargs", return_value={}), \
             patch.object(postgres, "_connection_pool", return_value=pool), \
             patch.object(pool, "getconn", side_effect=psycopg.OperationalError("private-host")), \
             timing.measure_operation() as failed:
            with self.assertRaises(sqlite3.OperationalError):
                postgres.connect()
        self.assertEqual(failed.as_dict()["pool_checkout"]["errors"], 1)
        self.assertNotIn("private", json.dumps(failed.as_dict()))


if __name__ == "__main__":
    unittest.main()
