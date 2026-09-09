"""Independent domain regression tests; all databases are temporary."""
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from roly.core import Core, ROLES
from roly.competition import Competition


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-core-test-")
        self.core = Core(Path(self.temp.name) / "test.sqlite3")
        self.core.setup_admin("admin", "test-password-1234")
        self.token = self.core.login("admin", "test-password-1234")

    def tearDown(self):
        self.temp.cleanup()

    def members(self):
        ids = []
        for i in range(10):
            mid = self.core.join_member(f"Player{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5])
            self.core.approve_member(self.token, mid, 100)
            ids.append(mid)
        return ids

    def test_setup_authentication_and_session_revocation(self):
        self.assertTrue(self.core.has_admin())
        with self.assertRaises(PermissionError):
            self.core.setup_admin("other", "other-password")
        with self.assertRaises(PermissionError):
            self.core.login("admin", "wrong-password")
        self.assertIsNone(self.core.session("invented-token"))
        self.assertEqual(self.core.session(self.token)["role"], "admin")
        with self.core.transaction() as db:
            account = dict(db.execute("SELECT * FROM accounts").fetchone())
            self.assertNotEqual(account["password_hash"], "test-password-1234")
            stored_token = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
            self.assertNotEqual(stored_token, self.token)
        self.core.logout(self.token)
        with self.assertRaises(PermissionError):
            self.core.set_policy(self.token, k=15)

    def test_roles_and_last_admin_protection(self):
        staff_id = self.core.create_account(self.token, "host", "host-password-1234", role="organizer")
        staff = self.core.login("host", "host-password-1234")
        ids = self.members()
        game = self.core.record_game(staff, "staff-game", ids[:5], ids[5:], "A")
        for operation in (
            lambda: self.core.correct_game(staff, game, "B", "not authorized"),
            lambda: self.core.void_game(staff, game, "not authorized"),
            lambda: self.core.adjust_score(staff, ids[0], 10, "not authorized"),
            lambda: self.core.set_policy(staff, k=15),
            lambda: self.core.grant_award(staff, ids[:5], 25, "invented event", "invented", event_id="invented"),
        ):
            with self.assertRaises(PermissionError):
                operation()
        with self.assertRaises(ValueError):
            self.core.set_account_role(self.token, 1, "organizer")
        self.core.set_account_role(self.token, staff_id, "organizer", active=False)
        self.assertIsNone(self.core.session(staff))

    def test_member_identity_and_pending_cannot_self_approve(self):
        mid = self.core.join_member(" Player#KR1 ", "탑", "정글", base_score=9999)
        member = self.core.get_member(mid)
        self.assertEqual((member["status"], member["base_score"]), ("PENDING", 0))
        self.assertEqual(member["main_role"], "TOP")
        self.assertEqual(self.core.list_members(), [])
        with self.assertRaises(ValueError):
            self.core.join_member("player#kr1", "TOP", "JG")
        with self.assertRaises(PermissionError):
            self.core.approve_member("fake-token", mid, 100)
        with self.assertRaises(ValueError):
            self.core.join_member("Other#KR1", "TOP", "TOP")
        self.core.approve_member(self.token, mid, 100)
        self.core.kick_member(self.token, mid, "회원 탈퇴 요청")
        self.assertEqual(self.core.list_members(), [])
        with self.assertRaises(ValueError):
            self.core.join_member("PLAYER#KR1", "TOP", "JG")

    def test_member_projection_is_one_query_and_preserves_score_results_and_awards(self):
        ids = self.members()
        pending = self.core.join_member("Pending#KR1", "TOP", "JG")
        game = self.core.record_game(self.token, "member-projection-normal", ids[:5], ids[5:], "A")
        self.core.record_game(self.token, "member-projection-auction", ids[:5], ids[5:], "A", kind="AUCTION")
        self.core.adjust_score(self.token, ids[0], 7, "독립 수동 점수")
        self.core.grant_award(self.token, [ids[0]], 156, "모든 기호 단위 확인", "projection-awards")
        statements = []
        connect = self.core.connect

        def traced_connect():
            database = connect()
            database.set_trace_callback(statements.append)
            return database

        def read_expected(score, wins, losses, units, *, approved_count=10):
            with patch.object(self.core, "connect", side_effect=traced_connect):
                statements.clear()
                member = self.core.get_member(ids[0])
                self.assertEqual(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements), 1)
                statements.clear()
                members = self.core.list_members()
                self.assertEqual(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements), 1)
            self.assertEqual(len(members), approved_count)
            self.assertNotIn(pending, [row["id"] for row in members])
            self.assertEqual(members, sorted(members, key=lambda row: row["riot_id"]))
            self.assertEqual(next(row for row in members if row["id"] == ids[0]), member)
            self.assertEqual((member["score"], member["wins"], member["losses"], member["award_units"]), (score, wins, losses, units))
            self.assertEqual((member["primary_role"], member["secondary_role"], member["nickname"], member["riot_tag"]), ("TOP", "JG", "Player0", "KR1"))
            return member

        member = read_expected(117, 1, 0, 156)
        self.assertEqual((member["cats"], member["stars"], member["medals"], member["trophies"]), (1, 1, 1, 1))
        self.core.correct_game(self.token, game, "B", "결과 정정")
        read_expected(97, 0, 1, 156)
        self.core.void_game(self.token, game, "경기 무효")
        read_expected(107, 0, 0, 156)
        self.core.grant_award(self.token, [ids[0]], -1, "기호 1단위 회수", "projection-awards-reverse")
        self.core.kick_member(self.token, ids[-1], "목록 승인 필터 확인")
        member = read_expected(107, 0, 0, 155, approved_count=9)
        self.assertEqual((member["cats"], member["stars"], member["medals"], member["trophies"]), (0, 1, 1, 1))
        with patch.object(self.core, "connect", side_effect=traced_connect):
            statements.clear()
            everyone = self.core.list_members(include_pending=True)
            self.assertEqual(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements), 1)
        self.assertEqual(len(everyone), 11)
        self.assertEqual(next(row for row in everyone if row["id"] == pending)["score"], 0)
        self.assertEqual(next(row for row in everyone if row["id"] == ids[-1])["status"], "KICKED")

    def test_game_retry_and_changed_payload(self):
        ids = self.members()
        game = self.core.record_game(self.token, "once", ids[:5], ids[5:], "A")
        self.assertEqual(self.core.record_game(self.token, "once", ids[:5], ids[5:], "A"), game)
        self.assertEqual(self.core.get_member(ids[0])["score"], 110)
        with self.assertRaises(ValueError):
            self.core.record_game(self.token, "once", ids[:5], ids[5:], "B")
        self.assertEqual(len(self.core.list_games()), 1)
        self.assertEqual(len(self.core.get_game(game)["ledger"]), 10)

    def test_record_failure_rolls_back_all_rows(self):
        ids = self.members()
        with self.core.transaction() as db:
            db.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON score_ledger WHEN NEW.member_id=7 BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.core.record_game(self.token, "fails", ids[:5], ids[5:], "A")
        self.assertEqual(self.core.list_games(), [])
        self.assertEqual([m["score"] for m in self.core.list_members()], [100] * 10)
        with self.core.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM game_players").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM score_ledger").fetchone()[0], 0)

    def test_outer_transaction_can_rollback_competition_and_game(self):
        ids = self.members()
        with self.assertRaisesRegex(RuntimeError, "later failure"):
            with self.core.transaction() as db:
                self.core.record_game(self.token, "nested", ids[:5], ids[5:], "A", conn=db)
                raise RuntimeError("later failure")
        self.assertEqual(self.core.list_games(), [])

    def test_caught_nested_failure_leaves_no_partial_game_or_correction(self):
        ids = self.members()
        with self.core.transaction() as db:
            db.execute("CREATE TRIGGER fail_nested_ledger BEFORE INSERT ON score_ledger WHEN NEW.member_id=7 BEGIN SELECT RAISE(ABORT,'nested failure'); END")
            db.execute("UPDATE members SET notes='independent outer change' WHERE id=?", (ids[0],))
            with self.assertRaises(sqlite3.IntegrityError):
                self.core.record_game(self.token, 'nested-failed-game', ids[:5], ids[5:], 'A', conn=db)
        self.assertEqual(self.core.list_games(), [])
        self.assertEqual(self.core.get_member(ids[0])['notes'], 'independent outer change')
        with self.core.transaction() as db:
            db.execute('DROP TRIGGER fail_nested_ledger')
        game = self.core.record_game(self.token, 'nested-failed-game', ids[:5], ids[5:], 'A')
        before = self.core.get_game(game)
        with self.core.transaction() as db:
            db.execute("CREATE TRIGGER fail_nested_ledger BEFORE INSERT ON score_ledger WHEN NEW.member_id=7 BEGIN SELECT RAISE(ABORT,'nested failure'); END")
            for action in (lambda: self.core.correct_game(self.token, game, 'B', 'failed correction', conn=db),
                           lambda: self.core.void_game(self.token, game, 'failed void', conn=db)):
                with self.assertRaises(sqlite3.IntegrityError):
                    action()
        self.assertEqual(self.core.get_game(game), before)
        self.assertEqual(self.core.get_member(ids[0])['score'], 110)

    def test_caught_nested_award_failure_can_retry_without_partial_payout(self):
        ids = self.members()
        with self.core.transaction() as db:
            db.execute("CREATE TRIGGER fail_nested_award BEFORE INSERT ON award_ledger WHEN NEW.member_id=3 BEGIN SELECT RAISE(ABORT,'award failure'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                self.core.grant_award(self.token, ids[:5], 1, 'review award', 'nested-award', conn=db)
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM award_batches').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM award_ledger').fetchone()[0], 0)
        with self.core.transaction() as db:
            db.execute('DROP TRIGGER fail_nested_award')
        self.core.grant_award(self.token, ids[:5], 1, 'review award', 'nested-award')
        self.assertEqual([self.core.get_member(member)['award_units'] for member in ids[:5]], [1] * 5)

    def test_supplied_connection_requires_an_active_transaction(self):
        ids = self.members()
        db = self.core.connect()
        try:
            with self.assertRaises(ValueError):
                self.core.record_game(self.token, 'autocommit', ids[:5], ids[5:], 'A', conn=db)
        finally:
            db.close()
        self.assertEqual(self.core.list_games(), [])

    def test_policy_snapshot_and_independent_manual_adjustment(self):
        ids = self.members()
        game = self.core.record_game(self.token, "first", ids[:5], ids[5:], "A")
        old_policy = self.core.get_game(game)["policy_id"]
        self.core.adjust_score(self.token, ids[0], 7, "실력 보정 근거")
        self.core.set_policy(self.token, k=20)
        self.core.update_member(self.token, ids[0], "Changed#KR1", "TOP", "JG", 200, "평가 갱신")
        self.core.correct_game(self.token, game, "B", "승리팀 오입력")
        self.assertEqual(self.core.get_member(ids[0])["score"], 197)
        result = self.core.get_game(game)
        self.assertEqual(result["policy_id"], old_policy)
        first = next(s for s in result["settlements"] if s["member_id"] == ids[0] and s["revision"] == 1)
        second = next(s for s in result["settlements"] if s["member_id"] == ids[0] and s["revision"] == 2)
        self.assertEqual((first["score_before"], first["delta"]), (100, 10))
        self.assertEqual((second["score_before"], second["delta"]), (100, -10))
        next_game = self.core.record_game(self.token, "next", ids[:5], ids[5:], "A")
        self.assertEqual(self.core.get_member(ids[0])["score"], 217)
        self.assertNotEqual(self.core.get_game(next_game)["policy_id"], old_policy)

    def test_correction_void_and_retries_preserve_history(self):
        ids = self.members()
        game = self.core.record_game(self.token, "revise", ids[:5], ids[5:], "A")
        self.core.correct_game(self.token, game, "B", "결과 정정")
        self.core.correct_game(self.token, game, "B", "같은 정정 재시도")
        self.assertEqual((self.core.get_member(ids[0])["wins"], self.core.get_member(ids[0])["losses"]), (0, 1))
        self.core.void_game(self.token, game, "무효 경기")
        self.core.void_game(self.token, game, "무효 재시도")
        self.assertEqual(self.core.get_member(ids[0])["score"], 100)
        self.assertEqual(self.core.get_member(ids[0])["losses"], 0)
        self.assertEqual(len(self.core.get_game(game)["revisions"]), 3)

    def test_tournament_pins_policy_and_requires_coordinated_changes(self):
        ids = self.members()
        initial_policy = self.core.policy()["id"]
        competition = Competition(self.core)
        event_id = competition.create_normal(self.token, [
            {"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(ids)])
        event = competition.get_event(event_id)
        fixture = event["games"][0]
        winner = next(team["id"] for team in event["teams"] if any(p["member_id"] == ids[0] for p in team["players"]))
        other = fixture["team_b"] if winner == fixture["team_a"] else fixture["team_a"]
        self.core.set_policy(self.token, k=30)
        game = competition.record_result(self.token, event_id, fixture["id"], winner)
        self.assertEqual(self.core.get_member(ids[0])["score"], 110)
        self.assertEqual(self.core.get_game(game)["policy_id"], initial_policy)
        with self.assertRaises(ValueError):
            self.core.correct_game(self.token, game, "B", "must coordinate competition")
        with self.assertRaises(ValueError):
            self.core.record_game(self.token, "direct-event", ids[:5], ids[5:], "A", tournament_id=event_id)
        competition.record_result(self.token, event_id, fixture["id"], other, reason="competition correction")
        self.assertEqual(self.core.get_member(ids[0])["score"], 90)

    def test_reject_duplicate_role_and_inactive_member(self):
        ids = self.members()
        with self.assertRaises(ValueError):
            self.core.record_game(self.token, "duplicate", ids[:5], ids[:5], "A")
        assignments = [{"member_id": member, "role": "TOP"} for member in ids[:5]]
        with self.assertRaises(ValueError):
            self.core.record_game(self.token, "roles", assignments, ids[5:], "A")
        self.core.kick_member(self.token, ids[0], "참가 자격 종료")
        with self.assertRaises(ValueError):
            self.core.record_game(self.token, "inactive", ids[:5], ids[5:], "A")
        self.assertEqual(self.core.list_games(), [])

    def test_auction_points_and_awards_use_separate_ledgers(self):
        ids = self.members()
        self.core.record_game(self.token, "auction", ids[:5], ids[5:], "A", kind="AUCTION")
        self.core.grant_award(self.token, ids[:5], 1, "독립 원장 검증", "first-key")
        self.core.grant_award(self.token, ids[:5], 1, "독립 원장 검증", "first-key")
        member = self.core.get_member(ids[0])
        self.assertEqual((member["score"], member["wins"], member["cats"]), (100, 0, 1))
        self.core.grant_award(self.token, [ids[0]], 4, "특별 포상", "manual-1")
        self.assertEqual((self.core.get_member(ids[0])["cats"], self.core.get_member(ids[0])["stars"]), (0, 1))
        with self.assertRaises(ValueError):
            self.core.grant_award(self.token, [ids[0]], -6, "초과 회수", "manual-2")

    def test_simultaneous_retry_has_one_settlement(self):
        ids = self.members()
        def submit(_):
            return self.core.record_game(self.token, "concurrent", ids[:5], ids[5:], "A")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.core.get_member(ids[0])["score"], 110)

    def test_csv_formula_prefix_is_neutralized(self):
        exported = self.core.csv_bytes([{"nickname": "=SUM(A1:A2)", "score": -10}]).decode("utf-8-sig")
        self.assertIn("'=SUM(A1:A2)", exported)
        self.assertIn("-10", exported)

    def test_private_adjustments_and_audit_always_require_admin(self):
        ids = self.members()
        self.core.adjust_score(self.token, ids[0], 7, "PRIVATE adjustment reason")
        self.core.create_account(self.token, "reviewhost", "review-password-123", role="organizer")
        self.core.create_account(self.token, "reviewmember", "review-password-123", role="member", member_id=ids[0])
        denied_tokens = (None, "invalid-token", self.core.login("reviewhost", "review-password-123"),
                         self.core.login("reviewmember", "review-password-123"))
        for token in denied_tokens:
            with self.subTest(token_kind="missing" if token is None else "non-admin"):
                with self.assertRaises(PermissionError):
                    self.core.list_adjustments(token=token)
                with self.assertRaises(PermissionError):
                    self.core.audit_log(token=token)
        self.assertEqual(self.core.list_adjustments(token=self.token)[0]["reason"], "PRIVATE adjustment reason")
        self.assertTrue(self.core.audit_log(token=self.token))


if __name__ == "__main__":
    unittest.main()
