"""Exercise the real psycopg pool with isolated recording connections.

No credentials or network: production lease/reset boundaries run against the
official pool's actual worker threads, queue limits and connection replacement.
"""
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import psycopg

from roly import postgres
from tests.test_postgres_adapter import RecordingDriver

_CONNECTION_CLASS_FACTORY = postgres._pooled_connection_class


class PoolDriver(RecordingDriver):
    created = []
    creation_lock = threading.Lock()

    @classmethod
    def connect(cls, _conninfo="", **kwargs):
        raw = cls()
        raw.kwargs = kwargs
        with cls.creation_lock:
            cls.created.append(raw)
        return raw

    def __init__(self):
        super().__init__()
        self.pgconn = self.info
        self.autocommit = True
        self.read_only = self.isolation_level = self.deferrable = None
        self.prepare_threshold = None
        self.row_factory = postgres._row_factory
        self.session_state = {}
        self.reset_failed = self.rollback_failed = self.remote_closed = False

    def execute(self, statement, parameters=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        if self.closed or self.remote_closed:
            self.close()
            raise psycopg.OperationalError("synthetic-private-remote-host")
        if text == "DISCARD ALL":
            if self.reset_failed:
                raise psycopg.OperationalError("synthetic-private-reset-detail")
            self.session_state.clear()
        elif text.startswith("SET LOCAL search_path TO"):
            self.session_state["search_path"] = text
        return super().execute(statement, parameters)

    def rollback(self):
        if self.rollback_failed:
            raise psycopg.OperationalError("synthetic-private-rollback-detail")
        return super().rollback()

    def close(self):
        super().close()
        self.info.transaction_status = psycopg.pq.TransactionStatus.UNKNOWN

    def __repr__(self):
        return "<Recording pool connection>"


class PostgresPoolTests(unittest.TestCase):
    def setUp(self):
        postgres.close_pools()
        PoolDriver.created = []
        self.settings = {"host": "local-fake", "user": "fake", "password": "test-only"}
        for target, options in (
            ("roly.storage_config.postgres_kwargs", {"side_effect": lambda: self.settings.copy()}),
            ("roly.postgres._pooled_connection_class", {"return_value": PoolDriver}),
            ("roly.postgres.POOL_MAX_SIZE", {"new": 1}),
            ("roly.postgres.POOL_TIMEOUT", {"new": 0.2}),
            ("psycopg.capabilities.has_pipeline", {"return_value": True}),
        ):
            mock = patch(target, **options)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(postgres.close_pools)

    @staticmethod
    def ready(pool):
        deadline = time.monotonic() + 3
        while pool.get_stats()["pool_available"] < 1 and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        if pool.get_stats()["pool_available"] < 1:
            raise AssertionError("Pool did not return a cleaned connection")

    def test_reuse_clears_transaction_and_session_before_switching_schema(self):
        first = postgres.connect("rolymoly_qa_0123456789abcdef")
        raw, pool = first._raw, first._pool
        first.execute("BEGIN")
        self.assertIn('"rolymoly_qa_0123456789abcdef"', raw.session_state["search_path"])
        raw.session_state["temporary_object"] = "private-to-first-lease"
        raw.read_only = True
        raw.autocommit = False
        raw.prepare_threshold = 4
        raw.row_factory = object()
        first.close()
        self.ready(pool)
        self.assertEqual(raw.calls[-2:], [("ROLLBACK", None), ("DISCARD ALL", None)])
        second = postgres.connect("rolymoly_qa_fedcba9876543210")
        self.addCleanup(second.close)
        self.assertIs(second._raw, raw)
        self.assertEqual(raw.session_state, {})
        self.assertTrue(raw.autocommit)
        self.assertIsNone(raw.read_only)
        self.assertIsNone(raw.prepare_threshold)
        self.assertIs(raw.row_factory, postgres._row_factory)
        second.execute("BEGIN IMMEDIATE")
        self.assertIn('"rolymoly_qa_fedcba9876543210"', raw.session_state["search_path"])
        self.assertIn(("SELECT pg_advisory_xact_lock(%s)",
                       (postgres.advisory_key(second.schema),)), raw.calls)
        self.assertEqual(len(PoolDriver.created), 1)
        first.close()  # Double-return must not make the active lease available.
        self.assertEqual(pool.get_stats()["pool_available"], 0)
        for operation in (lambda: first.execute("SELECT 1"), first.commit, first.rollback,
                          lambda: first.fetch_batches([])):
            with self.assertRaises(sqlite3.ProgrammingError):
                operation()

    def test_sql_error_returns_clean_lease_without_committing(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with postgres.connect() as connection:
                raw, pool = connection._raw, connection._pool
                connection.execute("BEGIN IMMEDIATE")
                raw.fail_on = "INVALID"
                connection.execute("INVALID")
        self.ready(pool)
        self.assertNotIn(("COMMIT", None), raw.calls)
        self.assertEqual(raw.calls[-2:], [("ROLLBACK", None), ("DISCARD ALL", None)])
        with postgres.connect() as later:
            self.assertIs(later._raw, raw)
            self.assertFalse(later.in_transaction)

    def test_failed_rollback_discards_connection_and_sanitizes_pool_logs(self):
        first = postgres.connect()
        raw, pool = first._raw, first._pool
        first.execute("BEGIN")
        raw.rollback_failed = True
        with self.assertLogs("psycopg.pool", level=logging.INFO) as captured:
            first.close()
            self.ready(pool)
        self.assertTrue(raw.closed)
        self.assertNotIn("synthetic-private", "\n".join(captured.output))
        with postgres.connect() as later:
            self.assertIsNot(later._raw, raw)

    def test_reset_failure_never_releases_poisoned_connection(self):
        first = postgres.connect()
        raw, pool = first._raw, first._pool
        raw.reset_failed = True
        with self.assertLogs("psycopg.pool", level=logging.INFO) as captured:
            first.close()
            self.ready(pool)
        self.assertTrue(raw.closed)
        self.assertNotIn("synthetic-private", "\n".join(captured.output))
        with postgres.connect() as later:
            self.assertIsNot(later._raw, raw)

    def test_long_idle_disconnect_is_replaced_before_lending(self):
        first = postgres.connect()
        raw, pool = first._raw, first._pool
        first.close()
        self.ready(pool)
        raw._roly_released_at = time.monotonic() - 31
        raw.remote_closed = True
        with self.assertLogs("psycopg.pool", level=logging.INFO) as captured:
            with postgres.connect() as later:
                self.assertIsNot(later._raw, raw)
        self.assertNotIn("synthetic-private", "\n".join(captured.output))

    def test_capacity_blocks_extra_request_and_never_shares_active_connection(self):
        with patch.object(postgres, "POOL_MAX_SIZE", 3):
            first = postgres.connect()
        pool = first._pool
        first.close()
        self.ready(pool)
        held, guard, all_held, release = [], threading.Lock(), threading.Event(), threading.Event()

        def hold():
            with postgres.connect() as connection:
                with guard:
                    held.append(id(connection._raw))
                    if len(held) == 3:
                        all_held.set()
                if not release.wait(3):
                    raise AssertionError("Timed out releasing isolated leases")

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(hold) for _ in range(3)]
            try:
                self.assertTrue(all_held.wait(3))
                self.assertEqual(len(set(held)), 3)
                with self.assertRaises(sqlite3.OperationalError) as unavailable:
                    postgres.connect()
                self.assertNotIn("local-fake", str(unavailable.exception))
                self.assertLessEqual(len(PoolDriver.created), 3)
            finally:
                release.set()
            for future in futures:
                future.result()

    def test_settings_rotation_retires_old_pool_without_cross_lending(self):
        previous = postgres.connect()
        old_raw, old_pool = previous._raw, previous._pool
        self.settings["password"] = "changed-test-only"
        with postgres.connect() as current:
            self.assertIsNot(current._pool, old_pool)
            self.assertTrue(old_pool.closed)
            self.assertIsNot(current._raw, old_raw)
            self.assertEqual(current._raw.kwargs["password"], "changed-test-only")
        previous.close()
        self.assertTrue(old_raw.closed)

    def test_driver_connect_failure_is_sanitized_before_background_logging(self):
        # Use the production subclass, isolated from the patched pool factory.
        with patch.object(psycopg.Connection, "connect", side_effect=psycopg.OperationalError("synthetic-private-host-password")):
            connection_type = _CONNECTION_CLASS_FACTORY()
            with self.assertRaises(psycopg.OperationalError) as failure:
                connection_type.connect(host="local-fake", password="test-only")
        self.assertNotIn("synthetic-private", str(failure.exception))
        self.assertTrue(failure.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
