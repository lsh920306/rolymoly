"""Preparation transitions, live hand-off, ranking and original ledger rules."""
from collections import Counter
from contextlib import closing
import json
import sqlite3
import unittest
import uuid

from roly.core import Core
from roly.competition import Competition, ROLES, auction_budget
from roly.tournament import TournamentService, allowed_formats, allowed_team_counts
from roly.live_auction import LiveAuction


class TournamentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "testing-password-only")
        cls.token = cls.base.login("admin", "testing-password-only")
        Competition(cls.base)
        cls.ids = []
        for i in range(40):
            mid = cls.base.join_member(f"Tournament{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5])
            cls.base.approve_member(cls.token, mid, 100 + i)
            cls.ids.append(mid)
        cls.captain_tokens = {}
        for i, mid in enumerate(cls.ids[::5]):
            cls.base.create_account(cls.token, f"captain{i}", "captain-password-only", role="member", member_id=mid)
            cls.captain_tokens[mid] = cls.base.login(f"captain{i}", "captain-password-only")
        cls.base.create_account(cls.token, "host", "host-password-only")
        cls.host = cls.base.login("host", "host-password-only")
        cls.base.create_account(cls.token, "other", "other-password-only")
        cls.other = cls.base.login("other", "other-password-only")

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        self.core = Core(":memory:")
        self.base._keeper.backup(self.core._keeper)
        self.comp = Competition(self.core)
        self.service = TournamentService(self.core, self.comp)

    def tearDown(self):
        self.core._keeper.close()

    def draft(self, teams=4, mode="AUCTION", format_name="TOURNAMENT", token=None):
        return self.service.create(token or self.token, "테스트 대회", "2026-10-05T20:30:00+09:00", "실제 대회 설명", mode, teams, format_name)

    def assignments(self, count=20):
        return [{"member_id": m, "role": ROLES[i % 5]} for i, m in enumerate(self.ids[:count])]

    def team_building(self, teams=4, mode="AUCTION", format_name="TOURNAMENT"):
        event_id = self.draft(teams, mode, format_name)
        self.service.open_recruitment(self.token, event_id)
        self.service.set_participants(self.token, event_id, self.assignments(teams * 5))
        self.service.confirm_participants(self.token, event_id)
        self.service.set_captains(self.token, event_id, self.ids[:teams * 5:5])
        return event_id

    def live_complete(self, event_id):
        self.service.prepare_auction(self.token, event_id)
        clock = [2_000_000_000.0]
        live = LiveAuction(self.core, self.comp, clock=lambda: clock[0])
        event = self.comp.get_event(event_id)
        captain_by_member = {p["member_id"]: self.ids[(self.ids.index(p["member_id"]) // 5) * 5] for p in event["pool"]}
        live.configure(self.token, event_id, bid_seconds=5, order=[p["member_id"] for p in event["pool"]])
        live.start(self.token, event_id)
        while live.get_state(event_id)["status"] != "COMPLETED":
            state = live.get_state(event_id)
            lot = state["current_lot"]
            if state["status"] == "RUNNING":
                captain = captain_by_member[lot["member_id"]]
                live.place_bid(self.captain_tokens[captain], event_id, lot["id"], 10, str(uuid.uuid4()))
                clock[0] = live.get_state(event_id)["current_lot"]["closes_at"]
            else:
                clock[0] += 3
            live.settle_due()
        return live

    def finish(self, event_id):
        while True:
            ready = [g for g in self.comp.get_event(event_id)["games"] if g["status"] == "PENDING" and g["team_a"] and g["team_b"]]
            if not ready:
                break
            for game in ready:
                self.comp.record_result(self.token, event_id, game["id"], game["team_a"])

    def test_metadata_capacity_permissions_and_additive_reopen(self):
        event_id = self.draft(token=self.host)
        event = self.service.get_event(event_id)
        self.assertEqual(event["status"], "DRAFT")
        self.assertEqual(event["starts_at"], "2026-10-05T11:30:00+00:00")
        self.assertEqual(event["created_by_name"], self.core.session(self.host)["display_name"])
        self.assertEqual(event["team_count"], 4)
        with self.assertRaises(PermissionError):
            self.service.open_recruitment(self.other, event_id)
        with self.assertRaises(PermissionError):
            self.service.open_recruitment(self.captain_tokens[self.ids[0]], event_id)
        self.service.open_recruitment(self.host, event_id)
        self.service.set_participants(self.host, event_id, self.assignments(19))
        with self.assertRaises(ValueError):
            self.service.confirm_participants(self.host, event_id)
        with self.assertRaises(ValueError):
            self.service.set_participants(self.host, event_id, self.assignments(21))
        with self.assertRaises(ValueError):
            self.service.set_participants(self.host, event_id, [self.assignments()[0]] * 2)
        self.assertEqual(self.service.get_event(event_id)["participant_count"], 19)
        self.assertEqual(TournamentService(self.core).get_event(event_id)["status"], "RECRUITING")
        self.assertEqual(allowed_team_counts("AUCTION"), (4, 6, 8))
        self.assertIn("RANKING", allowed_formats(4, "AUCTION"))
        with self.assertRaises(ValueError):
            self.draft(2)

    def test_exclusion_replacement_preserves_member_and_snapshot(self):
        event_id = self.draft()
        self.service.open_recruitment(self.token, event_id)
        self.service.set_participants(self.token, event_id, self.assignments())
        first = self.ids[0]
        score = self.core.get_member(first)["score"]
        with self.assertRaises(ValueError):
            self.service.warn_participant(self.token, event_id, first, "")
        with self.assertRaises(ValueError):
            self.service.exclude_participant(self.token, event_id, first, "")
        with self.assertRaisesRegex(PermissionError, "본인이 만든"):
            self.service.warn_participant(self.other, event_id, first, "다른 진행자 조치")
        with self.assertRaisesRegex(PermissionError, "본인이 만든"):
            self.service.exclude_participant(self.other, event_id, first, "다른 진행자 조치")
        self.service.warn_participant(self.token, event_id, first, "참가자 안내 위반 확인")
        self.service.exclude_participant(self.token, event_id, first, "본인 참가 취소 요청")
        event = self.service.get_event(event_id)
        self.assertEqual(event["participant_count"], 19)
        excluded = event["excluded_players"][0]
        self.assertEqual(excluded["warning_count"], 1)
        self.assertEqual(self.core.get_member(first)["status"], "APPROVED")
        self.assertEqual(self.core.get_member(first)["score"], score)
        selection = self.assignments()[1:] + [{"member_id": self.ids[20], "role": "TOP"}]
        self.service.set_participants(self.token, event_id, selection)
        self.service.confirm_participants(self.token, event_id)
        player = next(p for p in self.service.get_event(event_id)["players"] if p["member_id"] == self.ids[1])
        self.assertEqual(player["main_role_snapshot"], "JG")
        self.assertIsNone(player["tier_snapshot"])
        self.service.set_captains(self.token, event_id, [self.ids[20], *self.ids[5:20:5]])
        self.service.prepare_auction(self.token, event_id)
        live = LiveAuction(self.core, self.comp)
        live.configure(self.token, event_id)
        live.start(self.token, event_id)
        self.assertEqual(self.comp.get_event(event_id)["status"], "AUCTION")

    def test_legacy_attendance_is_preserved_and_does_not_block_preparation(self):
        event_id = self.draft(2, "BALANCE", "SINGLE")
        self.service.open_recruitment(self.token, event_id)
        self.service.set_participants(self.token, event_id, self.assignments(10))
        first = self.ids[0]
        # Seed old data directly: attendance is no longer a service operation.
        requested_at = "2026-09-01T10:00:00+00:00"
        confirmed_at = "2026-09-01T10:01:00+00:00"
        actor = self.core.session(self.token)
        with self.core.transaction() as conn:
            conn.execute("UPDATE competition_players SET attendance_status='ABSENT',attendance_requested_at=?,attendance_confirmed_at=? WHERE event_id=? AND member_id=?", (requested_at, confirmed_at, event_id, first))
            self.comp._audit(conn, event_id, actor, "ATTENDANCE_CONFIRM", "기존 출석 확인 기록")
        legacy_audit = next(row for row in self.service.get_event(event_id)["audit"] if row["action"] == "ATTENDANCE_CONFIRM")
        self.service.exclude_participant(self.token, event_id, first, "명단 조정")
        self.service.set_participants(self.token, event_id, self.assignments(10))
        self.service.confirm_participants(self.token, event_id)
        self.service.set_captains(self.token, event_id, [self.ids[0], self.ids[5]])
        self.service.auto_balance(self.token, event_id)
        self.service.build_bracket(self.token, event_id)
        self.service.confirm_bracket(self.token, event_id)
        restored = self.service.get_event(event_id)
        self.assertEqual(restored["status"], "READY")
        player = next(player for player in restored["players"] if player["member_id"] == first)
        self.assertEqual((player["attendance_status"], player["attendance_requested_at"], player["attendance_confirmed_at"]), ("ABSENT", requested_at, confirmed_at))
        self.assertIn(legacy_audit, restored["audit"])

    def test_balancing_fixes_captains_positions_and_locks_after_start(self):
        for count in (2, 4):
            event_id = self.team_building(count, "BALANCE", "SINGLE" if count == 2 else "TOURNAMENT")
            self.service.auto_balance(self.token, event_id)
            event = self.service.get_event(event_id)
            for team in event["teams"]:
                self.assertIn(team["captain_id"], [p["member_id"] for p in team["players"]])
                self.assertEqual({p["role"] for p in team["players"]}, set(ROLES))
            with self.assertRaises(ValueError):
                self.service.unassign_player(self.token, event_id, event["teams"][0]["captain_id"])
            self.service.build_bracket(self.token, event_id)
            self.assertEqual(self.service.get_event(event_id)["status"], "BRACKET_SETUP")
            game = self.service.get_event(event_id)["games"][0]
            with self.assertRaises(ValueError):
                self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
            self.service.confirm_bracket(self.token, event_id)
            with self.assertRaises(ValueError):
                self.comp.swap_players(self.token, event_id, event["teams"][0]["captain_id"], event["teams"][1]["captain_id"])
            self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
            with self.assertRaises(ValueError):
                self.service.exclude_participant(self.token, event_id, self.ids[1], "늦은 변경")

    def test_manual_roster_validation_and_exclusion_reset_only_unplayed(self):
        event_id = self.team_building(2, "MANUAL", "SINGLE")
        teams = self.service.get_event(event_id)["teams"]
        with self.assertRaises(ValueError):
            self.service.build_bracket(self.token, event_id)
        for index, team in enumerate(teams):
            for member in self.ids[index * 5 + 1:index * 5 + 5]:
                self.service.assign_player(self.token, event_id, member, team["id"])
        with self.assertRaises(ValueError):
            self.service.assign_player(self.token, event_id, self.ids[1], teams[1]["id"])
        frozen = self.service.get_event(event_id)["players"]
        for action in (self.service.build_bracket, self.service.confirm_bracket):
            # Current eligibility is checked at BOTH boundaries; a historical
            # score still belongs to the confirmed participant snapshot.
            with self.core.transaction() as conn:
                conn.execute("UPDATE members SET status='KICKED' WHERE id=?", (self.ids[1],))
            with self.assertRaisesRegex(ValueError, "승인"):
                action(self.token, event_id)
            with self.core.transaction() as conn:
                conn.execute("UPDATE members SET status='APPROVED',base_score=999 WHERE id=?", (self.ids[1],))
            from unittest.mock import patch
            with patch.object(self.comp, "_player_snapshot", side_effect=AssertionError("per-player read")):
                action(self.token, event_id)
            self.assertEqual(self.service.get_event(event_id)["players"], frozen)
        self.service.exclude_participant(self.token, event_id, self.ids[1], "시작 전 불참")
        event = self.service.get_event(event_id)
        self.assertEqual(event["status"], "RECRUITING")
        self.assertEqual(event["games"], [])
        self.assertEqual(event["teams"], [])
        self.assertEqual(len(event["players"]), 9)
        self.assertEqual(self.core.list_games(), [])

    def test_live_guards_budgets_role_repair_and_four_team_rankings(self):
        event_id = self.team_building(format_name="RANKING")
        before = {m: self.core.get_member(m)["score"] for m in self.ids[:20]}
        event = self.service.get_event(event_id)
        self.assertEqual(event["teams"][0]["budget"], auction_budget(before[self.ids[0]]))
        self.live_complete(event_id)
        event = self.service.get_event(event_id)
        self.assertEqual(event["status"], "BRACKET_SETUP")
        self.assertEqual(event["games"], [])
        with self.assertRaises(ValueError):
            self.comp.draw_player(self.token, event_id)
        with self.assertRaises(ValueError):
            self.comp.move_player(self.token, event_id, self.ids[1], event["teams"][1]["id"], 0, "우회 시도")
        with self.assertRaises(ValueError):
            self.comp.finalize_auction(self.token, event_id)
        with self.assertRaises(ValueError):
            self.service.exclude_participant(self.token, event_id, self.ids[1], "경매 후 명단 제거")
        self.service.set_player_role(self.token, event_id, self.ids[1], "TOP")
        with self.assertRaises(ValueError):
            self.service.build_bracket(self.token, event_id)
        self.service.set_player_role(self.token, event_id, self.ids[1], "JG")
        self.service.build_bracket(self.token, event_id)
        games = self.service.get_event(event_id)["games"]
        self.service.build_bracket(self.token, event_id)  # no reseeding on repeated request
        self.assertEqual(games, self.service.get_event(event_id)["games"])
        self.service.confirm_bracket(self.token, event_id)
        semis = [g for g in games if g["stage"] == "MAIN"]
        for game in semis:
            self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        self.comp.record_result(self.token, event_id, semis[0]["id"], semis[0]["team_b"], reason="준결승 승리팀 입력 정정")
        event = self.service.get_event(event_id)
        final = next(g for g in event["games"] if g["stage"] == "FINAL")
        third = next(g for g in event["games"] if g["stage"] == "THIRD_PLACE")
        self.assertEqual({final["team_a"], final["team_b"]}, {semis[0]["team_b"], semis[1]["team_a"]})
        self.assertEqual({third["team_a"], third["team_b"]}, {semis[0]["team_a"], semis[1]["team_b"]})
        self.comp.record_result(self.token, event_id, final["id"], final["team_a"])
        with self.assertRaises(ValueError):
            self.comp.finalize_event(self.token, event_id)
        self.comp.record_result(self.token, event_id, third["id"], third["team_b"])
        with self.assertRaises(ValueError):
            self.comp.record_result(self.token, event_id, semis[0]["id"], semis[0]["team_a"], reason="후속 경기가 완료됨")
        winner = self.comp.finalize_event(self.token, event_id)
        self.assertEqual(winner, final["team_a"])
        event = self.service.get_event(event_id)
        self.assertEqual([r["rank"] for r in event["final_rankings"]], [1, 2, 3, 4])
        appearances = Counter(t for g in event["games"] for t in (g["team_a"], g["team_b"]))
        self.assertEqual(set(appearances.values()), {2})
        for member in before:
            self.assertEqual(self.core.get_member(member)["score"], before[member])
        with closing(self.core.connect()) as conn:
            awards = list(conn.execute("SELECT l.* FROM award_ledger l JOIN award_batches b ON b.id=l.batch_id WHERE b.event_id=?", (str(event_id),)))
            self.assertEqual(len(awards), 5)
            self.assertTrue(all(a["units"] == 1 for a in awards))
        self.assertEqual(self.comp.finalize_event(self.token, event_id), winner)

    def test_team_role_form_validates_complete_roster_before_saving(self):
        event_id = self.team_building()
        self.live_complete(event_id)
        event = self.service.get_event(event_id)
        team = event["teams"][0]
        original = {player["member_id"]: player["role"] for player in team["players"]}
        members = list(original)
        desired = dict(original)
        desired[members[0]], desired[members[1]] = desired[members[1]], desired[members[0]]
        wrong_team = dict(desired)
        wrong_team[self.ids[5]] = wrong_team.pop(members[-1])
        duplicates = dict(desired)
        duplicates[members[0]] = duplicates[members[1]]
        before_audit = len(event["audit"])
        for invalid in ({key: value for key, value in desired.items() if key != members[-1]}, wrong_team, duplicates):
            with self.assertRaises(ValueError):
                self.service.set_team_roles(self.token, event_id, team["id"], invalid)
            saved = self.service.get_event(event_id)
            self.assertEqual({p["member_id"]: p["role"] for p in saved["teams"][0]["players"]}, original)
            self.assertEqual(len(saved["audit"]), before_audit)
        with self.assertRaises(PermissionError):
            self.service.set_team_roles(self.captain_tokens[self.ids[0]], event_id, team["id"], desired)
        with self.assertRaises(PermissionError):
            self.service.set_team_roles(self.other, event_id, team["id"], desired)
        self.service.set_team_roles(self.token, event_id, team["id"], desired)
        saved = self.service.get_event(event_id)
        self.assertEqual({p["member_id"]: p["role"] for p in saved["teams"][0]["players"]}, desired)
        self.assertEqual(len(saved["audit"]), before_audit + 1)
        self.assertEqual([(p["member_id"], p["price"], p["score"]) for p in saved["teams"][0]["players"]], [(p["member_id"], p["price"], p["score"]) for p in team["players"]])
        self.service.set_team_roles(self.token, event_id, team["id"], desired)
        self.assertEqual(len(self.service.get_event(event_id)["audit"]), before_audit + 1)
        self.service.build_bracket(self.token, event_id)
        self.service.confirm_bracket(self.token, event_id)
        with self.assertRaises(ValueError):
            self.service.set_team_roles(self.token, event_id, team["id"], original)

    def test_team_roles_roll_back_all_players_and_audit_on_mid_write_failure(self):
        event_id = self.team_building()
        self.live_complete(event_id)
        event = self.service.get_event(event_id)
        team = event["teams"][0]
        original = {player["member_id"]: player["role"] for player in team["players"]}
        members = list(original)
        desired = dict(original)
        desired[members[0]], desired[members[1]] = desired[members[1]], desired[members[0]]
        # Persistent test-only trigger fails the second UPDATE on the fresh
        # service connection, after the first player's role has been written.
        with self.core.transaction() as conn:
            conn.execute(f"CREATE TRIGGER test_role_failure BEFORE UPDATE OF role ON competition_players WHEN OLD.event_id={int(event_id)} AND OLD.member_id={int(members[1])} BEGIN SELECT RAISE(ABORT,'test second role failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.set_team_roles(self.token, event_id, team["id"], desired)
        saved = self.service.get_event(event_id)
        self.assertEqual({p["member_id"]: p["role"] for p in saved["teams"][0]["players"]}, original)
        self.assertEqual(saved["audit"], event["audit"])

    def test_six_eight_team_live_brackets_and_bye_has_no_core_game(self):
        for count in (6, 8):
            event_id = self.team_building(count)
            self.live_complete(event_id)
            self.service.build_bracket(self.token, event_id)
            self.service.confirm_bracket(self.token, event_id)
            event = self.service.get_event(event_id)
            self.assertEqual(sum(g["status"] == "BYE" for g in event["games"]), 2 if count == 6 else 0)
            self.assertTrue(all(g["core_game_id"] is None for g in event["games"]))
            self.finish(event_id)
            self.comp.finalize_event(self.token, event_id)
            event = self.service.get_event(event_id)
            self.assertEqual(sum(g["core_game_id"] is not None for g in event["games"]), count - 1)
            with closing(self.core.connect()) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM award_ledger l JOIN award_batches b ON b.id=l.batch_id WHERE b.event_id=?", (str(event_id),)).fetchone()[0], 5)

    def test_legacy_upgrade_keeps_identity_budgets_and_rejects_started(self):
        event_id = self.comp.create_auction(self.token, self.ids[:20], self.ids[:20:5])
        original = self.comp.get_event(event_id)
        self.service.upgrade_auction(self.token, event_id)
        current = self.comp.get_event(event_id)
        self.assertEqual(current["status"], "AUCTION_READY")
        self.assertEqual(current["id"], original["id"])
        self.assertEqual(current["teams"], original["teams"])
        self.assertEqual(current["players"], original["players"])
        started = self.comp.create_auction(self.token, self.ids[:20], self.ids[:20:5])
        self.comp.draw_player(self.token, started)
        with self.assertRaises(ValueError):
            self.service.upgrade_auction(self.token, started)

    def test_confirmation_freezes_score_and_budget_after_approved_only_selection(self):
        event_id = self.draft()
        self.service.open_recruitment(self.token, event_id)
        pending = self.core.join_member("미승인#KR1", "TOP", "JG")
        with self.assertRaises(ValueError):
            self.service.set_participants(self.token, event_id, [{"member_id": pending, "role": "TOP"}])
        self.service.set_participants(self.token, event_id, self.assignments())
        captain = self.ids[0]
        self.core.adjust_score(self.token, captain, 200, "확정 전 전력 보정")
        self.service.confirm_participants(self.token, event_id)
        self.core.adjust_score(self.token, captain, 100, "확정 이후 전력 보정")
        self.service.set_captains(self.token, event_id, self.ids[:20:5])
        self.service.prepare_auction(self.token, event_id)
        event = self.service.get_event(event_id)
        self.assertEqual(next(p["score"] for p in event["players"] if p["member_id"] == captain), 300)
        self.assertEqual(event["teams"][0]["budget"], auction_budget(300))
        self.assertEqual(self.core.get_member(captain)["score"], 400)
        old = self.comp.create_normal(self.token, self.assignments(10))
        old_auction = self.comp.create_auction(self.token, self.ids[:20], self.ids[:20:5])
        listed = {e["id"]: e for e in self.comp.list_events()}
        self.assertEqual(listed[old]["team_count"], 2)
        self.assertEqual(listed[old_auction]["team_count"], 4)

    def test_reopen_preparation_preserves_budget_history_and_can_complete_again(self):
        unconfigured = self.team_building()
        self.service.prepare_auction(self.token, unconfigured)
        self.service.reopen_preparation(self.token, unconfigured, "명단 확인")
        self.assertEqual(self.comp.get_event(unconfigured)["status"], "RECRUITING")

        event_id = self.team_building()
        self.service.warn_participant(self.token, event_id, self.ids[1], "기존 경고 기록")
        self.service.prepare_auction(self.token, event_id)
        live = LiveAuction(self.core, self.comp)
        teams = self.comp.get_event(event_id)["teams"]
        budgets = {team["id"]: amount for team, amount in zip(teams, (440, 550, 660, 770))}
        original = live.configure(self.token, event_id, bid_seconds=15, team_budgets=budgets)
        self.service.reopen_preparation(self.token, event_id, "팀장과 참가 명단 오선택 수정")
        reopened = self.comp.get_event(event_id)
        self.assertEqual(reopened["status"], "RECRUITING")
        self.assertIsNone(reopened["participants_confirmed_at"])
        self.assertEqual({p["member_id"] for p in reopened["players"]}, set(self.ids[:20]))
        self.assertTrue(all(p["team_id"] is None and p["price"] == 0 for p in reopened["players"]))
        self.assertEqual(next(p for p in reopened["players"] if p["member_id"] == self.ids[1])["warning_count"], 1)
        self.assertEqual(reopened["teams"], [])
        self.assertIsNone(live.get_state(event_id))
        revision = next(row for row in reopened["audit"] if row["action"] == "PREPARATION_REOPENED")
        snapshot = json.loads(revision["detail"])
        self.assertEqual({team["id"]: team["budget"] for team in snapshot["teams"]}, budgets)
        self.assertEqual(snapshot["live_settings"]["bid_seconds"], 15)
        self.assertEqual([lot["id"] for lot in snapshot["auction_order"]], [lot["id"] for lot in original["lots"]])
        self.assertEqual(len(snapshot["live_history"]), 1)
        archived_config = snapshot["live_history"][0]
        self.assertEqual(archived_config["type"], "CONFIGURE")
        self.assertEqual(json.loads(archived_config["detail"])["team_budgets"], {str(key): amount for key, amount in budgets.items()})
        self.service.set_participants(self.token, event_id, list(reversed(self.assignments())))
        self.service.confirm_participants(self.token, event_id)
        self.service.set_captains(self.token, event_id, self.ids[:20:5])
        self.live_complete(event_id)
        self.service.build_bracket(self.token, event_id)
        self.service.confirm_bracket(self.token, event_id)
        self.finish(event_id)
        self.comp.finalize_event(self.token, event_id)
        self.assertEqual(self.comp.get_event(event_id)["status"], "COMPLETED")

    def test_reopen_rejects_unauthorized_started_and_history_tampering_atomically(self):
        event_id = self.team_building()
        self.service.prepare_auction(self.token, event_id)
        live = LiveAuction(self.core, self.comp, clock=lambda: 2_000_000_000)
        live.configure(self.token, event_id)
        for token, reason in ((self.token, ""), (self.host, "다른 진행자"), (self.captain_tokens[self.ids[0]], "회원")):
            before = self.comp.get_event(event_id)
            state = live.get_state(event_id)
            with self.assertRaises((ValueError, PermissionError)):
                self.service.reopen_preparation(token, event_id, reason)
            self.assertEqual(self.comp.get_event(event_id), before)
            self.assertEqual(live.get_state(event_id), state)
        live.start(self.token, event_id)
        with self.assertRaises(ValueError):
            self.service.reopen_preparation(self.token, event_id, "이미 시작")
        with self.core.transaction() as conn:
            conn.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (event_id,))
            conn.execute("UPDATE live_sessions SET status='READY' WHERE event_id=?", (event_id,))
        before = self.comp.get_event(event_id)
        with self.assertRaises(ValueError):
            self.service.reopen_preparation(self.token, event_id, "상태만 되돌려도 시작 이력 보호")
        self.assertEqual(self.comp.get_event(event_id), before)

    def test_close_unfinished_after_departure_preserves_results_and_auction_ledger(self):
        event_id = self.team_building()
        self.live_complete(event_id)
        self.service.build_bracket(self.token, event_id)
        self.service.confirm_bracket(self.token, event_id)
        games = self.comp.get_event(event_id)["games"]
        first, second = [g for g in games if g["team_a"] and g["team_b"]]
        self.comp.record_result(self.token, event_id, first["id"], first["team_a"])
        absent = next(p["member_id"] for p in self.comp.get_event(event_id)["players"] if p["team_id"] == second["team_a"])
        self.core.kick_member(self.token, absent, "경기 도중 참가 중단")
        with self.assertRaises(ValueError):
            self.comp.record_result(self.token, event_id, second["id"], second["team_a"])
        with self.assertRaises(ValueError):
            self.comp.cancel_event(self.token, event_id, "기존 취소는 실경기 보호")
        tables = ("games", "game_players", "game_settlements", "score_ledger", "competition_games", "competition_players", "competition_teams", "live_bids", "live_lots")
        def snapshot():
            with closing(self.core.connect()) as conn:
                return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
        before = snapshot()
        self.comp.close_unfinished(self.token, event_id, "남은 경기를 진행할 수 없어 기록 보존 후 중단")
        self.assertEqual(snapshot(), before)
        event = self.comp.get_event(event_id)
        self.assertEqual(event["status"], "CANCELLED")
        self.assertIsNone(event["winner_team_id"])
        self.assertTrue(any(row["action"] == "CLOSE_UNFINISHED" for row in event["audit"]))
        with closing(self.core.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.comp.finalize_event(self.token, event_id)
        with self.assertRaises(ValueError):
            self.comp.record_result(self.token, event_id, first["id"], first["team_b"], reason="중단 뒤 정정 거부")

    def test_close_unfinished_requires_admin_playing_games_and_no_award(self):
        normal = self.comp.create_normal(self.token, self.assignments(10))
        game = self.comp.get_event(normal)["games"][0]
        self.comp.record_result(self.token, normal, game["id"], game["team_a"])
        self.comp.finalize_event(self.token, normal)
        with self.assertRaises(ValueError):
            self.comp.close_unfinished(self.token, normal, "완료된 일반내전 보호")
        event_id = self.team_building()
        self.live_complete(event_id)
        self.service.build_bracket(self.token, event_id)
        self.service.confirm_bracket(self.token, event_id)
        with self.assertRaises(ValueError):
            self.comp.close_unfinished(self.token, event_id, "실경기 없음")
        first = next(g for g in self.comp.get_event(event_id)["games"] if g["team_a"] and g["team_b"])
        self.comp.record_result(self.token, event_id, first["id"], first["team_a"])
        for token, reason in ((self.token, ""), (self.host, "진행자"), (self.captain_tokens[self.ids[0]], "회원")):
            before = self.comp.get_event(event_id)
            with self.assertRaises((ValueError, PermissionError)):
                self.comp.close_unfinished(token, event_id, reason)
            self.assertEqual(self.comp.get_event(event_id), before)
        self.finish(event_id)
        with self.core.transaction() as conn:
            winner = self.comp.resolve_award_winner(conn, event_id)
            winners = [row[0] for row in conn.execute("SELECT member_id FROM competition_players WHERE event_id=? AND team_id=?", (event_id, winner))]
            self.core.award_tournament(self.token, event_id, winners, 4, "pre-finalize-award", conn=conn)
        with self.assertRaisesRegex(ValueError, "보상"):
            self.comp.close_unfinished(self.token, event_id, "지급 후 거부")
        self.comp.finalize_event(self.token, event_id)
        with self.assertRaises(ValueError):
            self.comp.close_unfinished(self.token, event_id, "완료 후 거부")

    def test_normal_twenty_player_interruption_preserves_actual_scores_and_history(self):
        event_id = self.comp.create_normal(self.token, self.assignments(20), format_name="TOURNAMENT")
        before_scores = {mid: self.core.get_member(mid)["score"] for mid in self.ids[:20]}
        first, second = [g for g in self.comp.get_event(event_id)["games"] if g["team_a"] and g["team_b"]]
        core_game_id = self.comp.record_result(self.token, event_id, first["id"], first["team_a"])
        scores = {mid: self.core.get_member(mid)["score"] for mid in self.ids[:20]}
        self.assertEqual(Counter(scores[mid] - before_scores[mid] for mid in scores), Counter({10: 5, -10: 5, 0: 10}))
        departed = next(p["member_id"] for p in self.comp.get_event(event_id)["players"] if p["team_id"] == second["team_a"])
        self.core.kick_member(self.token, departed, "일반내전 도중 참가 중단")
        with self.assertRaises(ValueError):
            self.comp.record_result(self.token, event_id, second["id"], second["team_a"])
        with self.assertRaises(ValueError):
            self.core.void_game(self.token, core_game_id, "대회 원장 직접 무효 방지")
        with self.assertRaises(ValueError):
            self.comp.cancel_event(self.token, event_id, "실제 경기 점수 보호")
        original = self.comp.get_event(event_id)
        game_before = self.core.get_game(core_game_id)
        with self.assertRaises(PermissionError):
            self.comp.close_unfinished(self.host, event_id, "진행자 중단 종료 거부")
        self.comp.close_unfinished(self.token, event_id, "실제 1경기 기록과 점수 보존 후 중단")
        event = self.comp.get_event(event_id)
        self.assertEqual(event["status"], "CANCELLED")
        self.assertIsNone(event["winner_team_id"])
        self.assertEqual(self.core.get_game(core_game_id), game_before)
        self.assertEqual(event["players"], original["players"])
        self.assertEqual(event["teams"], original["teams"])
        self.assertEqual(event["games"], original["games"])
        self.assertEqual({mid: self.core.get_member(mid)["score"] for mid in self.ids[:20]}, scores)
        self.assertTrue(any(row["action"] == "CLOSE_UNFINISHED" for row in event["audit"]))
        with closing(self.core.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM award_batches WHERE event_id=?", (str(event_id),)).fetchone()[0], 0)
        for operation in (
            lambda: self.comp.record_result(self.token, event_id, second["id"], second["team_a"]),
            lambda: self.comp.record_result(self.token, event_id, first["id"], first["team_b"], reason="중단 뒤 정정"),
            lambda: self.comp.finalize_event(self.token, event_id),
        ):
            with self.assertRaises(ValueError):
                operation()


if __name__ == "__main__":
    unittest.main()
