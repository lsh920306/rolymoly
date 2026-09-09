"""Versioned registration review on real isolated SQLite and the admin page."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition


ROOT = Path(__file__).resolve().parents[1]


class ApprovalContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-approval-context-")
        self.core = Core(Path(self.temp.name) / "isolated.sqlite3")
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.admin = self.core.login("admin", "synthetic-admin-password")
        self.comp = Competition(self.core)
        self.mid, self.member_token = self.registration("member")

    def tearDown(self):
        self.temp.cleanup()

    def registration(self, username):
        receipt = self.core.register_member(username, "synthetic-member-password", f"{username}#QA", "TOP", "JG",
                                             "original application", request_key=str(uuid4()))
        return receipt["member_id"], self.core.login(username, "synthetic-member-password")

    def test_stale_approval_and_rejection_preserve_new_application_then_fresh_approval_succeeds(self):
        old = self.core.get_member(self.mid)
        self.core.resubmit_registration(self.member_token, "Edited#QA", "MID", "AD", "new application")
        current = self.core.get_member(self.mid)
        for operation in (
            lambda: self.core.approve_member(self.admin, self.mid, 170, "stale notes", expected_updated_at=old["updated_at"]),
            lambda: self.core.reject_registration(self.admin, self.mid, "stale rejection", expected_updated_at=old["updated_at"]),
        ):
            with self.assertRaisesRegex(ValueError, "최신 신청"):
                operation()
            self.assertEqual(self.core.get_member(self.mid), current)
        self.core.approve_member(self.admin, self.mid, 230, "reviewed new application", expected_updated_at=current["updated_at"])
        approved = self.core.get_member(self.mid)
        self.assertEqual((approved["riot_id"], approved["main_role"], approved["status"], approved["base_score"], approved["notes"]),
                         ("Edited#QA", "MID", "APPROVED", 230, "reviewed new application"))

    def test_rejection_and_resubmission_advance_version_even_when_clock_is_unchanged(self):
        first = self.core.get_member(self.mid)
        with patch("roly.auth.stamp", return_value=first["updated_at"]):
            self.core.reject_registration(self.admin, self.mid, "revise identity", expected_updated_at=first["updated_at"])
            rejected = self.core.get_member(self.mid)
            self.core.resubmit_registration(self.member_token, "Reapplied#QA", "MID", "AD", "resubmitted")
            latest = self.core.get_member(self.mid)
        self.assertLess(first["updated_at"], rejected["updated_at"])
        self.assertLess(rejected["updated_at"], latest["updated_at"])
        with self.assertRaises(ValueError):
            self.core.reject_registration(self.admin, self.mid, "old review", expected_updated_at=first["updated_at"])
        self.assertEqual(latest["registration_status"], "PENDING")

    def test_concurrent_edit_and_approval_commit_only_the_reviewed_state(self):
        for index in range(4):
            mid, token = self.registration(f"race{index}")
            original = self.core.get_member(mid)
            barrier = Barrier(2)
            def approve():
                barrier.wait(timeout=10)
                try:
                    self.core.approve_member(self.admin, mid, 190, "reviewed original", expected_updated_at=original["updated_at"])
                    return "APPROVED"
                except ValueError:
                    return "STALE"
            def edit():
                barrier.wait(timeout=10)
                try:
                    self.core.resubmit_registration(token, f"Changed{index}#QA", "MID", "AD", "edited application")
                    return "EDITED"
                except ValueError:
                    return "ALREADY_APPROVED"
            with ThreadPoolExecutor(max_workers=2) as pool:
                approving, editing = pool.submit(approve), pool.submit(edit)
                result = approving.result(timeout=15), editing.result(timeout=15)
            member = self.core.get_member(mid)
            if result == ("APPROVED", "ALREADY_APPROVED"):
                self.assertEqual((member["riot_id"], member["status"], member["base_score"], member["notes"]),
                                 (original["riot_id"], "APPROVED", 190, "reviewed original"))
            else:
                self.assertEqual(result, ("STALE", "EDITED"))
                self.assertEqual((member["riot_id"], member["status"], member["base_score"], member["application_notes"]),
                                 (f"Changed{index}#QA", "PENDING", 0, "edited application"))

    @staticmethod
    def by_label(widgets, label):
        return next(widget for widget in widgets if widget.label == label)

    def admin_page(self):
        self.enterContext(patch("roly.ui.context", return_value=(self.core, self.comp, self.admin, self.core.session(self.admin))))
        app = AppTest.from_file(str(ROOT / "app_pages" / "admin.py"), default_timeout=30).run()
        app.selectbox(key="admin_pending_id").select(self.mid).run()
        self.assertFalse(list(app.exception))
        return app

    def test_admin_page_disables_stale_review_and_reload_resets_notes_and_score(self):
        app = self.admin_page()
        self.by_label(app.number_input, "승인 기본점수").set_value(170)
        self.by_label(app.text_area, "운영 메모").set_value("old admin notes")
        self.core.resubmit_registration(self.member_token, "ChangedOnAnotherTab#QA", "MID", "AD", "new applicant note")
        self.by_label(app.button, "가입 승인").click().run()
        self.assertFalse(list(app.exception))
        self.assertTrue(self.by_label(app.button, "가입 승인").disabled)
        self.assertTrue(self.by_label(app.button, "신청 거절").disabled)
        self.assertEqual(self.core.get_member(self.mid)["status"], "PENDING")
        self.assertEqual(self.core.get_member(self.mid)["application_notes"], "new applicant note")
        self.assertEqual(self.core.get_member(self.mid)["notes"], "")
        self.by_label(app.button, "최신 신청 불러오기").click().run()
        self.assertFalse(list(app.exception))
        self.assertFalse(self.by_label(app.button, "가입 승인").disabled)
        self.assertEqual(self.by_label(app.number_input, "승인 기본점수").value, 0)
        self.assertEqual(self.by_label(app.text_area, "운영 메모").value, "")
        self.by_label(app.number_input, "승인 기본점수").set_value(230)
        self.by_label(app.button, "가입 승인").click().run()
        self.assertFalse(list(app.exception))
        member = self.core.get_member(self.mid)
        self.assertEqual((member["riot_id"], member["base_score"], member["notes"], member["application_notes"]), ("ChangedOnAnotherTab#QA", 230, "", "new applicant note"))

    def test_admin_page_stale_reject_submission_does_not_reject_edited_application(self):
        app = self.admin_page()
        self.by_label(app.text_input, "거절 사유").set_value("reviewed old application")
        self.core.resubmit_registration(self.member_token, "Corrected#QA", "MID", "AD", "corrected details")
        self.by_label(app.button, "신청 거절").click().run()
        self.assertFalse(list(app.exception))
        self.assertTrue(self.by_label(app.button, "신청 거절").disabled)
        member = self.core.get_member(self.mid)
        self.assertEqual((member["status"], member["registration_status"], member["rejection_reason"]), ("PENDING", "PENDING", ""))
        self.by_label(app.button, "최신 신청 불러오기").click().run()
        self.assertEqual(self.by_label(app.text_input, "거절 사유").value, "")
        self.by_label(app.text_input, "거절 사유").set_value("reviewed latest application")
        self.by_label(app.button, "신청 거절").click().run()
        self.assertFalse(list(app.exception))
        self.assertEqual(self.core.get_member(self.mid)["registration_status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
