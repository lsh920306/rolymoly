"""Regression for membership removal permanently revoking old login sessions."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from roly.core import Core


class MembershipSessionTests(unittest.TestCase):
    def test_session_lookup_does_not_reserve_writer_and_observes_logout(self):
        with tempfile.TemporaryDirectory(prefix="roly-auth-read-") as folder:
            core = Core(Path(folder) / "test.sqlite3")
            core.setup_admin("admin", "security-review-password")
            token = core.login("admin", "security-review-password")
            original_connect = core.connect
            entered, release = threading.Event(), threading.Event()

            def connect():
                db = original_connect()
                def trace(sql):
                    if "FROM sessions s JOIN accounts a" in sql:
                        entered.set()
                        release.wait(timeout=5)
                db.set_trace_callback(trace)
                return db

            with patch.object(core, "connect", side_effect=connect), ThreadPoolExecutor(max_workers=2) as pool:
                reader = pool.submit(core.session, token)
                self.assertTrue(entered.wait(timeout=2))
                try:
                    # Authentication-only reads must not hold the writer lock
                    # while waiting; revocation can commit before this SELECT.
                    pool.submit(core.logout, token).result(timeout=2)
                finally:
                    release.set()
                self.assertIsNone(reader.result(timeout=2))
            self.assertIsNone(core.session(token))

    def test_reapproval_requires_new_login_and_preserves_unrelated_sessions(self):
        core = Core(":memory:")
        try:
            core.setup_admin("admin", "security-review-password")
            admin = core.login("admin", "security-review-password")
            member_id = core.join_member("SessionReview#KR1", "TOP", "JG")
            core.approve_member(admin, member_id, 100)
            core.create_account(admin, "member", "member-review-password", role="member", member_id=member_id)
            old_sessions = [core.login("member", "member-review-password") for _ in range(2)]
            self.assertTrue(all(core.session(token) for token in old_sessions))
            core.kick_member(admin, member_id, "클랜 탈퇴 확인")
            self.assertTrue(all(core.session(token) is None for token in old_sessions))
            with self.assertRaises(PermissionError):
                core.login("member", "member-review-password")
            self.assertIsNotNone(core.session(admin))
            core.restore_member(admin, member_id, "회원 복귀")
            self.assertTrue(all(core.session(token) is None for token in old_sessions))
            renewed = core.login("member", "member-review-password")
            self.assertEqual(core.session(renewed)["member_id"], member_id)
        finally:
            core._keeper.close()


if __name__ == "__main__":
    unittest.main()
