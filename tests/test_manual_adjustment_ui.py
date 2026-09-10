"""Identical native submissions and lost replies cannot double manual awards."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.competition import Competition
from roly.core import Core
from roly.ui import services


ROOT = Path(__file__).resolve().parents[1]


class ManualAdjustmentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "manual-admin-password")
        cls.token = cls.base.login("admin", "manual-admin-password")
        Competition(cls.base)
        receipt = cls.base.register_member("member", "manual-member-password", "Manual#QA", "TOP", "JG", request_key=uuid4().hex)
        cls.mid = receipt["member_id"]
        cls.base.approve_member(cls.token, cls.mid, 100)

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        self.enterContext(native_blocks())
        temporary = TemporaryDirectory(prefix="roly-manual-ui-")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(services.clear)
        self.core = Core(Path(temporary.name) / "rolymoly.sqlite3")
        with closing(self.core.connect()) as db:
            self.base._keeper.backup(db)

    def page(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        for key, value in {"space": "운영 공간", "db_path": self.core.db_path, "token": self.token}.items():
            app.session_state[key] = value
        app.run()
        app.switch_page("app_pages/admin.py").run()
        self.healthy(app)
        return app

    def healthy(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])

    def widget(self, app, kind, label):
        found = [widget for widget in getattr(app, kind) if widget.label == label]
        self.assertEqual(len(found), 1, label)
        return found[0]

    def prepare(self, app, kind):
        app.session_state.admin_active_tab = "회원·점수" if kind == "score" else "업적 관리"
        app.run()
        self.healthy(app)
        if kind == "score":
            app.selectbox(key="admin_member_id").select(self.mid).run()
            self.widget(app, "number_input", "점수 보정량").set_value(7)
            self.widget(app, "text_input", "점수 보정 사유").set_value("동일 점수 요청 검증")
            label = "점수 보정 적용"
        else:
            self.widget(app, "multiselect", "업적을 조정할 회원").set_value([self.mid])
            self.widget(app, "text_input", "업적 조정 사유").set_value("동일 업적 요청 검증")
            label = "업적 조정 적용"
        button = self.widget(app, "button", label)
        button.click()
        # Preserve the exact native request, including its trigger and values.
        # AppTest's ordinary rerun would reset the trigger instead of replaying it.
        return app._tree.get_widget_states(), button.proto.id, label

    def balance(self, kind):
        member = self.core.get_member(self.mid)
        return member["score"] - 100 if kind == "score" else member["award_units"]

    def count(self, table):
        with self.core.read_snapshot() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_exact_score_submission_replay_adds_seven_only_once(self):
        app = self.page()
        request, old_id, label = self.prepare(app, "score")
        app._run(request)
        self.healthy(app)
        self.assertEqual(self.balance("score"), 7)
        self.assertNotEqual(self.widget(app, "button", label).proto.id, old_id)
        app._run(request)
        self.healthy(app)
        self.assertEqual(self.balance("score"), 7)
        self.assertEqual(self.count("score_ledger"), 1)
        self.assertEqual(self.count("score_adjustment_requests"), 1)
        # A new, deliberately filled form remains a new valid adjustment.
        another, _, _ = self.prepare(app, "score")
        app._run(another)
        self.healthy(app)
        self.assertEqual(self.balance("score"), 14)
        self.assertEqual(self.count("score_adjustment_requests"), 2)

    def test_exact_award_submission_replay_grants_one_only_once(self):
        app = self.page()
        request, old_id, label = self.prepare(app, "award")
        app._run(request)
        self.healthy(app)
        self.assertEqual(self.balance("award"), 1)
        self.assertNotEqual(self.widget(app, "button", label).proto.id, old_id)
        app._run(request)
        self.healthy(app)
        self.assertEqual(self.balance("award"), 1)
        self.assertEqual(self.count("award_batches"), 1)
        self.assertEqual(self.count("award_ledger"), 1)

    def test_committed_but_lost_reply_preserves_draft_and_retries_same_receipt(self):
        for kind, method in (("score", "adjust_score"), ("award", "grant_award")):
            with self.subTest(kind=kind):
                app = self.page()
                request, old_id, label = self.prepare(app, kind)
                original = getattr(Core, method)
                lost_reply = [False]

                def commit_then_lose_response(instance, *args, **kwargs):
                    result = original(instance, *args, **kwargs)
                    if not lost_reply[0]:
                        lost_reply[0] = True
                        raise sqlite3.OperationalError("synthetic lost response after commit")
                    return result

                with patch.object(Core, method, commit_then_lose_response):
                    app._run(request)
                    self.healthy(app)
                    self.assertTrue(app.error)
                    self.assertEqual(self.widget(app, "button", label).proto.id, old_id)
                    self.assertEqual(self.balance(kind), 7 if kind == "score" else 1)
                    reason_label = "점수 보정 사유" if kind == "score" else "업적 조정 사유"
                    self.assertTrue(self.widget(app, "text_input", reason_label).value)
                    if kind == "score":
                        self.assertEqual(self.widget(app, "number_input", "점수 보정량").value, 7)
                    else:
                        self.assertEqual(self.widget(app, "multiselect", "업적을 조정할 회원").value, [self.mid])
                    app._run(request)
                    self.healthy(app)
                    self.assertFalse(app.error)
                    self.assertEqual(self.balance(kind), 7 if kind == "score" else 1)
                    self.assertNotEqual(self.widget(app, "button", label).proto.id, old_id)
                    app._run(request)
                    self.healthy(app)
                    self.assertEqual(self.balance(kind), 7 if kind == "score" else 1)
                self.assertEqual(self.count("score_adjustment_requests" if kind == "score" else "award_batches"), 1)


if __name__ == "__main__":
    unittest.main()
