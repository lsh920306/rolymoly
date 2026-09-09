"""Public PostgreSQL login and private first-admin setup without remote access."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition
from roly.lounge import Lounge
from roly import ui
from scripts import create_admin


ROOT = Path(__file__).resolve().parents[1]


class PostgresDisplayFacade:
    """Expose PostgreSQL presentation flags while all methods stay SQLite-bound."""
    is_postgres = True
    db_path = "supabase://rolymoly"

    def __init__(self, local_core):
        self._local_core = local_core

    def __getattr__(self, name):
        return getattr(self._local_core, name)


class OperatingLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-operating-login-")
        self.addCleanup(temporary.cleanup)
        self.core = Core(Path(temporary.name) / "test.sqlite3")
        self.competition = Competition(self.core)
        Lounge(self.core)
        self.facade = PostgresDisplayFacade(self.core)
        self.assertFalse(self.core.is_postgres)
        ui.lounge_service.clear()
        self.addCleanup(ui.lounge_service.clear)
        for replacement in (
            patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temporary.name}),
            patch.object(ui, "operating_database", return_value="supabase://rolymoly"),
            patch.object(ui, "services", return_value=(self.facade, self.competition)),
            patch("roly.storage_config._runtime_document", side_effect=AssertionError("Real settings must not be read")),
            # This facade tests PostgreSQL presentation using SQLite. Riot has
            # its own imported config reader, so blocking only storage_config
            # does not isolate the external service or its cached worker.
            patch("roly.riot_ui.riot_service", return_value=None),
            patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("Riot settings are outside login tests")),
            patch("roly.riot_api._runtime_document", side_effect=AssertionError("Riot settings must not be read")),
            patch("roly.riot_api._http_get", side_effect=AssertionError("Riot HTTP is outside login tests")),
            patch("roly.riot_sync.start_worker", side_effect=AssertionError("Login fixture must not start a Riot worker")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def app(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertFalse(app.exception, [error.message for error in app.exception])
        return app

    def test_public_postgres_starts_in_operation_space_without_admin_creation(self):
        app = self.app()
        self.assertEqual(app.session_state["space"], "운영 공간")
        self.assertEqual(app.session_state["db_path"], "supabase://rolymoly")
        self.assertEqual(app.selectbox(key="space").value, "운영 공간")
        self.assertIsNone(app.session_state["token"])
        self.assertFalse(any(widget.label in ("관리자 아이디", "비밀번호 (10자 이상)", "비밀번호 확인")
                             for widget in app.text_input))
        self.assertFalse(any(button.label == "운영 시작하기" for button in app.button))
        self.assertTrue(any("최초 관리자 계정 설정 후 로그인" in message.value for message in app.info))
        self.assertFalse(self.core.has_admin())
        self.assertEqual(self.core.list_members(True), [])

    def test_existing_admin_gets_expanded_login_and_can_sign_in(self):
        self.core.setup_admin("operator", "synthetic-login-password", "테스트 운영진")
        app = self.app()
        login = next(expander for expander in app.expander if expander.label == "계정 로그인")
        self.assertTrue(login.proto.expanded)
        self.assertEqual(app.text_input(key="login_username").label, "로그인 아이디")
        self.assertEqual(app.text_input(key="login_password").label, "비밀번호")
        self.assertIsNone(app.session_state["token"])
        app.text_input(key="login_username").set_value("operator")
        app.text_input(key="login_password").set_value("synthetic-login-password")
        next(button for button in app.button if button.label == "로그인").click().run()
        self.assertFalse(app.exception, [error.message for error in app.exception])
        self.assertFalse(app.error, [error.value for error in app.error])
        actor = self.core.session(app.session_state["token"])
        self.assertEqual((actor["username"], actor["role"]), ("operator", "admin"))
        self.assertTrue(any(button.label == "로그아웃" for button in app.button))
        self.assertTrue(any("테스트 운영진 · 관리자" in message.value for message in app.caption))
        self.assertFalse(any(widget.key == "login_password" for widget in app.text_input))

    def test_reload_restores_only_a_server_validated_tab_token(self):
        self.core.setup_admin("operator", "synthetic-login-password", "테스트 운영진")
        token = self.core.login("operator", "synthetic-login-password")
        def browser_reply(**kwargs):
            return SimpleNamespace(loaded={"checked": True, "token": token, "nonce": kwargs["data"]["nonce"]})
        with patch("roly.browser_session._component", return_value=browser_reply):
            app = self.app()
            self.assertEqual(self.core.session(app.session_state["token"])["username"], "operator")
            self.core.logout(token)
            app.run()
            self.assertFalse(app.exception)
            self.assertIsNone(app.session_state["token"])
            # A new connection must also reject the same revoked browser token.
            reloaded = self.app()
            self.assertIsNone(reloaded.session_state["token"])

    def test_pending_account_can_only_view_own_application_then_becomes_member(self):
        self.core.setup_admin("operator", "synthetic-login-password", "테스트 운영진")
        admin = self.core.login("operator", "synthetic-login-password")
        receipt = self.core.register_member("new-user", "synthetic-login-password", "새가입#KR1", "TOP", "JG", request_key=uuid4().hex)
        token = self.core.login("new-user", "synthetic-login-password")
        app = self.app()
        app.session_state.token = token
        app.switch_page("app_pages/normal.py").run()
        self.assertFalse(app.exception)
        self.assertFalse(any((button.key or "").startswith("normal_create_submit_") for button in app.button))
        self.assertTrue(any("가입 승인 후" in row.value for row in app.info))
        app.switch_page("app_pages/join.py").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("가입 승인 대기" in row.value for row in app.info))
        self.core.reject_registration(admin, receipt["member_id"], "태그를 확인해 주세요")
        app.run()
        self.assertTrue(any("태그를 확인" in row.value for row in app.text))
        self.assertTrue(next(row for row in app.button if row.label == "보완하고 다시 신청").disabled)
        next(row for row in app.button if row.label == "최신 신청 정보 불러오기").click().run()
        next(row for row in app.text_input if row.label == "Riot ID").set_value("새가입#KR2")
        next(row for row in app.button if row.label == "보완하고 다시 신청").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertEqual(self.core.get_member(receipt["member_id"])["riot_id"], "새가입#KR2")
        self.core.approve_member(admin, receipt["member_id"], 200)
        app.run()
        self.assertTrue(any("가입 승인 완료" in row.value for row in app.success))
        self.assertEqual(self.core.session(token)["member_id"], receipt["member_id"])


class PrivateAdminSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-private-admin-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "test.sqlite3"
        self.core = Core(self.path)
        for replacement in (
            patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temporary.name}),
            patch.object(create_admin, "operating_database", return_value=str(self.path)),
            patch("roly.storage_config._runtime_document", side_effect=AssertionError("Real settings must not be read")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def run_cli(self, passwords, inputs=("operator", "비공개 운영진")):
        output = io.StringIO()
        with patch("builtins.input", side_effect=inputs) as prompt:
            with patch.object(create_admin.getpass, "getpass", side_effect=passwords) as secret_prompt:
                with redirect_stdout(output), redirect_stderr(output):
                    code = create_admin.main()
        return code, output.getvalue(), prompt, secret_prompt

    def account_count(self):
        with closing(self.core.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    def test_private_cli_creates_an_admin_that_can_authenticate(self):
        password = "synthetic-cli-password"
        code, output, prompt, secret_prompt = self.run_cli((password, password))
        self.assertEqual(code, 0)
        self.assertEqual((prompt.call_count, secret_prompt.call_count), (2, 2))
        self.assertEqual(self.account_count(), 1)
        token = self.core.login("operator", password)
        actor = self.core.session(token)
        self.assertEqual((actor["role"], actor["display_name"]), ("admin", "비공개 운영진"))
        self.assertNotIn(password, output)
        self.assertIn("관리자 계정을 만들었습니다", output)

    def test_existing_admin_stops_cli_before_credential_prompts(self):
        password = "synthetic-existing-password"
        self.core.setup_admin("existing", password)
        code, output, prompt, secret_prompt = self.run_cli(())
        self.assertEqual(code, 1)
        self.assertEqual(self.account_count(), 1)
        prompt.assert_not_called()
        secret_prompt.assert_not_called()
        self.assertTrue(self.core.session(self.core.login("existing", password)))
        self.assertNotIn(password, output)
        self.assertIn("이미 최초 관리자 계정", output)

    def test_password_mismatch_creates_no_account_and_prints_no_password(self):
        passwords = ("synthetic-first-password", "synthetic-other-password")
        code, output, _, secret_prompt = self.run_cli(passwords)
        self.assertEqual(code, 2)
        self.assertEqual(secret_prompt.call_count, 2)
        self.assertEqual(self.account_count(), 0)
        self.assertFalse(self.core.has_admin())
        for password in passwords:
            self.assertNotIn(password, output)
        self.assertIn("계정을 만들지 않았습니다", output)


if __name__ == "__main__":
    unittest.main()
