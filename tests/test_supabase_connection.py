"""Supabase diagnostic safety checks with temporary config and a fake driver."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import check_supabase


class SupabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-supabase-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "secrets.toml"
        self.values = {
            "host": "aws-0-ap-northeast-2.pooler.supabase.com",
            "port": 5432,
            "database": "postgres",
            "user": "postgres.syntheticproject1234",
            "password": "synthetic-secret-must-never-be-printed",
        }

    def write_config(self, **changes):
        values = dict(self.values, **changes)
        self.path.write_text(
            "[supabase]\n" + "\n".join(
                f"{key} = {json.dumps(value)}" for key, value in values.items()
            ),
            encoding="utf-8",
        )

    def run_cli(self, *args, connect=None):
        connect = connect if connect is not None else Mock()
        driver = SimpleNamespace(connect=connect)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"psycopg": driver}):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = check_supabase.main(
                    ["--secrets-file", str(self.path), *args]
                )
        return status, stdout.getvalue() + stderr.getvalue(), connect

    def fake_connection(self, row=(170006, "on", True, False)):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = row
        return connection

    def assert_safe_cleanup(self, connection):
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(statements[0], "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        self.assertTrue(all("COMMIT" not in sql.upper() for sql in statements))
        connection.close.assert_called_once_with()

    def test_init_preserves_existing_file_byte_for_byte(self):
        original = b"# already configured\r\npassword = 'synthetic-existing-secret'\r\n"
        self.path.write_bytes(original)
        status, output, connect = self.run_cli("--init-config")
        self.assertEqual(status, 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("synthetic-existing-secret", output)
        connect.assert_not_called()

    def test_init_creates_parent_directory_with_template_values_and_input_guidance(self):
        self.path = self.path.parent / "nested" / "secrets.toml"
        self.assertTrue(check_supabase.init_config(self.path))
        created = self.path.read_text(encoding="utf-8")
        template = check_supabase.TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(tomllib.loads(created), tomllib.loads(template))
        self.assertIn("실제 입력 파일입니다", created)
        self.assertIn("프로젝트 DB 비밀번호를 입력하세요", created)
        self.assertNotIn("실제 비밀번호를 넣지 마세요", created)
        self.assertIn("실제 비밀번호를 넣지 마세요", template)
        self.assertFalse(check_supabase.init_config(self.path))

    def test_template_is_intentionally_incomplete(self):
        check_supabase.init_config(self.path)
        with self.path.open("rb") as stream:
            values = tomllib.load(stream)["supabase"]
        self.assertEqual(values["port"], 5432)
        self.assertEqual(values["database"], "postgres")
        for name in ("host", "user", "password"):
            self.assertEqual(values[name], "")
        status, _, connect = self.run_cli("--check-config")
        self.assertEqual(status, 2)
        connect.assert_not_called()

    def test_missing_config_does_not_connect(self):
        status, output, connect = self.run_cli()
        self.assertEqual(status, 2)
        self.assertTrue(output.strip())
        connect.assert_not_called()

    def test_malformed_config_does_not_print_secret(self):
        secret = self.values["password"]
        for document in (
            f'[supabase]\npassword = "{secret}\n',
            f'[supabase]\npassword = "{secret}"\npassword = "duplicate"\n',
            f'[supabase]\npassword = "{secret}"\nport = not_a_number\n',
        ):
            with self.subTest(document_kind=document.splitlines()[-1][:10]):
                self.path.write_text(document, encoding="utf-8")
                status, output, connect = self.run_cli()
                self.assertEqual(status, 2)
                self.assertNotIn(secret, output)
                self.assertNotIn("Traceback", output)
                connect.assert_not_called()

    def test_incomplete_config_does_not_print_secret(self):
        secret = self.values["password"]
        documents = [f'password = "{secret}"\n', f'supabase = "{secret}"\n']
        for missing in self.values:
            documents.append("[supabase]\n" + "\n".join(
                f"{key} = {json.dumps(value)}"
                for key, value in self.values.items() if key != missing
            ))
        for index, document in enumerate(documents):
            with self.subTest(case=index):
                self.path.write_text(document, encoding="utf-8")
                status, output, connect = self.run_cli()
                self.assertEqual(status, 2)
                self.assertNotIn(secret, output)
                connect.assert_not_called()

    def test_bad_hosts_are_rejected_before_connect(self):
        for host in (
            "", "localhost", "127.0.0.1", "::1",
            "db.syntheticproject1234.supabase.co",
            "https://aws-0-ap-northeast-2.pooler.supabase.com",
            "postgresql://postgres:synthetic-url-secret@localhost/postgres",
            "aws-0-ap-northeast-2.pooler.supabase.com.evil.example",
            "aws-0-ap-northeast-2.pooler.supabase.com:5432",
            "aws-0-ap-northeast-2.pooler.supabase.com/path",
            "aws-0-ap-northeast-2.pooler.supabase.com\nlocalhost",
        ):
            with self.subTest(host=host):
                self.write_config(host=host)
                status, output, connect = self.run_cli()
                self.assertEqual(status, 2)
                self.assertNotIn(self.values["password"], output)
                self.assertNotIn("synthetic-url-secret", output)
                connect.assert_not_called()

    def test_bad_ports_are_rejected_before_connect(self):
        for port in (6543, 5433, 0, -1, 65536, "5432", True, 5432.0):
            with self.subTest(port=port):
                self.write_config(port=port)
                status, _, connect = self.run_cli()
                self.assertEqual(status, 2)
                connect.assert_not_called()

    def test_bad_users_are_rejected_before_connect(self):
        for user in (
            "", "postgres", "postgres.short", "other.syntheticproject1234",
            "postgres.synthetic-project", "postgres.SYNTHETICPROJECT",
            "postgres." + "a" * 41, "postgres.syntheticproject@localhost",
            "postgres.syntheticproject\notheruser",
        ):
            with self.subTest(user=user):
                self.write_config(user=user)
                status, _, connect = self.run_cli()
                self.assertEqual(status, 2)
                connect.assert_not_called()

    def test_wrong_database_or_password_controls_are_rejected(self):
        invalid = [dict(database="other_database"), dict(password="   ")]
        invalid.extend(dict(password="synthetic" + char + "secret")
                       for char in ("\0", "\r", "\n"))
        for values in invalid:
            with self.subTest(field=next(iter(values))):
                self.write_config(**values)
                status, _, connect = self.run_cli()
                self.assertEqual(status, 2)
                connect.assert_not_called()

    def test_literal_password_is_preserved_including_spaces_and_backslashes(self):
        password = '  synthetic@#$:with\\literal\\backslashes"and%20spaces  '
        self.write_config()
        document = self.path.read_text(encoding="utf-8")
        prefix = document.split("password =", 1)[0]
        self.path.write_text(prefix + f"password = '{password}'\n", encoding="utf-8")
        config = check_supabase.load_config(self.path)
        self.assertEqual(config.password, password)
        self.assertEqual(config.connect_kwargs()["password"], password)
        self.assertNotIn(password, repr(config))

    def test_check_config_does_not_connect_or_inspect_database(self):
        self.write_config()
        with patch.object(check_supabase, "inspect_connection") as inspect:
            status, output, connect = self.run_cli("--check-config")
        self.assertEqual(status, 0)
        self.assertNotIn(self.values["password"], output)
        connect.assert_not_called()
        inspect.assert_not_called()

    def test_database_check_uses_read_only_transaction_and_rolls_back(self):
        self.write_config()
        config = check_supabase.load_config(self.path)
        connection = self.fake_connection()
        connect = Mock(return_value=connection)
        result = check_supabase.inspect_connection(config, connect)
        self.assertEqual(result, {
            "postgres_major": 17, "read_only": True,
            "cron_available": True, "cron_installed": False,
        })
        connect.assert_called_once()
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["sslmode"], "require")
        self.assertGreater(kwargs["connect_timeout"], 0)
        self.assertIn("default_transaction_read_only=on", kwargs["options"])
        self.assertIn("statement_timeout=", kwargs["options"])
        self.assertTrue(kwargs["autocommit"])
        self.assertEqual(kwargs["password"], self.values["password"])
        self.assertEqual(connection.execute.call_count, 3)
        self.assertTrue(connection.execute.call_args_list[1].args[0].startswith("SELECT "))
        self.assert_safe_cleanup(connection)

    def test_query_failure_still_rolls_back_and_closes(self):
        self.write_config()
        connection = self.fake_connection()
        error = RuntimeError("synthetic-private-driver-details")
        connection.execute.side_effect = [Mock(), error, Mock()]
        with self.assertRaisesRegex(RuntimeError, "synthetic-private-driver-details"):
            check_supabase.inspect_connection(
                check_supabase.load_config(self.path), Mock(return_value=connection)
            )
        self.assert_safe_cleanup(connection)

    def test_rollback_failure_still_closes(self):
        self.write_config()
        connection = self.fake_connection()
        query_error = RuntimeError("synthetic-query-failed")
        connection.execute.side_effect = [
            Mock(), query_error, RuntimeError("synthetic-rollback-failed"),
        ]
        with self.assertRaisesRegex(RuntimeError, "synthetic-query-failed"):
            check_supabase.inspect_connection(
                check_supabase.load_config(self.path), Mock(return_value=connection)
            )
        self.assert_safe_cleanup(connection)

    def test_unconfirmed_read_only_state_fails_and_cleans_up(self):
        self.write_config()
        for row in (None, (170006, "off", True, False)):
            with self.subTest(row=row):
                connection = self.fake_connection(row)
                with self.assertRaises(RuntimeError):
                    check_supabase.inspect_connection(
                        check_supabase.load_config(self.path), Mock(return_value=connection)
                    )
                self.assert_safe_cleanup(connection)

    def test_cli_driver_errors_are_sanitized(self):
        self.write_config()
        for state in (None, "28P01", "28000", "53300", "57014", "unknown"):
            with self.subTest(sqlstate=state):
                error = RuntimeError(
                    f"postgresql://{self.values['user']}:{self.values['password']}"
                    f"@{self.values['host']}:5432/postgres synthetic-driver-detail"
                )
                error.sqlstate = state
                status, output, connect = self.run_cli(connect=Mock(side_effect=error))
                self.assertEqual(status, 4)
                self.assertTrue(output.strip())
                for private in (self.values["password"], self.values["user"],
                                self.values["host"], "synthetic-driver-detail", "Traceback"):
                    self.assertNotIn(private, output)
                connect.assert_called_once()

    def test_cli_query_failure_is_sanitized_and_cleans_up(self):
        self.write_config()
        connection = self.fake_connection()
        connection.execute.side_effect = [
            Mock(), RuntimeError(self.values["password"]), Mock(),
        ]
        status, output, _ = self.run_cli(connect=Mock(return_value=connection))
        self.assertEqual(status, 4)
        self.assertNotIn(self.values["password"], output)
        self.assert_safe_cleanup(connection)

    def test_cli_success_reports_capabilities_without_credentials(self):
        self.write_config()
        connection = self.fake_connection()
        status, output, _ = self.run_cli(connect=Mock(return_value=connection))
        self.assertEqual(status, 0)
        self.assertIn("PostgreSQL 17", output)
        self.assertIn("pg_cron", output)
        for private in (self.values["host"], self.values["user"], self.values["password"]):
            self.assertNotIn(private, output)
        self.assert_safe_cleanup(connection)


if __name__ == "__main__":
    unittest.main()
