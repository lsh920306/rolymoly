"""Shared deployment UX and stale demo callbacks, using SQLite facades only."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly import storage_config, ui
from roly.core import Core
from roly.competition import Competition
from roly.demo import DEMO_DATA_VERSION
from roly.lounge import Lounge
from tests.test_operating_login import PostgresDisplayFacade


ROOT = Path(__file__).resolve().parents[1]


class DeploymentUITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="roly-deployment-ui-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.core = Core(self.directory / f"demo-v{DEMO_DATA_VERSION}-existing.sqlite3")
        self.core.setup_admin("operator", "synthetic-operator-password")
        self.admin = self.core.login("operator", "synthetic-operator-password")
        self.comp, self.lounge = Competition(self.core), Lounge(self.core)
        self.facade = PostgresDisplayFacade(self.core)
        self.document = {"app": {"environment": "test"}, "supabase": {
            "host": "aws-0-ap-northeast-2.pooler.supabase.com", "port": 5432, "database": "postgres",
            "user": "postgres.syntheticproject1234", "password": "synthetic-settings-password"}}
        self.addCleanup(ui.services.clear)
        self.addCleanup(ui.lounge_service.clear)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ROLYMOLY_")}
        environment.update(ROLYMOLY_DATA_DIR=temporary.name, ROLYMOLY_DATABASE_TARGET=storage_config.SUPABASE_TARGET)
        def service(path):
            return (self.facade if path == storage_config.SUPABASE_TARGET else self.core), self.comp
        for replacement in (
            patch.dict(os.environ, environment, clear=True),
            patch.object(storage_config, "_runtime_document", return_value=self.document),
            patch.object(ui, "services", side_effect=service),
            patch.object(ui, "lounge_service", return_value=self.lounge),
            patch("roly.demo.seed", side_effect=AssertionError("Deployment must not seed demo data")),
            patch("roly.riot_ui.riot_service", return_value=None),
            patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No live Riot settings")),
            patch("roly.riot_api._http_get", side_effect=AssertionError("No HTTP")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def app(self, state=None):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        for key, value in (state or {}).items():
            app.session_state[key] = value
        return self.healthy(app.run())

    def healthy(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])
        return app

    def assert_deployed(self, app, *, test=True):
        self.assertEqual(app.session_state["space"], "운영 공간")
        self.assertEqual(app.session_state["db_path"], storage_config.SUPABASE_TARGET)
        self.assertFalse(any(widget.label in ("Workspace", "체험 계정") for widget in app.selectbox))
        self.assertFalse(any("체험" in widget.label for widget in app.button))
        self.assertEqual(any("검수용" in row.value for row in app.markdown), test)

    def test_test_mode_uses_shared_personal_signup_and_other_browser_login(self):
        app = self.app()
        self.assert_deployed(app)
        app.switch_page("app_pages/join.py").run()
        self.healthy(app)
        fields = {widget.label: widget for widget in app.text_input}
        fields["사용할 로그인 아이디"].set_value("shared-member")
        fields["사용할 비밀번호 (10자 이상)"].set_value("synthetic-member-password")
        fields["비밀번호 확인"].set_value("synthetic-member-password")
        fields["Riot ID"].set_value("SharedMember#QA")
        app.checkbox[0].check()
        next(widget for widget in app.button if widget.label == "회원가입").click().run()
        self.healthy(app)
        self.assertEqual(len(self.core.list_members(True)), 1)
        member = self.core.list_members(True)[0]
        self.core.approve_member(self.admin, member["id"], 170)
        other = self.app()
        other.text_input(key="login_username").set_value("shared-member")
        other.text_input(key="login_password").set_value("synthetic-member-password")
        next(widget for widget in other.button if widget.label == "로그인").click().run()
        self.healthy(other)
        self.assert_deployed(other)
        self.assertEqual(self.core.session(other.session_state["token"])["member_id"], member["id"])
        self.assertEqual(len(list(self.directory.glob("*.sqlite3"))), 1)

    def test_production_preserves_same_db_login_and_removes_demo_credentials(self):
        self.document["app"]["environment"] = "production"
        app = self.app({"space": "운영 공간", "db_path": storage_config.SUPABASE_TARGET, "token": self.admin,
                        "normal_creation_draft": {"current": True}, "demo_token": "old-demo-token",
                        "demo_accounts": [{"username": "old", "password": "old-password"}]})
        self.assert_deployed(app, test=False)
        self.assertEqual(app.session_state["token"], self.admin)
        self.assertEqual(app.session_state["normal_creation_draft"], {"current": True})
        self.assertNotIn("demo_token", app.session_state)
        self.assertNotIn("demo_accounts", app.session_state)
        self.assertIsNotNone(self.core.session(self.admin))

    def test_stale_demo_token_database_and_forms_are_cleared_before_shared_access(self):
        stale = {"space": "체험 공간", "db_path": "never-open-demo.sqlite3", "token": "old-demo-token",
                 "demo_token": "old-demo-token", "demo_id": "old", "demo_accounts": [{"password": "old-password"}],
                 "home_dialog": "create", "normal_creation_draft": {"old": True}, "t_creation_draft": {"old": True},
                 "live_reset_review": {"old": True}, "_clan_profile_editor": {"member_id": 1}, "profile_database": "old"}
        with patch.object(self.core, "session", wraps=self.core.session) as session:
            app = self.app(stale)
        self.assert_deployed(app)
        self.assertIsNone(app.session_state["token"])
        self.assertTrue(app.session_state["auth_storage_checked"])
        self.assertTrue(all(call.args[0] != "old-demo-token" for call in session.call_args_list))
        for key in stale.keys() - {"space", "db_path", "token"}:
            self.assertNotIn(key, app.session_state)
        self.assertEqual(self.comp.list_events(), [])
        self.assertEqual(self.core.list_members(True), [])

    def test_invalid_or_missing_shared_settings_stops_before_services(self):
        for invalid in ("invalid-environment-secret", None):
            with self.subTest(invalid=invalid is None):
                self.document["app"]["environment"] = invalid or "production"
                self.document["supabase"]["host"] = "" if invalid is None else "aws-0-ap-northeast-2.pooler.supabase.com"
                with patch.object(ui, "services", side_effect=AssertionError("Must stop before storage")) as service:
                    app = self.app({"space": "체험 공간", "token": "old-demo-token", "normal_creation_draft": {"old": True}})
                self.assertTrue(app.error)
                self.assertFalse(app.selectbox)
                self.assertIsNone(app.session_state["token"])
                self.assertNotIn("normal_creation_draft", app.session_state)
                self.assertNotIn("invalid-environment-secret", " ".join(row.value for row in app.error))
                service.assert_not_called()

    def test_old_demo_account_callback_rechecks_current_deployment_mode(self):
        self.document["app"]["environment"] = "local"
        app = self.app({"space": "체험 공간", "demo_id": "existing", "db_path": self.core.db_path,
                        "token": self.admin, "demo_token": self.admin,
                        "demo_accounts": [{"username": "operator", "password": "synthetic-operator-password", "label": "Demo operator"},
                                          {"username": "old-other", "password": "never-use-password", "label": "Demo other"}]})
        self.assertEqual(app.selectbox(key="demo_account_choice").value, "operator")
        self.document["app"]["environment"] = "production"
        with patch.object(self.core, "login", side_effect=AssertionError("Old demo callback must not authenticate")) as login:
            app.selectbox(key="demo_account_choice").select("old-other").run()
        self.healthy(app)
        self.assert_deployed(app, test=False)
        self.assertIsNone(app.session_state["token"])
        login.assert_not_called()
