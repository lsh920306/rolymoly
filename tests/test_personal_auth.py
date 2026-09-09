"""Personal account lifecycle and transaction boundaries on isolated SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from roly.core import Core, ROLES, role
from roly.competition import Competition
from roly import auth


class PersonalAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-personal-auth-")
        self.core = Core(Path(self.temp.name) / "isolated.sqlite3")
        self.core.setup_admin("admin", "admin-password-123")
        self.admin = self.core.login("admin", "admin-password-123")

    def tearDown(self):
        self.temp.cleanup()

    def signup(self, username="player", riot_id="Player#KR1", **kwargs):
        return self.core.register_member(username, "personal-password-123", riot_id, "TOP", "JG",
                                         request_key=kwargs.pop("request_key", str(uuid.uuid4())), **kwargs)

    def counts(self):
        with self.core.read_snapshot() as db:
            return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("accounts", "members", "registration_requests"))

    def test_signup_is_atomic_retryable_and_payload_bound(self):
        key = str(uuid.uuid4())
        first = self.signup(request_key=key)
        self.assertEqual(self.signup(request_key=key), first)
        self.assertEqual(self.counts(), (2, 1, 1))
        for call in (
            lambda: self.signup("player", "Other#KR1"),
            lambda: self.signup("other", "Player#KR1"),
            lambda: self.signup("other", "Other#KR1", request_key=key),
            lambda: self.core.register_member("player", "different-password", "Player#KR1", "TOP", "JG", request_key=key),
            lambda: self.signup(request_key="not-a-uuid"),
        ):
            with self.assertRaises(ValueError):
                call()
            self.assertEqual(self.counts(), (2, 1, 1))
        with self.core.read_snapshot() as db:
            stored = str([dict(r) for r in db.execute("SELECT * FROM registration_requests")])
            stored += str([dict(r) for r in db.execute("SELECT * FROM audit")])
            self.assertNotIn("personal-password-123", stored)
            self.assertNotIn("different-password", stored)

    def test_simultaneous_signup_reuses_one_account_and_member(self):
        key = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.signup(request_key=key), range(4)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.counts(), (2, 1, 1))

    def test_admin_bootstrap_required_without_partial_registration(self):
        empty = Core(Path(self.temp.name) / "empty.sqlite3")
        with self.assertRaises(PermissionError):
            empty.register_member("player", "personal-password-123", "Player#KR1", "TOP", "JG", request_key=str(uuid.uuid4()))
        with empty.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM members").fetchone()[0], 0)
        empty.setup_admin("firstadmin", "admin-password-123")
        self.assertTrue(empty.has_admin())

    def test_pending_rejection_resubmission_approval_reuses_identity_and_session(self):
        registration = self.signup()
        token = self.core.login("player", "personal-password-123")
        actor = self.core.session(token)
        self.assertEqual((actor["role"], actor["member_status"], actor["registration_status"]), ("member", "PENDING", "PENDING"))
        with self.core.read_snapshot() as db:
            self.core.require_member(db, token, approved=False)
            for action in (lambda: self.core.require_member(db, token), lambda: self.core.require_event_manager(db, token), lambda: self.core.require_staff(db, token)):
                with self.assertRaises(PermissionError):
                    action()
        self.core.reject_registration(self.admin, registration["member_id"], "태그를 확인해주세요")
        self.assertEqual(self.core.session(token)["registration_status"], "REJECTED")
        self.assertEqual(self.core.get_member(registration["member_id"])["rejection_reason"], "태그를 확인해주세요")
        with self.assertRaises(ValueError):
            self.core.approve_member(self.admin, registration["member_id"], 100)
        self.assertEqual(self.core.resubmit_registration(token, "Corrected#KR2", "MID", "AD", "수정 완료"), registration["member_id"])
        self.core.approve_member(self.admin, registration["member_id"], 170)
        approved = self.core.session(token)
        self.assertEqual((approved["id"], approved["member_id"]), (registration["account_id"], registration["member_id"]))
        self.assertEqual((approved["member_status"], approved["registration_status"]), ("APPROVED", "APPROVED"))
        with self.core.read_snapshot() as db:
            self.core.require_event_manager(db, token)
        self.assertEqual(self.counts(), (2, 1, 1))
        with self.assertRaises(ValueError):
            self.core.resubmit_registration(token, "Another#KR1", "TOP", "JG")

    def test_personal_link_and_role_are_fixed_and_kick_revokes_access(self):
        one = self.signup()
        other = self.signup("other", "Other#KR1")
        token = self.core.login("player", "personal-password-123")
        with self.assertRaises(ValueError):
            self.core.set_account_role(self.admin, one["account_id"], "admin")
        with self.assertRaises(ValueError):
            self.core.link_account_member(self.admin, one["account_id"], other["member_id"], "change identity")
        self.core.set_account_role(self.admin, one["account_id"], "member", active=True)
        self.assertIsNone(self.core.session(token))
        token = self.core.login("player", "personal-password-123")
        self.core.kick_member(self.admin, one["member_id"], "탈퇴")
        self.assertIsNone(self.core.session(token))
        with self.assertRaises(PermissionError):
            self.core.login("player", "personal-password-123")
        with self.assertRaises(PermissionError):
            self.core.resubmit_registration(token, "Other#KR1", "TOP", "JG")

    def test_pending_edit_preserves_identity_and_loses_race_with_approval_safely(self):
        own = self.signup()
        token = self.core.login("player", "personal-password-123")
        self.assertEqual(self.core.resubmit_registration(token, "Edited#KR2", "MID", "AD", "대기 중 수정"), own["member_id"])
        actor = self.core.session(token)
        self.assertEqual((actor["id"], actor["member_id"], actor["username"], actor["registration_status"]),
                         (own["account_id"], own["member_id"], "player", "PENDING"))
        self.assertEqual(self.core.get_member(own["member_id"])["riot_id"], "Edited#KR2")
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='REGISTRATION_UPDATE'").fetchone()[0], 1)
        entered, release = threading.Event(), threading.Event()
        def suspended_role(value):
            entered.set()
            if not release.wait(10):
                raise AssertionError("registration edit was never released")
            return role(value)
        with patch("roly.core.role", side_effect=suspended_role), ThreadPoolExecutor(max_workers=1) as pool:
            editing = pool.submit(self.core.resubmit_registration, token, "TooLate#KR1", "TOP", "JG")
            try:
                self.assertTrue(entered.wait(10))
                self.core.approve_member(self.admin, own["member_id"], 123)
            finally:
                release.set()
            with self.assertRaises(ValueError):
                editing.result(timeout=10)
        member = self.core.get_member(own["member_id"])
        self.assertEqual((member["riot_id"], member["status"], member["base_score"]), ("Edited#KR2", "APPROVED", 123))

    def test_resubmission_duplicate_riot_rolls_back_and_cannot_modify_other_account(self):
        one = self.signup()
        other = self.signup("other", "Other#KR1")
        token = self.core.login("player", "personal-password-123")
        self.core.reject_registration(self.admin, one["member_id"], "확인 필요")
        with self.assertRaises(ValueError):
            self.core.resubmit_registration(token, "Other#KR1", "MID", "AD")
        self.assertEqual(self.core.get_member(one["member_id"])["riot_id"], "Player#KR1")
        self.assertEqual(self.core.session(token)["registration_status"], "REJECTED")
        self.assertEqual(self.core.get_member(other["member_id"])["riot_id"], "Other#KR1")
        with self.assertRaises(PermissionError):
            self.core.reject_registration(token, other["member_id"], "forged admin")

    def test_password_change_verifies_current_and_revokes_all_sessions(self):
        self.signup()
        first = self.core.login("player", "personal-password-123")
        second = self.core.login("player", "personal-password-123")
        with self.assertRaises(PermissionError):
            self.core.change_password(first, "wrong-password", "new-password-123")
        self.assertIsNotNone(self.core.session(second))
        self.core.change_password(first, "personal-password-123", "new-password-123")
        self.assertIsNone(self.core.session(first))
        self.assertIsNone(self.core.session(second))
        with self.assertRaises(PermissionError):
            self.core.login("player", "personal-password-123")
        self.assertIsNotNone(self.core.session(self.core.login("player", "new-password-123")))

    def test_reset_is_admin_only_hashed_single_use_and_reissue_invalidates_old_code(self):
        registration = self.signup()
        token = self.core.login("player", "personal-password-123")
        with self.assertRaises(PermissionError):
            self.core.issue_password_reset(token, registration["account_id"])
        first = self.core.issue_password_reset(self.admin, registration["account_id"])
        self.assertIsNone(self.core.session(token))
        second = self.core.issue_password_reset(self.admin, registration["account_id"])
        with self.assertRaises(PermissionError):
            self.core.reset_password(first["token"], "recovered-password")
        with self.core.read_snapshot() as db:
            stored = str([dict(r) for r in db.execute("SELECT * FROM password_resets")])
            audit = str([dict(r) for r in db.execute("SELECT * FROM audit")])
            self.assertNotIn(second["token"], stored + audit)
            self.assertIn(hashlib.sha256(second["token"].encode()).hexdigest(), stored)
        self.core.reset_password(second["token"], "recovered-password")
        with self.assertRaises(PermissionError):
            self.core.reset_password(second["token"], "another-password")
        self.assertIsNotNone(self.core.session(self.core.login("player", "recovered-password")))

    def test_expired_or_deactivated_reset_and_session_are_rejected(self):
        registration = self.signup()
        reset = self.core.issue_password_reset(self.admin, registration["account_id"])
        token = self.core.login("player", "personal-password-123")
        with self.core.transaction() as db:
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            db.execute("UPDATE password_resets SET expires_at=?", (expired,))
            db.execute("UPDATE sessions SET expires_at=? WHERE account_id=?", (expired, registration["account_id"]))
        self.assertIsNone(self.core.session(token))
        with self.assertRaises(PermissionError):
            self.core.reset_password(reset["token"], "recovered-password")
        reset = self.core.issue_password_reset(self.admin, registration["account_id"])
        self.core.set_account_role(self.admin, registration["account_id"], "member", active=False)
        with self.assertRaises(PermissionError):
            self.core.reset_password(reset["token"], "recovered-password")
        self.core.logout(token)
        self.core.logout(None)

    def test_hashing_does_not_hold_writer_lock_for_login_signup_or_admin_create(self):
        real_digest = auth.password_digest
        for target, operation in (
            ("roly.core.password_digest", lambda: self.core.login("admin", "admin-password-123")),
            ("roly.auth.password_digest", lambda: self.signup()),
            ("roly.auth.password_digest", lambda: self.core.create_account(self.admin, "host", "host-password-123")),
        ):
            entered, release = threading.Event(), threading.Event()
            def held_digest(password, salt):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("writer could not progress while hashing")
                return real_digest(password, salt)
            with self.subTest(target=target), patch(target, side_effect=held_digest), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(operation)
                try:
                    self.assertTrue(entered.wait(5))
                    with closing(self.core.connect()) as db:
                        db.execute("PRAGMA busy_timeout=100")
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("UPDATE policies SET k=k")
                        db.commit()
                finally:
                    release.set()
                future.result(timeout=5)

    def test_login_rechecks_account_and_attempts_after_password_computation(self):
        registration = self.signup()
        digest = auth.password_digest
        def deactivate(password, salt):
            result = digest(password, salt)
            with self.core.transaction() as db:
                db.execute("UPDATE accounts SET active=0 WHERE id=?", (registration["account_id"],))
            return result
        with patch("roly.core.password_digest", side_effect=deactivate), self.assertRaises(PermissionError):
            self.core.login("player", "personal-password-123")
        with self.core.transaction() as db:
            db.execute("UPDATE accounts SET active=1 WHERE id=?", (registration["account_id"],))
            db.execute("DELETE FROM login_failures WHERE username='player'")
        def throttle(password, salt):
            result = digest(password, salt)
            with self.core.transaction() as db:
                for _ in range(5):
                    db.execute("INSERT INTO login_failures VALUES('player',?)", (auth.stamp(),))
            return result
        with patch("roly.core.password_digest", side_effect=throttle), self.assertRaises(PermissionError):
            self.core.login("player", "personal-password-123")
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE account_id=?", (registration["account_id"],)).fetchone()[0], 0)

    def test_password_reset_concurrent_use_has_one_winner(self):
        registration = self.signup()
        code = self.core.issue_password_reset(self.admin, registration["account_id"])["token"]
        def use_code(index):
            try:
                self.core.reset_password(code, f"recovered-password-{index}")
                return index
            except PermissionError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [i for i in pool.map(use_code, range(2)) if i is not None]
        self.assertEqual(len(winners), 1)
        self.assertIsNotNone(self.core.session(self.core.login("player", f"recovered-password-{winners[0]}")))

    def test_event_owner_capability_is_bound_to_fixture_roster_and_policy(self):
        own = self.signup()
        self.core.approve_member(self.admin, own["member_id"], 100)
        token = self.core.login("player", "personal-password-123")
        assignments = [{"member_id": own["member_id"], "role": "TOP"}]
        for i in range(1, 10):
            member = self.core.join_member(f"Roster{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5])
            self.core.approve_member(self.admin, member, 100)
            assignments.append({"member_id": member, "role": ROLES[i % 5]})
        comp = Competition(self.core)
        event_id = comp.create_normal(token, assignments, balanced=False)
        other_event = comp.create_normal(self.admin, assignments, balanced=False)
        with self.core.read_snapshot() as db:
            fixture = dict(db.execute("SELECT * FROM competition_games WHERE event_id=?", (event_id,)).fetchone())
            foreign_fixture = db.execute("SELECT id FROM competition_games WHERE event_id=?", (other_event,)).fetchone()[0]
            policy = json.loads(db.execute("SELECT policy_snapshot FROM competition_events WHERE id=?", (event_id,)).fetchone()[0])["score_policy"]["id"]
            rosters = [[dict(r) for r in db.execute("SELECT member_id,role FROM competition_players WHERE event_id=? AND team_id=?", (event_id, fixture[side]))] for side in ("team_a", "team_b")]
            with self.assertRaises(PermissionError):
                self.core.require_event_manager(db, token, other_event)
        for kwargs, teams in (
            ({"fixture_id": foreign_fixture, "policy_id": policy}, rosters),
            ({"fixture_id": fixture["id"], "policy_id": None}, rosters),
            ({"fixture_id": fixture["id"], "policy_id": policy}, list(reversed(rosters))),
            ({"policy_id": policy}, rosters),
        ):
            with self.core.transaction() as db, self.assertRaises(ValueError):
                self.core.record_game(token, "forged-fixture", *teams, "A", tournament_id=event_id, conn=db, **kwargs)
        for operation in (
            lambda: self.core.record_game(token, "direct", *rosters, "A"),
            lambda: self.core.grant_award(token, [own["member_id"]], 1, "manual", "forged-award"),
            lambda: self.core.adjust_score(token, own["member_id"], 10, "manual"),
        ):
            with self.assertRaises(PermissionError):
                operation()
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM games").fetchone()[0], 0)
        with self.assertRaisesRegex(RuntimeError, "rollback probe"):
            with self.core.transaction() as db:
                recorded = self.core.record_game(token, "reserved-fixture", *rosters, "A", tournament_id=event_id,
                    conn=db, fixture_id=fixture["id"], policy_id=policy)
                self.assertEqual(db.execute("SELECT core_game_id FROM competition_games WHERE id=?", (fixture["id"],)).fetchone()[0], recorded)
                self.assertEqual(self.core.record_game(token, "reserved-fixture", *rosters, "A", tournament_id=event_id,
                    conn=db, fixture_id=fixture["id"], policy_id=policy), recorded)
                with self.assertRaises(ValueError):
                    self.core.record_game(token, "second-request-same-fixture", *rosters, "A", tournament_id=event_id,
                        conn=db, fixture_id=fixture["id"], policy_id=policy)
                self.assertEqual(db.execute("SELECT count(*) FROM games").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM score_ledger").fetchone()[0], 10)
                raise RuntimeError("rollback probe")
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM games").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT core_game_id FROM competition_games WHERE id=?", (fixture["id"],)).fetchone()[0])
        comp.record_result(token, event_id, fixture["id"], fixture["team_a"])
        game = self.core.list_games()[0]
        self.assertEqual(game["actor_id"], own["account_id"])
        with self.assertRaises(PermissionError):
            self.core.correct_game(token, game["id"], "B", "owner is not admin")

    def test_legacy_link_change_is_blocked_during_participation(self):
        assignments = []
        for i in range(10):
            member = self.core.join_member(f"Legacy{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5])
            self.core.approve_member(self.admin, member, 100)
            assignments.append({"member_id": member, "role": ROLES[i % 5]})
        account_id = self.core.create_account(self.admin, "legacy", "legacy-password-123", role="member", member_id=assignments[0]["member_id"])
        comp = Competition(self.core)
        event = comp.create_normal(self.admin, assignments, balanced=False)
        with self.assertRaises(ValueError):
            self.core.link_account_member(self.admin, account_id, assignments[1]["member_id"], "identity correction")
        comp.cancel_event(self.admin, event, "내전 취소")
        self.core.link_account_member(self.admin, account_id, assignments[1]["member_id"], "identity correction")
        self.assertEqual(self.core.session(self.core.login("legacy", "legacy-password-123"))["member_id"], assignments[1]["member_id"])


if __name__ == "__main__":
    unittest.main()
