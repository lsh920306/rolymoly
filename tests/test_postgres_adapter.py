"""Local PostgreSQL boundary checks; no credentials or network are used.

Actual PostgreSQL constraints/concurrent writes are covered by the separately
authorized isolated-schema rehearsal, not by the recording driver below.
"""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psycopg

from roly.postgres import (
    PostgresConnection, Row, _bind_query, _script_statements,
    _with_generated_id, advisory_key, connect, drop_qa_schema, parse_uri,
)


class RecordingCursor:
    def __init__(self, rows=(), driver=None):
        self.rows = list(rows)
        self.rowcount = len(self.rows)
        self.driver = driver

    def before_fetch(self):
        if self.driver is not None:
            if self.driver.pipeline_depth:
                raise AssertionError("Fetched before pipeline synchronization")
            if self.driver.fetch_error:
                raise self.driver.fetch_error

    def fetchone(self):
        self.before_fetch()
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        self.before_fetch()
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self):
        return iter(self.rows)


class RecordingDriver:
    def __init__(self):
        self.closed = False
        self.info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)
        self.calls = []
        self.fail_on = None
        self.pipeline_depth = 0
        self.pipeline_batches = []
        self.sync_error = None
        self.fetch_error = None

    @contextmanager
    def pipeline(self):
        start = len(self.calls)
        self.pipeline_depth += 1
        try:
            yield
            if self.sync_error:
                self.info.transaction_status = psycopg.pq.TransactionStatus.INERROR
                raise self.sync_error
        finally:
            self.pipeline_depth -= 1
            self.pipeline_batches.append(self.calls[start:])

    def execute(self, statement, parameters=None):
        statement = statement if isinstance(statement, str) else statement.as_string()
        self.calls.append((statement, parameters))
        if statement.startswith("BEGIN"):
            self.info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        if self.fail_on and self.fail_on in statement:
            self.info.transaction_status = psycopg.pq.TransactionStatus.INERROR
            raise psycopg.errors.UniqueViolation("synthetic-private-row-value")
        if statement.startswith("ROLLBACK TO"):
            self.info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        if "RETURNING id" in statement:
            return RecordingCursor([Row(("id",), (417,))], self)
        if statement.startswith("SELECT"):
            return RecordingCursor([Row(("value",), (7,))], self)
        return RecordingCursor(driver=self)

    def commit(self):
        self.calls.append(("COMMIT", None))
        self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def rollback(self):
        self.calls.append(("ROLLBACK", None))
        self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def close(self):
        self.closed = True


