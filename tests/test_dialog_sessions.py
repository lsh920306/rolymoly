"""Dialog entrypoint reruns with captured arguments and no outer app auth check.

AppTest reruns scripts, so this harness invokes the actual decorated dialog
directly. It preserves the original token/actor like a dialog-only rerun does.
Only disposable SQLite databases and synthetic accounts are used.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.competition import Competition
from roly.core import Core
from roly.tournament import TournamentService


DIALOG_APP = """
import streamlit as st
from roly.core import Core
from roly.competition import Competition
from roly.tournament import TournamentService
from roly.registration_ui import creation_dialog, participants_dialog
from roly.member_editor import show_member_editor
core = Core(st.session_state.test_db)
service = TournamentService(core, Competition(core))
if st.session_state.dialog_kind == 'creation':
    creation_dialog(service, st.session_state.dialog_token)
elif st.session_state.dialog_kind == 'participants':
    participants_dialog(service, st.session_state.dialog_token, st.session_state.event_id)
else:
    show_member_editor(core, st.session_state.dialog_token,
                       st.session_state.captured_actor, st.session_state.member_id)
"""


class DialogSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="roly-dialog-session-")
        self.addCleanup(temporary.cleanup)
        self.core = Core(Path(temporary.name) / "isolated.sqlite3")
        self.comp = Competition(self.core)
        self.service = TournamentService(self.core, self.comp)
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.admin = self.core.login("admin", "synthetic-admin-password")
        self.receipt = self.register("host")
        self.mid = self.receipt["member_id"]
        self.core.approve_member(self.admin, self.mid, 170, "PRIVATE-OPERATING-MEMO")
        self.token = self.core.login("host", "synthetic-member-password")

    def register(self, name):
        return self.core.register_member(name, "synthetic-member-password", f"{name}#QA", "TOP", "JG", request_key=str(uuid4()))

    def dialog(self, kind="creation", *, token=None, event_id=None):
        token = token or self.token
        app = AppTest.from_string(DIALOG_APP, default_timeout=20)
        for key, value in {"test_db": self.core.db_path, "dialog_kind": kind,
                           "dialog_token": token, "token": token, "event_id": event_id,
                           "member_id": self.mid, "captured_actor": self.core.session(token)}.items():
            app.session_state[key] = value
        app.run()
        self.healthy(app)
        return app

    def healthy(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])

    def denied(self, app):
        self.healthy(app)
        self.assertTrue(app.warning)
        self.assertFalse(app.text_input)
        self.assertFalse(app.multiselect)
        self.assertFalse(app.button)

    def create_payload(self, app):
        next(widget for widget in app.text_input if widget.label == "경매 이름").set_value("Original private draft")
        next(widget for widget in app.button if widget.label == "경매 생성").click()
        return app._tree.get_widget_states()

    def event(self):
        starts_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        event_id = self.service.create(self.token, "Session review", starts_at, build_mode="AUCTION", team_count=4)
        self.service.open_recruitment(self.token, event_id)
        return event_id

    def test_logout_rejects_captured_creation_submit_without_outer_app_rerun(self):
        app = self.dialog()
        payload = self.create_payload(app)
        self.core.logout(self.token)
        app._run(payload)
        self.denied(app)
        self.assertEqual(self.comp.list_events(), [])

    def test_role_change_invalidates_captured_creation_token_then_fresh_login_works(self):
        app = self.dialog()
        payload = self.create_payload(app)
        self.core.set_account_role(self.admin, self.receipt["account_id"], "organizer")
        app._run(payload)
        self.denied(app)
        self.assertEqual(self.comp.list_events(), [])
        fresh = self.core.login("host", "synthetic-member-password")
        app.session_state["token"] = fresh
        app.session_state["dialog_token"] = fresh
        app.run()
        self.healthy(app)
        self.assertTrue(any(widget.label == "경매 생성" for widget in app.button))

    def test_pending_login_cannot_render_creation_form(self):
        self.register("pending")
        token = self.core.login("pending", "synthetic-member-password")
        app = self.dialog(token=token)
        self.denied(app)
        self.assertNotIn("t_creation_draft", app.session_state)
        self.assertEqual(self.comp.list_events(), [])

    def test_account_switch_blocks_old_valid_token_and_opens_a_distinct_blank_draft(self):
        app = self.dialog()
        payload = self.create_payload(app)
        old_key = app.session_state["t_creation_draft"]["request_key"]
        app.session_state["token"] = self.admin
        self.assertIsNotNone(self.core.session(self.token))
        app._run(payload)
        self.denied(app)
        self.assertEqual(self.comp.list_events(), [])
        self.assertEqual(app.session_state["t_creation_draft"]["request_key"], old_key)
        app.session_state["dialog_token"] = self.admin
        app.run()
        self.healthy(app)
        self.assertNotEqual(app.session_state["t_creation_draft"]["request_key"], old_key)
        self.assertEqual(next(widget for widget in app.text_input if widget.label == "경매 이름").value, "")

    def test_kick_hides_participant_editor_and_keeps_event_unchanged(self):
        event_id = self.event()
        app = self.dialog("participants", event_id=event_id)
        self.assertTrue(app.multiselect)
        next(widget for widget in app.button if widget.label == "참가 명단 저장").click()
        payload = app._tree.get_widget_states()
        before = self.service.get_event(event_id)
        self.core.kick_member(self.admin, self.mid, "membership ended")
        app._run(payload)
        self.denied(app)
        self.assertEqual(self.service.get_event(event_id), before)

    def test_other_approved_member_cannot_render_foreign_participant_editor(self):
        event_id = self.event()
        other = self.register("other")
        self.core.approve_member(self.admin, other["member_id"], 170)
        token = self.core.login("other", "synthetic-member-password")
        app = self.dialog("participants", token=token, event_id=event_id)
        self.denied(app)
        self.assertFalse(any(key.startswith("t_roster_base_") for key in app.session_state.filtered_state))

    def test_revoked_admin_captured_actor_cannot_read_internal_notes(self):
        self.core.set_account_role(self.admin, self.receipt["account_id"], "admin")
        token = self.core.login("host", "synthetic-member-password")
        app = self.dialog("member", token=token)
        self.assertEqual(next(widget for widget in app.text_area if widget.label == "운영 메모").value, "PRIVATE-OPERATING-MEMO")
        self.core.set_account_role(self.admin, self.receipt["account_id"], "member")
        app.run()
        self.denied(app)
        self.assertFalse(app.text_area)
        # The caller still has the old admin dict; a new, lower-privilege token
        # must also be checked instead of trusting that captured dictionary.
        fresh = self.core.login("host", "synthetic-member-password")
        app.session_state["token"] = fresh
        app.session_state["dialog_token"] = fresh
        app.run()
        self.healthy(app)
        self.assertTrue(app.error)
        self.assertFalse(app.text_area)
        self.assertEqual(self.core.get_member(self.mid)["notes"], "PRIVATE-OPERATING-MEMO")


if __name__ == "__main__":
    unittest.main()
