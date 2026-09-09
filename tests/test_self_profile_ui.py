"""Personal nickname editing uses the existing account and no Riot requests."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from roly.member_ranks import save_riot_profile
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.competition import Competition
from roly.core import Core
from roly.lounge import Lounge
from roly.ui import services, lounge_service


ROOT = Path(__file__).resolve().parents[1]
DIALOG_APP = """
import streamlit as st
from roly.core import Core
from roly.self_profile_ui import nickname_dialog
core = Core(st.session_state.original_database)
nickname_dialog(core, st.session_state.original_token, st.session_state.member_id)
"""


class SelfProfileUITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="roly-self-profile-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "members.sqlite3"
        self.core = Core(self.path)
        Competition(self.core)
        Lounge(self.core)
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.admin = self.core.login("admin", "synthetic-admin-password")
        self.mid = self.register("personal", "Original#QA")
        self.core.approve_member(self.admin, self.mid, 170, "PRIVATE-OPERATOR-NOTE")
        self.core.update_member(self.admin, self.mid, "Original#QA", "TOP", "JG", 170,
                                "fixture", clan_tier="클랜 골드")
        self.token = self.core.login("personal", "synthetic-member-password")
        services.clear()
        lounge_service.clear()
        self.addCleanup(services.clear)
        self.addCleanup(lounge_service.clear)
        for replacement in (
            patch.dict(os.environ, {"ROLYMOLY_APP_ENVIRONMENT": "local", "ROLYMOLY_DATA_DIR": temporary.name,
                                    "ROLYMOLY_DATABASE_TARGET": str(self.path)}),
            patch("roly.riot_ui.riot_service", return_value=None),
            patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No Riot settings")),
            patch("roly.riot_api._http_get", side_effect=AssertionError("No HTTP")),
            patch("roly.riot_sync.start_worker", side_effect=AssertionError("No Riot worker")),
            patch("roly.storage_config._runtime_document", side_effect=AssertionError("No real database settings")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def register(self, username, riot_id):
        return self.core.register_member(username, "synthetic-member-password", riot_id, "TOP", "JG",
                                         request_key=uuid4().hex)["member_id"]

    def healthy(self, app):
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def widget(self, app, kind, label):
        matches = [item for item in getattr(app, kind) if item.label == label]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def page(self, name="profile", *, token=None, member_id=None):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        for key, value in {"space": "운영 공간", "db_path": self.core.db_path,
                           "token": self.token if token is None else token}.items():
            app.session_state[key] = value
        self.healthy(app.run())
        if name == "profile":
            app.query_params["member"] = str(self.mid if member_id is None else member_id)
        return self.healthy(app.switch_page(f"app_pages/{name}.py").run())

    def dialog(self):
        app = AppTest.from_string(DIALOG_APP, default_timeout=30)
        for key, value in {"original_database": self.core.db_path, "db_path": self.core.db_path,
                           "original_token": self.token, "token": self.token, "member_id": self.mid}.items():
            app.session_state[key] = value
        return self.healthy(app.run())

    def prepare_submit(self, app, name="Renamed#QA"):
        self.widget(app, "text_input", "새 Riot ID").set_value(name)
        self.widget(app, "button", "닉네임 저장").click()
        return app._tree.get_widget_states()

    def test_own_profile_rename_keeps_login_scores_roles_and_same_destination_without_http(self):
        before = self.core.get_member(self.mid)
        payload = {"current_tier": "골드 2", "lp": 37, "profile_icon_url": "", "champions": [],
                   "updated_at": "2026-09-09T00:00:00+00:00", "puuid": "PRIVATE-PUUID"}
        with self.core.transaction() as db:
            save_riot_profile(db, self.mid, before["canonical_id"], payload, 100.0)
        app = self.page()
        self.widget(app, "button", "닉네임 변경").click().run()
        self.healthy(app)
        self.assertEqual([field.label for field in app.text_input], ["새 Riot ID"])
        self.assertFalse(any(field.label in ("주 포지션", "부 포지션", "현재 티어") for field in app.selectbox))
        self.assertFalse(app.number_input)
        self.assertNotIn("PRIVATE-OPERATOR-NOTE", " ".join(item.value for item in app.text))
        # AppTest reruns the entrypoint instead of dispatching an existing
        # dialog fragment. Invoke the same decorated dialog with captured
        # arguments, then perform the full page rerun requested on success.
        dialog = self.dialog()
        self.prepare_submit(dialog)
        self.healthy(dialog.run())
        self.healthy(app.run())
        after = self.core.get_member(self.mid)
        self.assertEqual(after["riot_id"], "Renamed#QA")
        for key in ("id", "main_role", "sub_role", "clan_tier", "base_score", "score", "wins", "losses", "notes"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual((after["current_tier"], after["current_tier_lp"]), ("", None))
        actor = self.core.session(app.session_state["token"])
        self.assertEqual((actor["username"], actor["member_id"], actor["display_name"]), ("personal", self.mid, "Renamed#QA"))
        self.assertEqual(app.session_state["token"], self.token)
        self.assertEqual(app.query_params["member"], [str(self.mid)])
        header = " ".join(item.proto.body for item in app.get("html"))
        self.assertIn("Renamed", header)
        self.assertNotIn("PRIVATE-PUUID", header)
        self.assertFalse(any(item.label == "새 Riot ID" for item in app.text_input))
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM riot_profiles").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM riot_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM members").fetchone()[0], 1)

    def test_account_page_opens_same_editor_and_keeps_original_login_id(self):
        app = self.page("join")
        self.widget(app, "button", "닉네임 변경").click().run()
        self.healthy(app)
        dialog = self.dialog()
        self.prepare_submit(dialog, "AccountPage#QA")
        self.healthy(dialog.run())
        self.healthy(app.run())
        self.assertEqual(self.core.get_member(self.mid)["riot_id"], "AccountPage#QA")
        text = " ".join(item.value for item in app.markdown)
        self.assertIn("로그인 아이디 · personal", text)
        self.assertIn("AccountPage#QA", text)
        self.assertEqual(app.session_state["token"], self.token)

    def test_foreign_profile_and_legacy_or_pending_accounts_have_no_personal_rename_button(self):
        other = self.register("other", "Other#QA")
        self.core.approve_member(self.admin, other, 170)
        for token, member_id in ((self.token, other), (self.admin, self.mid)):
            app = self.page(token=token, member_id=member_id)
            self.assertFalse(any(item.label == "닉네임 변경" for item in app.button))
            if token == self.admin:
                self.assertTrue(any(item.label == "회원 정보 수정" for item in app.button))
        pending = self.register("pending", "Pending#QA")
        pending_token = self.core.login("pending", "synthetic-member-password")
        app = self.page("join", token=pending_token)
        self.assertFalse(any(item.label == "닉네임 변경" for item in app.button))
        self.assertEqual(self.core.get_member(pending)["status"], "PENDING")

    def test_duplicate_identity_and_unchanged_input_do_not_create_a_second_member_or_change_version(self):
        other = self.register("other", "Other#QA")
        before = self.core.get_member(self.mid)
        app = self.dialog()
        self.prepare_submit(app, "Other#QA")
        self.healthy(app.run())
        self.assertTrue(app.error)
        self.assertEqual(self.core.get_member(self.mid), before)
        self.prepare_submit(app, "Original#QA")
        self.healthy(app.run())
        self.assertFalse(app.error)
        self.assertEqual(self.core.get_member(self.mid), before)
        self.assertEqual(len(self.core.list_members(True)), 2)
        self.assertEqual(self.core.get_member(other)["riot_id"], "Other#QA")

    def test_stale_admin_edit_blocks_old_submit_and_reload_uses_the_new_version(self):
        app = self.dialog()
        payload = self.prepare_submit(app, "StaleDraft#QA")
        before = self.core.get_member(self.mid)
        self.core.update_member(self.admin, self.mid, "AdminLatest#QA", "MID", "SUP", 180,
                                "admin correction", clan_tier="클랜 다이아", expected_updated_at=before["updated_at"])
        self.healthy(app._run(payload))
        self.assertTrue(app.warning)
        self.assertTrue(self.widget(app, "button", "닉네임 저장").disabled)
        self.assertEqual(self.core.get_member(self.mid)["riot_id"], "AdminLatest#QA")
        self.widget(app, "button", "최신 닉네임 불러오기").click().run()
        self.healthy(app)
        self.assertEqual(self.widget(app, "text_input", "새 Riot ID").value, "AdminLatest#QA")
        self.assertFalse(self.widget(app, "button", "닉네임 저장").disabled)
        self.prepare_submit(app, "FreshDraft#QA")
        self.healthy(app.run())
        member = self.core.get_member(self.mid)
        self.assertEqual((member["riot_id"], member["main_role"], member["clan_tier"], member["score"]),
                         ("FreshDraft#QA", "MID", "클랜 다이아", 180))

    def test_captured_dialog_submit_rechecks_account_database_logout_and_kick(self):
        for change in ("account", "database", "logout", "kick"):
            with self.subTest(change=change):
                self.token = self.core.login("personal", "synthetic-member-password")
                app = self.dialog()
                payload = self.prepare_submit(app)
                if change == "account":
                    app.session_state.token = self.admin
                elif change == "database":
                    app.session_state.db_path = "different-database.sqlite3"
                elif change == "logout":
                    self.core.logout(self.token)
                else:
                    self.core.kick_member(self.admin, self.mid, "membership ended")
                self.healthy(app._run(payload))
                self.assertTrue(app.warning)
                self.assertFalse(app.text_input)
                self.assertFalse(app.button)
                self.assertEqual(self.core.get_member(self.mid)["riot_id"], "Original#QA")
