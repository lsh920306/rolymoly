"""Deployment selection never reads real secrets or connects to storage."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from roly import storage_config
from roly.deployment_config import (DeploymentConfig, bind_deployed_session,
                                    load_deployment_config, require_operating_target)


class DeploymentConfigTests(unittest.TestCase):
    def test_missing_mode_defaults_to_production_and_valid_modes_are_explicit(self):
        self.assertEqual(load_deployment_config({}, environ={}), DeploymentConfig("production"))
        for value in ("test", "production", "local"):
            config = load_deployment_config({"app": {"environment": value}}, environ={})
            self.assertEqual(config.allow_demo, value == "local")
            self.assertEqual(config.is_test, value == "test")

    def test_invalid_mode_and_app_shape_fail_without_echoing_values(self):
        secret = "sensitive-invalid-mode"
        for document in (None, [], {"app": []}, {"app": None},
                         *({"app": {"environment": value}} for value in (secret, "", True, 1, [], "PRODUCTION"))):
            with self.subTest(document_type=type(document).__name__), self.assertRaises(storage_config.ConfigError) as error:
                load_deployment_config(document, environ={})
            self.assertNotIn(secret, str(error.exception))

    def test_explicit_local_data_directory_skips_all_settings(self):
        with patch.object(storage_config, "_runtime_document", side_effect=AssertionError("No live settings")):
            self.assertTrue(load_deployment_config(environ={"ROLYMOLY_DATA_DIR": "isolated"}).allow_demo)
            self.assertTrue(load_deployment_config(environ={"ROLYMOLY_DATA_DIR": "isolated", "ROLYMOLY_DATABASE_TARGET": "test.sqlite3"}).allow_demo)

    def test_launcher_supabase_target_does_not_turn_deployment_into_local(self):
        with patch.object(storage_config, "_runtime_document", return_value={"app": {"environment": "test"}}) as read:
            config = load_deployment_config(environ={"ROLYMOLY_DATA_DIR": "launcher-data", "ROLYMOLY_DATABASE_TARGET": storage_config.SUPABASE_TARGET})
        self.assertTrue(config.is_test)
        read.assert_called_once_with()

    def test_process_mode_override_is_validated_before_local_exception(self):
        for value in ("test", "production", "local"):
            with patch.object(storage_config, "_runtime_document", side_effect=AssertionError("No settings needed")):
                config = load_deployment_config(environ={"ROLYMOLY_APP_ENVIRONMENT": value, "ROLYMOLY_DATA_DIR": "isolated"})
                self.assertEqual(config.environment, value)
        for value in ("", "typo", False):
            with self.assertRaises(storage_config.ConfigError):
                load_deployment_config(environ={"ROLYMOLY_APP_ENVIRONMENT": value, "ROLYMOLY_DATA_DIR": "isolated"})

    def test_local_file_and_cloud_use_the_same_validator(self):
        with TemporaryDirectory(prefix="roly-deployment-config-") as folder:
            path = Path(folder) / "temporary.toml"
            with patch.dict(os.environ, {}, clear=True), patch.object(storage_config, "DEFAULT_SECRETS", path), patch.object(
                storage_config, "_cloud_document", return_value={"app": {"environment": "test"}}
            ) as cloud:
                self.assertTrue(load_deployment_config().is_test)
                path.write_text('[app]\nenvironment = "production"\n', encoding="utf-8")
                self.assertEqual(load_deployment_config().environment, "production")
                self.assertEqual(cloud.call_count, 1)
                path.write_text('[app]\nenvironment = [', encoding="utf-8")
                with self.assertRaises(storage_config.ConfigError):
                    load_deployment_config()
                self.assertEqual(cloud.call_count, 1)

    def test_shared_deployments_reject_sqlite_targets(self):
        for mode in ("test", "production"):
            config = DeploymentConfig(mode)
            self.assertEqual(require_operating_target(config, storage_config.SUPABASE_TARGET), storage_config.SUPABASE_TARGET)
            for target in (None, "demo.sqlite3", "supabase://other"):
                with self.assertRaises(storage_config.ConfigError):
                    require_operating_target(config, target)
        self.assertEqual(require_operating_target(DeploymentConfig("local"), "isolated.sqlite3"), "isolated.sqlite3")

    def test_session_binding_clears_foreign_context_but_keeps_same_db_login(self):
        target = storage_config.SUPABASE_TARGET
        stale = {"space": "체험 공간", "db_path": "demo.sqlite3", "token": "old-token", "demo_accounts": [{"password": "old-password"}],
                 "normal_creation_draft": {"old": True}, "live_reset_review": {"old": True}, "profile_database": "demo.sqlite3"}
        self.assertTrue(bind_deployed_session(stale, target))
        self.assertEqual(stale, {"space": "운영 공간", "db_path": target, "token": None, "auth_storage_checked": True})
        normal = {"space": "운영 공간", "db_path": target, "token": "valid-current-token", "normal_creation_draft": {"valid": True},
                  "demo_token": "unrelated-old-token", "demo_accounts": [{"password": "old-password"}]}
        self.assertFalse(bind_deployed_session(normal, target))
        self.assertEqual(normal["token"], "valid-current-token")
        self.assertEqual(normal["normal_creation_draft"], {"valid": True})
        self.assertFalse(any(key.startswith("demo_") for key in normal))
        for state in ({"token": "unbound"}, {"db_path": target, "space": "운영 공간", "token": "demo", "demo_token": "demo"}):
            self.assertTrue(bind_deployed_session(state, target))
            self.assertIsNone(state["token"])

    def test_example_has_test_mode_and_updated_mastery_comment(self):
        path = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml.example"
        text = path.read_text(encoding="utf-8")
        self.assertTrue(load_deployment_config(tomllib.loads(text), environ={}).is_test)
        self.assertIn("상위 5개", text)
        self.assertIn("솔로·자유랭크", text)
