"""Stored profile validation, concurrent edits, and historical game identity."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.member_profile import CURRENT_TIERS, validate_clan_tier, validate_current_tier


class MemberProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-member-profile-")
        self.path = Path(self.temp.name) / "isolated.sqlite3"
        self.core = Core(self.path)
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.admin = self.core.login("admin", "synthetic-admin-password")
        self.key = str(uuid4())
        receipt = self.register(self.key)
        self.mid = receipt["member_id"]
        self.account_id = receipt["account_id"]
        self.token = self.core.login("member", "synthetic-member-password")

    def tearDown(self):
        self.temp.cleanup()

    def register(self, key, **overrides):
        fields = dict(current_tier="골드 2", current_tier_lp=50)
        fields.update(overrides)
        return self.core.register_member("member", "synthetic-member-password", "Member#QA", "TOP", "JG", "original note", request_key=key, **fields)

    def update(self, *, token=None, **changes):
        before = self.core.get_member(self.mid)
        values = {key: before[key] for key in ("riot_id", "main_role", "sub_role", "base_score")}
        values.update(changes)
        return self.core.update_member(token or self.admin, self.mid, reason="synthetic profile correction", **values)

    def test_tier_validation_has_exact_options_and_rejects_invalid_lp(self):
        self.assertEqual(len(CURRENT_TIERS), 33)
        for tier in CURRENT_TIERS:
            self.assertEqual(validate_current_tier(tier), (tier, None))
        self.assertEqual(validate_current_tier(" 마스터 ", "10000"), ("마스터", 10000))
        self.assertEqual(validate_clan_tier("  클랜 평가 A  "), "클랜 평가 A")
        for tier, lp in (("Gold 2", 5), ("골드", 5), ("마스터", -1), ("마스터", 10001),
                         ("마스터", True), ("마스터", 1.5), ("마스터", "1.5"), ("", 0), ("언랭크", 10)):
            with self.subTest(tier=tier, lp=lp), self.assertRaises(ValueError):
                validate_current_tier(tier, lp)
        with self.assertRaises(ValueError):
            validate_clan_tier("가" * 33)

    def test_signup_persists_rank_without_score_and_binds_it_to_receipt(self):
        member = self.core.get_member(self.mid)
        self.assertEqual((member["clan_tier"], member["current_tier"], member["current_tier_lp"], member["base_score"]),
                         ("", "골드 2", 50, 0))
        self.assertIsNotNone(member["current_tier_updated_at"])
        self.assertEqual(self.register(self.key), {"account_id": self.account_id, "member_id": self.mid})
        for different in ({"current_tier": "골드 1"}, {"current_tier_lp": 51}):
            with self.assertRaises(ValueError):
                self.register(self.key, **different)
        with self.assertRaises(TypeError):
            self.register(str(uuid4()), clan_tier="admin-only")
        restored = Core(self.path)
        self.assertEqual(restored.get_member(self.mid), member)
        self.assertEqual(restored.session(self.token)["id"], self.account_id)

    def test_pending_edit_preserves_omitted_rank_and_checks_stale_admin_changes(self):
        original = self.core.get_member(self.mid)
        self.core.resubmit_registration(self.token, "NewName#QA", "MID", "AD", "own note", expected_updated_at=original["updated_at"])
        own_edit = self.core.get_member(self.mid)
        self.assertEqual((own_edit["current_tier"], own_edit["current_tier_lp"], own_edit["current_tier_updated_at"]),
                         ("골드 2", 50, original["current_tier_updated_at"]))
        self.update(clan_tier="클랜 중급", current_tier="플래티넘 4", current_tier_lp=60, expected_updated_at=own_edit["updated_at"])
        administered = self.core.get_member(self.mid)
        with self.assertRaisesRegex(ValueError, "최신 신청"):
            self.core.resubmit_registration(self.token, "Stale#QA", "TOP", "JG", "old own note", current_tier="실버 1", current_tier_lp=1,
                                            expected_updated_at=own_edit["updated_at"])
        self.assertEqual(self.core.get_member(self.mid), administered)
        self.core.resubmit_registration(self.token, "Newest#QA", "TOP", "JG", "latest own note", current_tier="마스터", current_tier_lp=150,
                                        expected_updated_at=administered["updated_at"])
        latest = self.core.get_member(self.mid)
        self.assertEqual((latest["clan_tier"], latest["current_tier"], latest["current_tier_lp"], latest["base_score"]),
                         ("클랜 중급", "마스터", 150, 0))
        self.assertEqual(self.core.session(self.token)["display_name"], "Newest#QA")

    def test_admin_edit_syncs_existing_session_and_audits_before_after_without_score_change(self):
        self.core.approve_member(self.admin, self.mid, 170)
        before = self.core.get_member(self.mid)
        with patch("roly.core.now", return_value=before["updated_at"]):
            self.update(riot_id="Renamed#QA", clan_tier="클랜 A", current_tier="에메랄드 3", current_tier_lp=77,
                        expected_updated_at=before["updated_at"])
        after = self.core.get_member(self.mid)
        self.assertGreater(after["updated_at"], before["updated_at"])
        self.assertEqual(after["score"], before["score"])
        self.assertEqual(self.core.session(self.token)["display_name"], "Renamed#QA")
        self.assertEqual(self.core.session(self.token)["username"], "member")
        with self.core.read_snapshot() as db:
            details = json.loads(db.execute("SELECT details FROM audit WHERE action='MEMBER_UPDATE' ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(details["before"]["current_tier"], "골드 2")
            self.assertEqual(details["after"]["current_tier"], "에메랄드 3")
            self.assertEqual(details["reason"], "synthetic profile correction")
            self.assertEqual(db.execute("SELECT count(*) FROM base_history WHERE member_id=?", (self.mid,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM score_ledger WHERE member_id=?", (self.mid,)).fetchone()[0], 0)
        self.update(riot_id="SecondName#QA")
        second = self.core.get_member(self.mid)
        self.assertEqual((second["clan_tier"], second["current_tier"], second["current_tier_lp"], second["current_tier_updated_at"]),
                         ("클랜 A", "에메랄드 3", 77, after["current_tier_updated_at"]))

    def test_invalid_unauthorized_duplicate_and_failed_audit_edits_roll_back(self):
        before = self.core.get_member(self.mid)
        for fields in ({"clan_tier": "X" * 33}, {"current_tier": "invalid"}, {"current_tier_lp": -1},
                       {"current_tier": "언랭크"}, {"current_tier_lp": True}):
            with self.assertRaises(ValueError):
                self.update(riot_id="MustNotCommit#QA", **fields)
            self.assertEqual(self.core.get_member(self.mid), before)
        with self.assertRaises(PermissionError):
            self.update(token=self.token, clan_tier="self-granted")
        self.core.join_member("Duplicate#QA", "TOP", "JG")
        with self.assertRaises(ValueError):
            self.update(riot_id="Duplicate#QA", clan_tier="duplicate attempt")
        with patch.object(self.core, "_audit", side_effect=RuntimeError("synthetic failure")), self.assertRaises(RuntimeError):
            self.update(riot_id="FailedAudit#QA", clan_tier="must roll back")
        self.assertEqual(self.core.get_member(self.mid), before)
        self.assertEqual(self.core.session(self.token)["display_name"], before["riot_id"])
        self.update(current_tier="언랭크", current_tier_lp=None)
        self.assertEqual((self.core.get_member(self.mid)["current_tier"], self.core.get_member(self.mid)["current_tier_lp"]), ("언랭크", None))

    def test_two_editors_with_same_version_have_one_commit(self):
        before = self.core.get_member(self.mid)
        barrier = Barrier(2)
        def edit(index):
            barrier.wait(timeout=10)
            try:
                self.update(clan_tier=f"review {index}", current_tier=("골드 1", "실버 1")[index], current_tier_lp=index,
                            expected_updated_at=before["updated_at"])
                return index
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [result for result in pool.map(edit, (0, 1)) if result is not None]
        self.assertEqual(len(winners), 1)
        member = self.core.get_member(self.mid)
        self.assertEqual((member["clan_tier"], member["current_tier_lp"]), (f"review {winners[0]}", winners[0]))

    def test_game_history_keeps_name_and_tiers_through_rename_correct_and_void(self):
        self.core.approve_member(self.admin, self.mid, 100)
        self.update(clan_tier="클랜 B")
        ids = [self.mid]
        for index in range(1, 10):
            mid = self.core.join_member(f"Player{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, mid, 100)
            ids.append(mid)
        game_id = self.core.record_game(self.admin, "profile-history", ids[:5], ids[5:], "A")
        original = next(p for p in self.core.get_game(game_id)["players"] if p["member_id"] == self.mid)
        self.update(riot_id="AfterGame#QA", clan_tier="클랜 A", current_tier="다이아몬드 1", current_tier_lp=90)
        for action in (lambda: None, lambda: self.core.correct_game(self.admin, game_id, "B", "correct winner"),
                       lambda: self.core.void_game(self.admin, game_id, "void duplicate event")):
            action()
            player = next(p for p in self.core.get_game(game_id)["players"] if p["member_id"] == self.mid)
            for field in ("riot_id_snapshot", "clan_tier_snapshot", "current_tier_snapshot", "current_tier_lp_snapshot"):
                self.assertEqual(player[field], original[field])
            self.assertEqual((player["riot_id"], player["current_riot_id"], player["clan_tier"], player["current_tier"], player["current_tier_lp"]),
                             ("Member#QA", "AfterGame#QA", "클랜 B", "골드 2", 50))
        self.assertEqual(self.core.get_member(self.mid)["score"], 100)

    def test_legacy_sqlite_additive_columns_preserve_identity_and_session(self):
        # Simulate a v2 database shape using only this isolated fixture.
        with self.core.transaction() as db:
            db.execute("UPDATE members SET notes='original note' WHERE id=?", (self.mid,))
            db.execute("ALTER TABLE members DROP COLUMN application_notes")
            for column in ("current_tier_updated_at", "current_tier_lp", "current_tier", "clan_tier"):
                db.execute(f"ALTER TABLE members DROP COLUMN {column}")
            for column in ("current_tier_lp_snapshot", "current_tier_snapshot", "clan_tier_snapshot", "riot_id_snapshot"):
                db.execute(f"ALTER TABLE game_players DROP COLUMN {column}")
        migrated = Core(self.path)
        member = migrated.get_member(self.mid)
        self.assertEqual((member["riot_id"], member["notes"], member["clan_tier"], member["current_tier"], member["current_tier_lp"]),
                         ("Member#QA", "original note", "", "", None))
        self.assertEqual(member["application_notes"], "")
        self.assertNotIn("notes", migrated.get_own_member(self.token))
        self.assertEqual(migrated.session(self.token)["id"], self.account_id)
        self.assertEqual(migrated.session(self.token)["member_id"], self.mid)
        self.assertEqual(Core(self.path).get_member(self.mid), member)

    def test_new_result_for_legacy_event_keeps_unknown_tier_instead_of_current_profile(self):
        self.core.approve_member(self.admin, self.mid, 100)
        ids = [self.mid]
        for index in range(1, 10):
            mid = self.core.join_member(f"LegacyFixture{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, mid, 100)
            ids.append(mid)
        comp = Competition(self.core)
        event_id = comp.create_normal(self.admin, [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(ids)], balanced=False)
        with self.core.transaction() as db:
            db.execute("UPDATE competition_players SET clan_tier_snapshot=NULL,current_tier_snapshot=NULL,current_tier_lp_snapshot=NULL WHERE event_id=? AND member_id=?", (event_id, self.mid))
        self.update(riot_id="CurrentIdentity#QA", clan_tier="current clan label", current_tier="다이아몬드 1", current_tier_lp=99)
        fixture = comp.get_event(event_id)["games"][0]
        game_id = comp.record_result(self.admin, event_id, fixture["id"], fixture["team_a"])
        player = next(p for p in self.core.get_game(game_id)["players"] if p["member_id"] == self.mid)
        self.assertEqual((player["riot_id"], player["current_riot_id"]), ("Member#QA", "CurrentIdentity#QA"))
        for field in ("clan_tier_snapshot", "current_tier_snapshot", "current_tier_lp_snapshot", "clan_tier", "current_tier", "current_tier_lp"):
            self.assertIsNone(player[field])

    def test_concurrent_manual_adjustment_uuid_has_one_ledger_receipt_and_audit(self):
        request_key = str(uuid4())
        barrier = Barrier(4)
        def adjust(_):
            barrier.wait(timeout=10)
            return self.core.adjust_score(self.admin, self.mid, 7, "verified correction", request_key=request_key)
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(adjust, range(4)))
        self.assertEqual(len(set(receipts)), 1)
        self.assertEqual(self.core.get_member(self.mid)["score"], 7)
        self.assertEqual(self.core.adjust_score(self.admin, self.mid, 7, "verified correction", request_key=request_key), receipts[0])
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM score_ledger WHERE source='MANUAL'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM score_adjustment_requests").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='SCORE_ADJUST'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT ledger_id FROM score_adjustment_requests WHERE request_key=?", (request_key,)).fetchone()[0], receipts[0])

    def test_manual_receipt_is_bound_to_actor_member_amount_reason_and_current_permission(self):
        request_key = str(uuid4())
        original = self.core.adjust_score(self.admin, self.mid, 7, "reviewed", request_key=request_key)
        other_mid = self.core.join_member("OtherScore#QA", "TOP", "JG")
        self.core.create_account(self.admin, "secondadmin", "synthetic-second-password", role="admin")
        second = self.core.login("secondadmin", "synthetic-second-password")
        for token, mid, amount, reason in (
            (self.admin, self.mid, 8, "reviewed"), (self.admin, self.mid, 7, "different reason"),
            (self.admin, other_mid, 7, "reviewed"), (second, self.mid, 7, "reviewed"),
        ):
            with self.assertRaises(ValueError):
                self.core.adjust_score(token, mid, amount, reason, request_key=request_key)
        with self.assertRaises(PermissionError):
            self.core.adjust_score(self.token, self.mid, 7, "reviewed", request_key=request_key)
        self.core.logout(self.admin)
        with self.assertRaises(PermissionError):
            self.core.adjust_score(self.admin, self.mid, 7, "reviewed", request_key=request_key)
        renewed = self.core.login("admin", "synthetic-admin-password")
        self.assertEqual(self.core.adjust_score(renewed, self.mid, 7, "reviewed", request_key=request_key), original)
        self.assertEqual(self.core.get_member(self.mid)["score"], 7)
        for invalid in ("", "not-a-uuid", "00000000-0000-0000-0000-000000000000", True):
            with self.assertRaises(ValueError):
                self.core.adjust_score(renewed, self.mid, 7, "invalid request", request_key=invalid)

    def test_manual_audit_failure_rolls_back_receipt_and_can_retry_the_same_uuid(self):
        request_key = str(uuid4())
        with patch.object(self.core, "_audit", side_effect=RuntimeError("synthetic audit failure")), self.assertRaises(RuntimeError):
            self.core.adjust_score(self.admin, self.mid, 7, "reviewed", request_key=request_key)
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM score_ledger").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM score_adjustment_requests").fetchone()[0], 0)
        result = self.core.adjust_score(self.admin, self.mid, 7, "reviewed", request_key=request_key)
        self.assertEqual(self.core.adjust_score(self.admin, self.mid, 7, "reviewed", request_key=request_key), result)
        self.assertEqual(self.core.get_member(self.mid)["score"], 7)
        # Calls predating the optional receipt API remain separate operations.
        self.assertNotEqual(self.core.adjust_score(self.admin, self.mid, 1, "legacy"), self.core.adjust_score(self.admin, self.mid, 1, "legacy"))
        self.assertEqual(self.core.get_member(self.mid)["score"], 9)

    def test_frozen_clock_kick_and_restore_cannot_reuse_an_old_profile_version(self):
        original = self.core.get_member(self.mid)
        with patch("roly.core.now", return_value=original["updated_at"]):
            self.core.approve_member(self.admin, self.mid, 170, expected_updated_at=original["updated_at"])
            approved = self.core.get_member(self.mid)
            self.core.kick_member(self.admin, self.mid, "synthetic membership pause")
            kicked = self.core.get_member(self.mid)
            self.core.restore_member(self.admin, self.mid, "membership restored", expected_updated_at=kicked["updated_at"])
            restored = self.core.get_member(self.mid)
        self.assertLess(original["updated_at"], approved["updated_at"])
        self.assertLess(approved["updated_at"], kicked["updated_at"])
        self.assertLess(kicked["updated_at"], restored["updated_at"])
        with self.assertRaises(ValueError):
            self.update(riot_id="StaleAfterRestore#QA", expected_updated_at=approved["updated_at"])
        self.assertEqual(self.core.get_member(self.mid), restored)
        self.assertEqual((restored["current_tier"], restored["current_tier_lp"]), ("골드 2", 50))
        self.assertIsNone(self.core.session(self.token))
        self.assertEqual(self.core.session(self.core.login("member", "synthetic-member-password"))["member_id"], self.mid)
        with self.core.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT updated_at FROM registration_requests WHERE member_id=?", (self.mid,)).fetchone()[0], restored["updated_at"])


if __name__ == "__main__":
    unittest.main()
