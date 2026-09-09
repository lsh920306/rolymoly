"""Personal membership, staff permissions and private notes share one identity."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest
from roly.core import Core
from roly.competition import Competition

ROOT = Path(__file__).resolve().parents[1]


class AccountLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="roly-account-lifecycle-")
        self.addCleanup(temp.cleanup)
        self.core = Core(Path(temp.name) / "isolated.sqlite3")
        self.comp = Competition(self.core)
        self.bootstrap_id = self.core.setup_admin("admin", "synthetic-admin-password")
        self.admin = self.core.login("admin", "synthetic-admin-password")
        self.receipt = self.signup("member")
        self.mid = self.receipt["member_id"]
        self.member_token = self.core.login("member", "synthetic-member-password")

    def signup(self, username):
        return self.core.register_member(username, "synthetic-member-password", f"{username}#QA", "TOP", "JG", "applicant message", request_key=str(uuid4()))

    def approve(self, receipt=None):
        self.core.approve_member(self.admin, (receipt or self.receipt)["member_id"], 170, "PRIVATE-ADMIN-NOTE")

    def promote(self, receipt=None):
        receipt = receipt or self.receipt
        self.approve(receipt)
        self.core.set_account_role(self.admin, receipt["account_id"], "admin")

    def test_rejected_kicked_member_returns_to_same_rejected_application(self):
        self.core.reject_registration(self.admin, self.mid, "please revise")
        self.core.kick_member(self.admin, self.mid, "withdrawal")
        kicked = self.core.get_member(self.mid)
        with self.assertRaises(ValueError):
            self.core.approve_member(self.admin, self.mid, 170)
        restored = self.core.restore_member(self.admin, self.mid, "returned", expected_updated_at=kicked["updated_at"])
        self.assertEqual((restored["status"], restored["registration_status"], restored["rejection_reason"]), ("PENDING", "REJECTED", "please revise"))
        token = self.core.login("member", "synthetic-member-password")
        self.assertEqual(self.core.session(token)["id"], self.receipt["account_id"])
        self.assertEqual(self.core.resubmit_registration(token, "Revised#QA", "MID", "AD", "revised", expected_updated_at=restored["updated_at"]), self.mid)
        self.approve()
        self.assertEqual(self.core.get_member(self.mid)["status"], "APPROVED")

    def test_approved_restore_preserves_scores_notes_and_base_history(self):
        self.approve()
        self.core.adjust_score(self.admin, self.mid, 7, "review", request_key=str(uuid4()))
        before = self.core.get_member(self.mid)
        self.core.kick_member(self.admin, self.mid, "pause", expected_updated_at=before["updated_at"])
        with self.assertRaises(ValueError):
            self.core.restore_member(self.admin, self.mid, "stale", expected_updated_at=before["updated_at"])
        restored = self.core.restore_member(self.admin, self.mid, "return")
        for field in ("status", "score", "base_score", "notes", "application_notes", "registration_status"):
            self.assertEqual(restored[field], before[field])
        self.assertIsNone(self.core.session(self.member_token))
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM base_history WHERE member_id=?", (self.mid,)).fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.core.restore_member(self.admin, self.mid, "duplicate")

    def test_personal_admin_link_stays_fixed_and_kick_blocks_all_access(self):
        with self.assertRaises(ValueError):
            self.core.set_account_role(self.admin, self.receipt["account_id"], "admin")
        self.promote()
        token = self.core.login("member", "synthetic-member-password")
        with self.core.read_snapshot() as db:
            self.assertEqual(self.core.require_admin(db, token)["member_id"], self.mid)
        with self.assertRaises(ValueError):
            self.core.link_account_member(self.admin, self.receipt["account_id"], None, "relink")
        recovery = self.core.issue_password_reset(self.admin, self.receipt["account_id"])
        self.core.kick_member(self.admin, self.mid, "membership ended")
        self.assertIsNone(self.core.session(token))
        with self.assertRaises(PermissionError):
            self.core.login("member", "synthetic-member-password")
        with self.assertRaises(PermissionError):
            self.core.reset_password(recovery["token"], "replacement-password")
        with self.assertRaises(ValueError):
            self.core.issue_password_reset(self.admin, self.receipt["account_id"])
        self.core.restore_member(self.admin, self.mid, "reinstated")
        restored = self.core.session(self.core.login("member", "synthetic-member-password"))
        self.assertEqual((restored["role"], restored["member_id"]), ("admin", self.mid))

    def test_last_personal_admin_cannot_be_kicked_or_downgraded(self):
        self.promote()
        token = self.core.login("member", "synthetic-member-password")
        self.core.set_account_role(token, self.bootstrap_id, "organizer", active=False)
        before = self.core.get_member(self.mid)
        for call in (
            lambda: self.core.kick_member(token, str(self.mid), "self withdrawal"),
            lambda: self.core.set_account_role(token, self.receipt["account_id"], "member"),
            lambda: self.core.set_account_role(token, self.receipt["account_id"], "admin", active=False),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.core.get_member(self.mid), before)
        self.assertTrue(self.core.has_admin())
        self.assertIsNotNone(self.core.session(token))

    def test_concurrent_admin_kicks_leave_one_active_admin(self):
        second = self.signup("other")
        self.promote()
        self.promote(second)
        one_token = self.core.login("member", "synthetic-member-password")
        two_token = self.core.login("other", "synthetic-member-password")
        self.core.set_account_role(one_token, self.bootstrap_id, "organizer", active=False)
        barrier = Barrier(2)
        def kick(token, target):
            barrier.wait(timeout=10)
            try:
                self.core.kick_member(token, target, "concurrent removal")
                return True
            except (PermissionError, ValueError):
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(kick, one_token, second["member_id"])
            two = pool.submit(kick, two_token, self.mid)
            self.assertEqual(sum((one.result(timeout=20), two.result(timeout=20))), 1)
        with self.core.read_snapshot() as db:
            self.assertEqual(len(self.core._active_admins(db)), 1)

    def test_legacy_relink_revokes_recovery_and_synchronizes_display(self):
        first = self.core.join_member("First#QA", "TOP", "JG")
        second = self.core.join_member("Second#QA", "TOP", "JG")
        for member_id in (first, second):
            self.core.approve_member(self.admin, member_id, 100)
        account = self.core.create_account(self.admin, "issued", "synthetic-issued-password", "First#QA", "member", member_id=first)
        recovery = self.core.issue_password_reset(self.admin, account)
        self.core.link_account_member(self.admin, account, second, "correct old account")
        with self.assertRaises(PermissionError):
            self.core.reset_password(recovery["token"], "replacement-password")
        actor = self.core.session(self.core.login("issued", "synthetic-issued-password"))
        self.assertEqual((actor["member_id"], actor["display_name"]), (second, "Second#QA"))

    def test_private_notes_are_neither_exposed_nor_overwritten_by_applicant(self):
        member = self.core.get_member(self.mid)
        self.assertEqual((member["notes"], member["application_notes"]), ("", "applicant message"))
        self.core.update_member(self.admin, self.mid, member["riot_id"], "TOP", "JG", 0, "internal assessment", "PRIVATE-ADMIN-NOTE")
        own = self.core.get_own_member(self.member_token)
        self.assertNotIn("notes", own)
        self.assertNotIn("PRIVATE-ADMIN-NOTE", str(own))
        self.core.resubmit_registration(self.member_token, "Edited#QA", "MID", "AD", "new public message", expected_updated_at=own["updated_at"])
        member = self.core.get_member(self.mid)
        self.assertEqual((member["notes"], member["application_notes"]), ("PRIVATE-ADMIN-NOTE", "new public message"))

    def test_lifecycle_audit_failure_rolls_back_member_sessions_and_recovery(self):
        self.approve()
        recovery = self.core.issue_password_reset(self.admin, self.receipt["account_id"])
        token = self.core.login("member", "synthetic-member-password")
        before = self.core.get_member(self.mid)
        with patch.object(self.core, "_audit", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaises(RuntimeError):
                self.core.kick_member(self.admin, self.mid, "failed removal")
        self.assertEqual(self.core.get_member(self.mid), before)
        self.assertIsNotNone(self.core.session(token))
        self.core.reset_password(recovery["token"], "replacement-password")
        self.core.kick_member(self.admin, self.mid, "pause")
        kicked = self.core.get_member(self.mid)
        with patch.object(self.core, "_audit", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaises(RuntimeError):
                self.core.restore_member(self.admin, self.mid, "failed return")
        self.assertEqual(self.core.get_member(self.mid), kicked)

    def test_kick_preview_lists_active_captain_events_and_audits_impact(self):
        self.approve()
        members = [self.mid]
        for index in range(19):
            member_id = self.core.join_member(f"Auction{index}#QA", "TOP", "JG")
            self.core.approve_member(self.admin, member_id, 100)
            members.append(member_id)
        event = self.comp.create_auction(self.member_token, members, members[:4], "Affected auction")
        impact = self.core.member_active_events(self.admin, self.mid)
        self.assertEqual([(row["id"], row["is_captain"]) for row in impact], [(event, 1)])
        with self.assertRaises(PermissionError):
            self.core.member_active_events(self.member_token, self.mid)
        app = self.page("admin", self.admin)
        app.selectbox(key="admin_member_id").select(self.mid).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("진행 중인 내전·경매" in warning.value for warning in app.warning))
        tables = [item.value for item in app.dataframe if "팀장" in item.value.columns]
        self.assertTrue(any("팀장" in list(table["팀장"]) for table in tables))
        self.core.kick_member(self.admin, self.mid, "reviewed impact")
        with self.core.read_snapshot() as db:
            details = db.execute("SELECT details FROM audit WHERE action='MEMBER_KICK' AND target=?", (str(self.mid),)).fetchone()[0]
        self.assertIn("Affected auction", details)

    def test_kick_rejects_stale_member_review_after_profile_change(self):
        self.approve()
        app = self.page("admin", self.admin)
        app.selectbox(key="admin_member_id").select(self.mid).run()
        self.widget(app, "text_input", "탈퇴 처리 사유").set_value("reviewed old member")
        member = self.core.get_member(self.mid)
        self.core.update_member(self.admin, self.mid, "Changed#QA", "TOP", "JG", 170, "concurrent profile edit")
        with self.assertRaises(ValueError):
            self.core.kick_member(self.admin, self.mid, "stale", expected_updated_at=member["updated_at"])
        self.widget(app, "button", "탈퇴 처리").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(self.widget(app, "button", "탈퇴 처리").disabled)
        self.assertEqual(self.core.get_member(self.mid)["status"], "APPROVED")

    @staticmethod
    def widget(app, kind, label):
        return next(item for item in getattr(app, kind) if item.label == label)

    def page(self, name, token):
        self.enterContext(patch("roly.ui.context", side_effect=lambda: (self.core, self.comp, token, self.core.session(token))))
        app = AppTest.from_file(str(ROOT / "app_pages" / f"{name}.py"), default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def test_account_page_never_renders_internal_pending_notes(self):
        member = self.core.get_member(self.mid)
        self.core.update_member(self.admin, self.mid, member["riot_id"], "TOP", "JG", 0, "internal assessment", "PRIVATE-ADMIN-NOTE")
        app = self.page("join", self.member_token)
        self.assertFalse(app.text_area)
        self.assertNotIn("PRIVATE-ADMIN-NOTE", str(app))
        self.widget(app, "text_input", "Riot ID").set_value("UpdatedApplicant#QA")
        self.widget(app, "button", "신청 정보 수정").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.text_area)
        self.assertNotIn("PRIVATE-ADMIN-NOTE", str(app))
        member = self.core.get_member(self.mid)
        self.assertEqual(member["riot_id"], "UpdatedApplicant#QA")
        self.assertEqual((member["application_notes"], member["notes"]), ("applicant message", "PRIVATE-ADMIN-NOTE"))

    def test_admin_ui_promotes_existing_personal_account_without_issuing_or_relinking(self):
        self.approve()
        app = self.page("admin", self.admin)
        labels = [item.label for item in app.button] + [item.label for item in app.selectbox]
        self.assertNotIn("운영 계정 생성", labels)
        self.assertNotIn("회원 연결 변경", labels)
        app.selectbox(key="admin_account_id").select(self.receipt["account_id"]).run()
        self.widget(app, "selectbox", "변경할 권한").select("admin")
        self.widget(app, "button", "운영 계정 변경 저장").click().run()
        self.assertFalse(app.exception)
        actor = self.core.session(self.core.login("member", "synthetic-member-password"))
        self.assertEqual((actor["role"], actor["member_id"]), ("admin", self.mid))

    def test_admin_ui_restores_rejected_member_after_reviewing_current_state(self):
        self.core.reject_registration(self.admin, self.mid, "revise")
        self.core.kick_member(self.admin, self.mid, "withdrawal")
        app = self.page("admin", self.admin)
        app.selectbox(key="admin_member_id").select(self.mid).run()
        self.widget(app, "text_input", "복귀 사유").set_value("welcome back")
        self.widget(app, "button", "회원 복귀 처리").click().run()
        self.assertFalse(app.exception)
        member = self.core.get_member(self.mid)
        self.assertEqual((member["status"], member["registration_status"]), ("PENDING", "REJECTED"))


if __name__ == "__main__":
    unittest.main()
