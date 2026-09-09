"""Storage selection with temporary settings only; no database connections."""
from contextlib import chdir
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from roly import storage_config


class StorageConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-storage-config-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "secrets.toml"
        self.data_root = self.directory / "data"
        self.local_target = str(self.data_root / "rolymoly.sqlite3")
        self.values = {
            "host": "aws-0-ap-northeast-2.pooler.supabase.com",
            "port": 5432, "database": "postgres",
            "user": "postgres.syntheticproject1234",
            "password": "synthetic-storage-secret",
        }
        for replacement in (
            patch.object(storage_config, "DEFAULT_SECRETS", self.path),
            patch.dict(os.environ, {}, clear=True),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        cloud = patch.object(storage_config, "_cloud_document", return_value={})
        self.cloud = cloud.start()
        self.addCleanup(cloud.stop)

    def write_config(self, **changes):
        values = dict(self.values, **changes)
        self.path.write_text("[supabase]\n" + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in values.items()
        ), encoding="utf-8")

    def test_no_connection_settings_stops_without_creating_sqlite(self):
        with self.assertRaises(storage_config.ConfigError):
            storage_config.operating_database(self.data_root)
        self.assertFalse(self.data_root.exists())

    def test_blank_template_stops_without_creating_sqlite(self):
        self.write_config(host="", user="", password="")
        with self.assertRaises(storage_config.ConfigError):
            storage_config.operating_database(self.data_root)
        self.assertFalse(self.data_root.exists())
        self.cloud.assert_not_called()

    def test_configured_local_connection_selects_supabase_and_writable_kwargs(self):
        self.write_config()
        self.assertEqual(storage_config.operating_database(self.data_root), "supabase://rolymoly")
        kwargs = storage_config.postgres_kwargs()
        self.assertEqual(kwargs["password"], self.values["password"])
        self.assertEqual(kwargs["sslmode"], "require")
        self.assertEqual(kwargs["connect_timeout"], 10)
        self.assertIsNone(kwargs["prepare_threshold"])
        self.assertTrue(kwargs["autocommit"])
        self.assertIn("default_transaction_read_only=off", kwargs["options"])
        self.assertIn("statement_timeout=10000", kwargs["options"])
        diagnostic = storage_config.load_config(self.path).connect_kwargs()
        self.assertIn("default_transaction_read_only=on", diagnostic["options"])
        self.cloud.assert_not_called()

    def test_cloud_mapping_is_used_only_without_local_file(self):
        self.cloud.return_value = MappingProxyType({
            "supabase": MappingProxyType(self.values),
        })
        self.assertEqual(storage_config.operating_database(self.data_root), "supabase://rolymoly")
        self.assertEqual(storage_config.postgres_kwargs()["host"], self.values["host"])
        self.write_config(password="synthetic-local-precedence")
        self.assertEqual(storage_config.postgres_kwargs()["password"], "synthetic-local-precedence")
        self.assertEqual(self.cloud.call_count, 2)

    def test_partially_entered_connection_does_not_fall_back(self):
        for changes in (dict(host=""), dict(user=""), dict(password="")):
            with self.subTest(field=next(iter(changes))):
                self.write_config(**changes)
                with self.assertRaises(storage_config.ConfigError) as caught:
                    storage_config.operating_database(self.data_root)
                self.assertNotIn(self.values["password"], str(caught.exception))

    def test_malformed_local_settings_do_not_fall_back_to_cloud(self):
        self.path.write_text('[supabase]\npassword = "synthetic-unclosed', encoding="utf-8")
        self.cloud.return_value = {"supabase": self.values}
        with self.assertRaises(storage_config.ConfigError) as caught:
            storage_config.operating_database(self.data_root)
        self.assertNotIn("synthetic-unclosed", str(caught.exception))
        self.cloud.assert_not_called()

    def test_invalid_cloud_values_do_not_fall_back(self):
        for values in ("synthetic-private", {"password": True}, dict(self.values, port=6543)):
            with self.subTest(kind=type(values).__name__):
                self.cloud.return_value = {"supabase": values}
                with self.assertRaises(storage_config.ConfigError):
                    storage_config.operating_database(self.data_root)

    def test_explicit_data_directory_does_not_read_connection_settings(self):
        os.environ["ROLYMOLY_DATA_DIR"] = str(self.data_root)
        with patch.object(storage_config, "_runtime_document") as load:
            self.assertEqual(storage_config.operating_database(self.data_root), self.local_target)
        load.assert_not_called()

    def test_explicit_supabase_target_overrides_data_directory(self):
        os.environ["ROLYMOLY_DATABASE_TARGET"] = "supabase://rolymoly"
        os.environ["ROLYMOLY_DATA_DIR"] = str(self.data_root)
        self.write_config()
        self.assertEqual(storage_config.operating_database(self.data_root), "supabase://rolymoly")
        self.path.unlink()
        with self.assertRaises(storage_config.ConfigError):
            storage_config.operating_database(self.data_root)

    def test_explicit_sqlite_path_is_resolved_from_launch_directory(self):
        os.environ["ROLYMOLY_DATABASE_TARGET"] = "storage/custom.sqlite3"
        with patch.object(storage_config, "_runtime_document") as load, chdir(self.directory):
            result = storage_config.operating_database(self.data_root)
        self.assertEqual(result, str(self.directory / "storage" / "custom.sqlite3"))
        self.assertFalse(Path(result).exists())
        load.assert_not_called()

    def test_absolute_sqlite_path_is_preserved(self):
        target = self.directory / "storage" / "custom"
        os.environ["ROLYMOLY_DATABASE_TARGET"] = str(target)
        self.assertEqual(storage_config.operating_database(self.data_root), str(target))

    def test_invalid_target_never_exposes_connection_values(self):
        for target in (
            "", "supabase://other", "postgresql://user:synthetic-url-secret@host/postgres",
            "sqlite:///db.sqlite3", "file:db.sqlite3", ":memory:", "bad\npath",
            "\\\\host\\share\\db.sqlite3", "//host/share/db.sqlite3", str(self.directory),
        ):
            with self.subTest(kind=target.split(":", 1)[0][:8]):
                os.environ["ROLYMOLY_DATABASE_TARGET"] = target
                with self.assertRaises(storage_config.ConfigError) as caught:
                    storage_config.operating_database(self.data_root)
                self.assertNotIn("synthetic-url-secret", str(caught.exception))

    def test_diagnostic_script_runs_outside_project_without_reading_config(self):
        script = Path(storage_config.ROOT) / "scripts" / "check_supabase.py"
        result = subprocess.run([sys.executable, "-X", "utf8", str(script), "--help"],
                                cwd=self.directory, capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check-config", result.stdout)


class CloudSecretsErrorsTests(unittest.TestCase):
    def test_absent_cloud_settings_are_distinguished_from_invalid_settings(self):
        from streamlit.errors import StreamlitSecretNotFoundError
        for category, missing in (("no-secrets-found", True), ("failed-parsing-secrets-file", False)):
            with self.subTest(category=category):
                error = StreamlitSecretNotFoundError("synthetic-private-error", error_id=category)
                secrets = SimpleNamespace(to_dict=Mock(side_effect=error))
                with patch("streamlit.secrets", secrets):
                    if missing:
                        self.assertEqual(storage_config._cloud_document(), {})
                    else:
                        with self.assertRaises(storage_config.ConfigError) as caught:
                            storage_config._cloud_document()
                        self.assertNotIn("synthetic-private-error", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