class PostgresAdapterTests(unittest.TestCase):
    def setUp(self):
        capability = patch.object(psycopg.capabilities, "has_pipeline", return_value=True)
        capability.start()
        self.addCleanup(capability.stop)

    @staticmethod
    def migration_driver(version):
        class MigrationDriver(RecordingDriver):
            def execute(self, statement, parameters=None):
                result = super().execute(statement, parameters)
                text = statement if isinstance(statement, str) else statement.as_string()
                if "COALESCE(MAX(version),0)" in text:
                    return RecordingCursor([Row(("version",), (version,))], self)
                if "SELECT rolname FROM pg_roles" in text:
                    return RecordingCursor([], self)
                return result
        return MigrationDriver()

    def test_fresh_v7_and_v8_migrations_install_change_tracking_before_final_version(self):
        from roly import postgres
        for version in (0, 7, 8):
            with self.subTest(version=version):
                raw = self.migration_driver(version)
                database = PostgresConnection("rolymoly", raw)
                with patch.object(postgres, "connect", return_value=database), \
                     patch("roly.member_ranks.initialize_ranks"), \
                     patch("roly.member_profile.initialize_postgres"):
                    postgres.initialize()
                statements = [query for query, parameters in raw.calls]
                change_at = next(i for i, query in enumerate(statements) if query.startswith("CREATE TABLE IF NOT EXISTS _auction_versions"))
                revoke_at = next(i for i, query in enumerate(statements) if query.startswith("REVOKE ALL ON SCHEMA"))
                self.assertLess(change_at, revoke_at)
                self.assertTrue(any("pg_catalog.pg_notify" in query for query in statements))
                self.assertTrue(any(query.startswith("CREATE TRIGGER auction_change") for query in statements))
                versions = [parameters[0] for query, parameters in raw.calls
                            if query.startswith("INSERT INTO _schema_migrations(version,applied_at) VALUES(%s")]
                self.assertEqual(versions, [9])
                self.assertEqual(raw.calls[-1], ("COMMIT", None))

    def test_change_tracking_migration_failure_rolls_back_without_version_advance(self):
        from roly import postgres
        raw = self.migration_driver(8)
        raw.fail_on = "CREATE TRIGGER auction_change"
        database = PostgresConnection("rolymoly", raw)
        with patch.object(postgres, "connect", return_value=database), \
             patch("roly.member_ranks.initialize_ranks"):
            with self.assertRaises(sqlite3.IntegrityError):
                postgres.initialize()
        self.assertEqual(raw.calls[-1], ("ROLLBACK", None))
        self.assertFalse(any(query.startswith("INSERT INTO _schema_migrations") for query, _ in raw.calls))
        self.assertFalse(any(query == "COMMIT" for query, _ in raw.calls))

    def test_complete_live_view_accepts_safe_clock_and_coalesce_in_one_batch(self):
        from roly.core import session_query
        from roly.live_auction import LiveAuction
        raw = RecordingDriver()
        database = PostgresConnection("rolymoly", raw)
        database.execute("BEGIN")
        statements = [session_query("synthetic-session"), *LiveAuction._view_statements(19),
                      ("SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision", None)]
        result = database.fetch_batches(statements)
        self.assertEqual(len(result), 9)
        self.assertEqual(len(raw.pipeline_batches[-1]), 9)
        self.assertTrue(database.in_transaction)
        calls = len(raw.calls)
        for query in ("SELECT EXTRACT(EPOCH FROM evil.clock_timestamp())::double precision",
                      "SELECT COALESCE(pg_advisory_xact_lock(7),0)",
                      "SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision, pg_sleep(1)"):
            with self.assertRaises(sqlite3.ProgrammingError):
                database.fetch_batches([(query, None)])
        self.assertEqual(len(raw.calls), calls)

    def test_row_unpack_index_and_dict_match_sqlite_with_duplicate_columns(self):
        with sqlite3.connect(":memory:") as database:
            database.row_factory = sqlite3.Row
            expected = database.execute("SELECT 2 AS count,35 AS spent,99 AS count").fetchone()
        actual = Row(expected.keys(), tuple(expected))
        self.assertEqual(tuple(actual), tuple(expected))
        self.assertEqual(actual.keys(), expected.keys())
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(dict(actual), dict(expected))
        for key in (0, 1, -1, "count", "spent", slice(0, 2)):
            self.assertEqual(actual[key], expected[key])
        count, spent = Row(("count", "spent"), (2, 35))
        self.assertEqual((count, spent), (2, 35))

    def test_binding_preserves_quoted_questions_comments_and_script_bodies(self):
        query = "SELECT '?' AS literal, ? AS value -- ?\n/* ? /* nested ? */ */ WHERE name LIKE '%hi%' AND id=?"
        expected = "SELECT '?' AS literal, %s AS value -- ?\n/* ? /* nested ? */ */ WHERE name LIKE '%%hi%%' AND id=%s"
        self.assertEqual(_bind_query(query), expected)
        self.assertEqual(_bind_query(query, bind=False), query)
        self.assertEqual(_bind_query('SELECT "?", $$?$$, $body$?$body$, ?'), 'SELECT "?", $$?$$, $body$?$body$, %s')
        self.assertEqual(_bind_query(r"SELECT E'escaped\'?literal', ?"), r"SELECT E'escaped\'?literal', %s")
        script = "SELECT ';'; DO $$ BEGIN PERFORM 1; END $$; /* ; */ SELECT 3"
        self.assertEqual(list(_script_statements(script)), ["SELECT ';'", "DO $$ BEGIN PERFORM 1; END $$", "/* ; */ SELECT 3"])
        for malformed in ("SELECT 'unclosed", "SELECT $$unclosed", "SELECT /* unclosed"):
            with self.assertRaises(sqlite3.ProgrammingError):
                _bind_query(malformed)

    def test_generated_ids_only_for_known_id_tables(self):
        self.assertEqual(_with_generated_id("INSERT INTO members(x) VALUES(?) -- comment"), ("INSERT INTO members(x) VALUES(?) RETURNING id -- comment", True))
        self.assertEqual(_with_generated_id("INSERT INTO audit(x) VALUES('RETURNING');"), ("INSERT INTO audit(x) VALUES('RETURNING') RETURNING id;", True))
        for statement in (
            "INSERT INTO sessions(token_hash) VALUES(?)",
            "INSERT INTO game_players(game_id) VALUES(?)",
            "INSERT INTO unknown_table(x) VALUES(?)",
            "INSERT INTO members(x) VALUES(?) RETURNING id",
            "UPDATE members SET notes=? WHERE id=?",
        ):
            self.assertEqual(_with_generated_id(statement), (statement, False))
        raw = RecordingDriver()
        database = PostgresConnection("rolymoly", raw)
        result = database.execute("INSERT INTO members(riot_id) VALUES(?)", ("Synthetic#QA",))
        self.assertEqual(result.lastrowid, 417)
        self.assertFalse(database.in_transaction)
        self.assertIn(("INSERT INTO members(riot_id) VALUES(%s) RETURNING id", ("Synthetic#QA",)), raw.calls)

    def test_invalid_schemas_and_operational_cleanup_are_rejected_before_connect(self):
        self.assertEqual(parse_uri("supabase://rolymoly"), "rolymoly")
        self.assertEqual(parse_uri("supabase://rolymoly_qa_0123456789abcdef"), "rolymoly_qa_0123456789abcdef")
        for invalid in ("supabase://public", "supabase://rolymoly/other", "supabase://rolymoly?password=x", "supabase://rolymoly_qa_abc", 'supabase://rolymoly";DROP SCHEMA public;--'):
            with self.assertRaises(ValueError):
                parse_uri(invalid)
        with patch("roly.postgres.connect") as network:
            for invalid in ("rolymoly", "public", "rolymoly_qa_invalid", "rolymoly_qa_01234567;DROP"):
                with self.assertRaises(ValueError):
                    drop_qa_schema(invalid)
            network.assert_not_called()
        self.assertEqual(advisory_key("rolymoly"), advisory_key("rolymoly"))
        self.assertNotEqual(advisory_key("rolymoly"), advisory_key("rolymoly_qa_0123456789abcdef"))

    def test_database_errors_do_not_expose_connection_or_row_values(self):
        with patch("roly.storage_config.postgres_kwargs", return_value={}), patch("roly.postgres._connection_pool") as pool:
            pool.return_value.getconn.side_effect = psycopg.OperationalError("synthetic-private-connection-secret")
            with self.assertRaises(sqlite3.OperationalError) as connection_error:
                connect("rolymoly")
        self.assertNotIn("synthetic-private", str(connection_error.exception))
        self.assertTrue(connection_error.exception.__suppress_context__)
        raw = RecordingDriver()
        raw.fail_on = "INSERT"
        database = PostgresConnection("rolymoly", raw)
        with self.assertRaises(sqlite3.IntegrityError) as row_error:
            database.execute("INSERT INTO members(riot_id) VALUES(?)", ("Synthetic#QA",))
        self.assertNotIn("synthetic-private", str(row_error.exception))
        self.assertEqual(row_error.exception.sqlstate, "23505")
        self.assertTrue(row_error.exception.__suppress_context__)
        self.assertEqual(raw.calls[-1][0], "ROLLBACK")
        self.assertFalse(database.in_transaction)

    def test_writer_snapshot_and_nested_script_transaction_boundaries(self):
        raw = RecordingDriver()
        database = PostgresConnection("rolymoly", raw)
        result = database.execute("SELECT 7 AS value")
        self.assertEqual(result.fetchone()["value"], 7)
        self.assertEqual(raw.calls[0][0], "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertTrue(raw.calls[1][0].startswith('SET LOCAL search_path TO "rolymoly"'))
        self.assertEqual(len(raw.pipeline_batches[0]), 3)
        self.assertEqual(raw.calls[-1][0], "COMMIT")
        self.assertFalse(any("advisory" in query for query, _ in raw.calls))
        raw.calls.clear()
        database.execute("BEGIN IMMEDIATE")
        self.assertEqual(raw.calls[0][0], "BEGIN ISOLATION LEVEL READ COMMITTED")
        self.assertTrue(raw.calls[1][0].startswith('SET LOCAL search_path TO "rolymoly"'))
        self.assertEqual(raw.calls[2], ("SET LOCAL lock_timeout = '15s'", None))
        self.assertEqual(raw.calls[3], ("SELECT pg_advisory_xact_lock(%s)", (advisory_key("rolymoly"),)))
        self.assertEqual(raw.pipeline_batches[-1], raw.calls)
        database.executescript("CREATE TABLE temporary_example(x INTEGER); INSERT INTO temporary_example(x) VALUES(1);")
        self.assertTrue(database.in_transaction)
        self.assertNotIn("COMMIT", [query for query, _ in raw.calls])
        database.execute("SAVEPOINT nested")
        raw.fail_on = "INVALID_WRITE"
        with self.assertRaises(sqlite3.IntegrityError):
            database.execute("INVALID_WRITE")
        self.assertTrue(database.in_transaction)
        database.execute("ROLLBACK TO nested")
        database.execute("RELEASE nested")
        database.commit()
        self.assertFalse(database.in_transaction)

    def test_batch_results_are_fetched_after_one_sync_without_ending_snapshot(self):
        raw = RecordingDriver()
        database = PostgresConnection("rolymoly_qa_0123456789abcdef", raw)
        database.execute("BEGIN")
        raw.calls.clear()
        raw.pipeline_batches.clear()
        batches = database.fetch_batches([
            ("SELECT ? AS value", (7,)),
            ("SELECT ? AS value WHERE '?' <> ';' -- keep ? literal", (8,)),
        ])
        self.assertEqual([[dict(row) for row in rows] for rows in batches], [[{"value": 7}], [{"value": 7}]])
        self.assertEqual(len(raw.pipeline_batches), 1)
        self.assertEqual(raw.pipeline_batches[0], raw.calls)
        self.assertEqual(raw.calls[0], ("SELECT %s AS value", (7,)))
        self.assertEqual(raw.calls[1], ("SELECT %s AS value WHERE '?' <> ';' -- keep ? literal", (8,)))
        self.assertTrue(database.in_transaction)
        self.assertFalse(raw.closed)
        self.assertFalse(any(query in ("COMMIT", "ROLLBACK") for query, _ in raw.calls))
        database.rollback()
        self.assertFalse(database.in_transaction)

    def test_missing_pipeline_capability_uses_same_sequential_transaction(self):
        for capability in (SimpleNamespace(has_pipeline=lambda: False), SimpleNamespace()):
            with self.subTest(capability=bool(getattr(capability, "has_pipeline", None))):
                raw = RecordingDriver()
                database = PostgresConnection("rolymoly", raw)
                with patch.object(psycopg, "capabilities", capability):
                    database.execute("BEGIN IMMEDIATE")
                    batches = database.fetch_batches([("SELECT ? AS value", (7,))])
                self.assertEqual(batches[0][0]["value"], 7)
                self.assertFalse(raw.pipeline_batches)
                self.assertEqual(raw.calls[2][0], "SET LOCAL lock_timeout = '15s'")
                self.assertIn("pg_advisory_xact_lock", raw.calls[3][0])
                self.assertTrue(database.in_transaction)
                database.rollback()

    def test_batch_rejects_mutations_locks_and_calls_before_sending_any_sql(self):
        raw = RecordingDriver()
        database = PostgresConnection("rolymoly", raw)
        with self.assertRaises(sqlite3.ProgrammingError):
            database.fetch_batches([("SELECT 7", None)])
        self.assertFalse(raw.calls)
        database.execute("BEGIN IMMEDIATE")
        before = list(raw.calls)
        for invalid in (
            "UPDATE members SET notes='changed'", "SELECT 7; DELETE FROM members",
            "WITH changed AS (DELETE FROM members RETURNING id) SELECT * FROM changed",
            "SELECT * INTO copied_members FROM members", "SELECT * FROM members FOR UPDATE",
            "SELECT nextval('members_id_seq')", "SELECT set_config('search_path','public',false)",
            'SELECT "side_effect"()', "SELECT private_side_effect()", "COMMIT", "",
        ):
            with self.subTest(query=invalid), self.assertRaises(sqlite3.ProgrammingError):
                database.fetch_batches([("SELECT 7", None), (invalid, None)])
            self.assertEqual(raw.calls, before)
        self.assertEqual(database.fetch_batches([]), [])
        self.assertEqual(raw.calls, before)
        self.assertTrue(database.in_transaction)

    def test_delayed_batch_error_is_sanitized_and_caller_owns_rollback(self):
        for phase in ("sync", "fetch"):
            with self.subTest(phase=phase):
                raw = RecordingDriver()
                database = PostgresConnection("rolymoly", raw)
                database.execute("BEGIN")
                raw.calls.clear()
                setattr(raw, f"{phase}_error", psycopg.OperationalError("synthetic-private-connection-detail"))
                with self.assertRaises(sqlite3.OperationalError) as error:
                    database.fetch_batches([("SELECT 7", None), ("SELECT 8", None)])
                self.assertNotIn("synthetic-private", str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)
                self.assertTrue(database.in_transaction)
                self.assertFalse(any(query in ("COMMIT", "ROLLBACK") for query, _ in raw.calls))
                database.rollback()
                self.assertFalse(database.in_transaction)

    def test_begin_sync_failure_is_not_returned_as_success_or_committed(self):
        raw = RecordingDriver()
        database = PostgresConnection("rolymoly", raw)
        raw.sync_error = psycopg.errors.LockNotAvailable("synthetic-private-lock-detail")
        with self.assertRaises(sqlite3.OperationalError) as error:
            database.execute("BEGIN IMMEDIATE")
        self.assertNotIn("synthetic-private", str(error.exception))
        self.assertEqual(error.exception.sqlstate, "55P03")
        self.assertNotIn("COMMIT", [query for query, _ in raw.calls])
        self.assertTrue(database.in_transaction)
        database.rollback()
        self.assertFalse(database.in_transaction)


if __name__ == "__main__":
    unittest.main()
