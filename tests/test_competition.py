"""Domain integration checks using isolated SQLite memory databases."""
import unittest
from unittest.mock import patch

from roly.core import Core
from roly.competition import Competition, ROLES, auction_budget, balance_teams


class CompetitionTests(unittest.TestCase):
    def setUp(self):
        self.core = Core(":memory:")
        self.core.setup_admin("admin", "testing-password-only")
        self.token = self.core.login("admin", "testing-password-only")
        self.comp = Competition(self.core)
        self.ids = []
        for i in range(40):
            member = self.core.join_member(f"Test{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5])
            self.core.approve_member(self.token, member, 100 + i)
            self.ids.append(member)

    def tearDown(self):
        self.core._keeper.close()

    def normal(self, count=10, format_name="LEAGUE", token=None):
        return self.comp.create_normal(token or self.token,
            [{"member_id": m, "role": ROLES[i % 5]} for i, m in enumerate(self.ids[:count])],
            balanced=True, format_name=format_name)

    def auction(self, teams=4, format_name="TOURNAMENT", token=None):
        token = token or self.token
        event_id = self.comp.create_auction(token, self.ids[:teams * 5], self.ids[:teams * 5:5], format_name=format_name)
        event = self.comp.get_event(event_id)
        for i, team in enumerate(event["teams"]):
            for member_id in self.ids[i * 5 + 1:i * 5 + 5]:
                self.comp.bid(token, event_id, member_id, team["id"], 10)
        self.comp.finalize_auction(token, event_id)
        return event_id

    def finish(self, event_id, token=None):
        token = token or self.token
        while True:
            games = [g for g in self.comp.get_event(event_id)["games"] if g["status"] == "PENDING" and g["team_a"] and g["team_b"]]
            if not games:
                break
            for game in games:
                self.comp.record_result(token, event_id, game["id"], game["team_a"])

    def test_normal_10_20_balance_and_validation(self):
        for count in (10, 20):
            event = self.comp.get_event(self.normal(count))
            self.assertEqual(len(event["teams"]), count // 5)
            self.assertEqual(len(event["games"]), 1 if count == 10 else 6)
            self.assertEqual(len({p["member_id"] for t in event["teams"] for p in t["players"]}), count)
            for team in event["teams"]:
                self.assertEqual({p["role"] for p in team["players"]}, set(ROLES))
        with self.assertRaises(ValueError):
            self.comp.create_normal(self.token, [{"member_id": self.ids[0], "role": r} for r in ROLES] * 2)
        players = [{"member_id": i, "role": ROLES[i % 5], "score": i // 5 * 100} for i in range(10)]
        result = balance_teams(players)
        self.assertEqual(abs(sum(p["score"] for p in result[0]) - sum(p["score"] for p in result[1])), 100)

    def test_earlier_tiebreak_cannot_be_changed_after_next_batch_exists(self):
        for kind in ("NORMAL", "AUCTION"):
            with self.subTest(kind=kind):
                event_id = self.normal(20) if kind == "NORMAL" else self.auction(format_name="LEAGUE")
                event = self.comp.get_event(event_id)
                a, b, c, _ = [team["id"] for team in event["teams"]]
                cycle = {frozenset((a, b)): a, frozenset((b, c)): b, frozenset((a, c)): c}

                def record_cycle(games):
                    for game in games:
                        pair = frozenset((game["team_a"], game["team_b"]))
                        winner = cycle[pair] if pair in cycle else next(team for team in pair if team in (a, b, c))
                        self.comp.record_result(self.token, event_id, game["id"], winner)

                record_cycle(event["games"])
                self.comp.create_tiebreakers(self.token, event_id)
                first = [game for game in self.comp.get_event(event_id)["games"] if game["stage"] == "TIEBREAK"]
                record_cycle(first)
                self.comp.create_tiebreakers(self.token, event_id)
                for second_batch_completed in (False, True):
                    if second_batch_completed:
                        self.finish(event_id)
                    before = self.comp.get_event(event_id)
                    target = next(game for game in before["games"] if game["id"] == first[0]["id"])
                    source_before = self.core.get_game(target["core_game_id"])
                    corrected = target["team_b"] if target["winner_team_id"] == target["team_a"] else target["team_a"]
                    with self.assertRaisesRegex(ValueError, "후속 동률"):
                        self.comp.record_result(self.token, event_id, target["id"], corrected, reason="first tiebreak result correction")
                    self.assertEqual(self.comp.get_event(event_id), before)
                    self.assertEqual(self.core.get_game(target["core_game_id"]), source_before)

    def test_official_award_blocks_simple_correction_but_allows_identical_retry(self):
        event_id = self.auction()
        self.finish(event_id)
        event = self.comp.get_event(event_id)
        final = event["games"][-1]
        winners = [player["member_id"] for player in event["players"] if player["team_id"] == final["winner_team_id"]]
        with self.core.transaction() as conn:
            self.core.award_tournament(self.token, event_id, winners, 4, "official-before-event-finalize", conn=conn)
        original = self.core.get_game(final["core_game_id"])
        corrected = final["team_b"] if final["winner_team_id"] == final["team_a"] else final["team_a"]
        with self.assertRaisesRegex(ValueError, "보상이 지급"):
            self.comp.record_result(self.token, str(event_id).zfill(3), final["id"], corrected, reason="correction after official award")
        self.assertEqual(self.comp.get_event(event_id), event)
        self.assertEqual(self.core.get_game(final["core_game_id"]), original)
        self.assertEqual(self.comp.record_result(self.token, event_id, final["id"], final["winner_team_id"]), final["core_game_id"])
        self.assertEqual(self.comp.finalize_event(self.token, event_id), final["winner_team_id"])
        self.assertEqual([self.core.get_member(member_id)["award_units"] for member_id in winners], [1] * 5)

    def test_per_game_fixed_score_and_idempotence(self):
        event_id = self.normal()
        event = self.comp.get_event(event_id)
        game = event["games"][0]
        before = {m: self.core.get_member(m)["score"] for m in self.ids[:10]}
        result = self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        self.assertEqual(result, self.comp.record_result(self.token, event_id, game["id"], game["team_a"]))
        winner_ids = {p["member_id"] for t in event["teams"] if t["id"] == game["team_a"] for p in t["players"]}
        for member in before:
            self.assertEqual(self.core.get_member(member)["score"] - before[member], 10 if member in winner_ids else -10)
        self.comp.finalize_event(self.token, event_id)
        self.assertEqual(len(self.core.list_games()), 1)

    def test_policy_changes_apply_to_new_competitions(self):
        old_event = self.normal()
        self.core.set_policy(self.token, k=25)
        new_event = self.normal()
        for event_id, expected_delta in ((old_event, 10), (new_event, 25)):
            event = self.comp.get_event(event_id)
            game = event["games"][0]
            member = next(t["players"][0]["member_id"] for t in event["teams"] if t["id"] == game["team_a"])
            before = self.core.get_member(member)["score"]
            self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
            self.assertEqual(self.core.get_member(member)["score"] - before, expected_delta)

    def test_normal_swap_is_same_role_and_before_first_result(self):
        event_id = self.normal()
        event = self.comp.get_event(event_id)
        first = event["teams"][0]["players"][0]
        second = next(p for p in event["teams"][1]["players"] if p["role"] == first["role"])
        self.comp.swap_players(self.token, event_id, first["member_id"], second["member_id"])
        current = self.comp.get_event(event_id)
        self.assertIn(first["member_id"], [p["member_id"] for p in current["teams"][1]["players"]])
        game = current["games"][0]
        self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        with self.assertRaises(ValueError):
            self.comp.swap_players(self.token, event_id, first["member_id"], second["member_id"])

    def test_auction_budget_draw_refund_and_completion(self):
        self.assertEqual(auction_budget(699), 790)
        self.assertEqual(auction_budget(700), 690)
        event_id = self.comp.create_auction(self.token, self.ids[:20], self.ids[:20:5])
        event = self.comp.get_event(event_id)
        first, second = event["teams"][:2]
        drawn = self.comp.draw_player(self.token, event_id)
        self.assertEqual(drawn["member_id"], Competition(self.core).draw_player(self.token, event_id)["member_id"])
        self.comp.mark_unsold(self.token, event_id, drawn["member_id"])
        with self.assertRaises(ValueError):
            self.comp.finalize_auction(self.token, event_id)
        with self.assertRaises(ValueError):
            self.comp.bid(self.token, event_id, drawn["member_id"], first["id"], first["budget"] + 1)
        self.comp.bid(self.token, event_id, drawn["member_id"], first["id"], 50)
        self.comp.move_player(self.token, event_id, drawn["member_id"], second["id"], 20, "입력 오류 정정")
        after = self.comp.get_event(event_id)
        self.assertEqual(after["teams"][0]["remaining"], first["budget"])
        self.assertEqual(after["teams"][1]["remaining"], second["budget"] - 20)
        with self.assertRaises(ValueError):
            self.comp.move_player(self.token, event_id, first["captain_id"], second["id"], 0, "팀장 이동")
        with self.assertRaises(ValueError):
            self.comp.create_auction(self.token, self.ids[:19], self.ids[:20:5])

    def test_auction_roster_lines_required(self):
        event_id = self.comp.create_auction(self.token, self.ids[:20], self.ids[:20:5])
        event = self.comp.get_event(event_id)
        for i, team in enumerate(event["teams"]):
            for member in self.ids[i * 5 + 1:i * 5 + 5]:
                self.comp.bid(self.token, event_id, member, team["id"], 0)
        self.comp.set_player_role(self.token, event_id, self.ids[1], "TOP")
        with self.assertRaisesRegex(ValueError, "배정"):
            self.comp.finalize_auction(self.token, event_id)
        self.comp.set_player_role(self.token, event_id, self.ids[1], "JG")
        self.comp.finalize_auction(self.token, event_id)

    def test_tournaments_4_6_8_and_awards(self):
        for team_count in (4, 6, 8):
            event_id = self.auction(team_count)
            event = self.comp.get_event(event_id)
            self.assertEqual(sum(g["status"] == "BYE" for g in event["games"]), 2 if team_count == 6 else 0)
            self.finish(event_id)
            games = self.comp.get_event(event_id)["games"]
            self.assertEqual(sum(g["status"] == "COMPLETED" for g in games), team_count - 1)
            winner = self.comp.finalize_event(self.token, event_id)
            self.assertEqual(winner, self.comp.finalize_event(self.token, event_id))
            with self.core.connect() as db:
                units = db.execute("SELECT sum(l.units) FROM award_ledger l JOIN award_batches b ON l.batch_id=b.id WHERE b.event_id=?", (str(event_id),)).fetchone()[0]
                self.assertEqual(units, 5 * {4: 1, 6: 5, 8: 25}[team_count])
            self.assertTrue(all(self.core.get_member(i)["score"] == 100 + index for index, i in enumerate(self.ids)))

    def test_semifinal_correction_propagates_then_blocks_after_final(self):
        event_id = self.normal(20, "TOURNAMENT")
        semifinals = [g for g in self.comp.get_event(event_id)["games"] if g["round"] == 1]
        for game in semifinals:
            self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        first = semifinals[0]
        self.comp.record_result(self.token, event_id, first["id"], first["team_b"], reason="승리팀 오입력")
        final = self.comp.get_event(event_id)["games"][-1]
        self.assertEqual(final["team_a"], first["team_b"])
        self.comp.record_result(self.token, event_id, final["id"], final["team_a"])
        with self.assertRaisesRegex(ValueError, "분쟁"):
            self.comp.record_result(self.token, event_id, first["id"], first["team_a"], reason="후속 경기 완료")
        self.assertEqual(self.comp.get_event(event_id)["games"][-1]["winner_team_id"], final["team_a"])

    def test_league_three_way_tie_requires_played_tiebreak(self):
        event_id = self.normal(20)
        event = self.comp.get_event(event_id)
        a, b, c, d = [t["id"] for t in event["teams"]]
        cycle_winners = {frozenset((a, b)): a, frozenset((b, c)): b, frozenset((a, c)): c}
        for game in event["games"]:
            pair = frozenset((game["team_a"], game["team_b"]))
            winner = next(t for t in pair if t != d) if d in pair else cycle_winners[pair]
            self.comp.record_result(self.token, event_id, game["id"], winner)
        self.assertEqual(sum(s["rank"] == 1 for s in self.comp.get_event(event_id)["standings"]), 3)
        with self.assertRaisesRegex(ValueError, "동률"):
            self.comp.finalize_event(self.token, event_id)
        self.comp.create_tiebreakers(self.token, event_id)
        with self.assertRaises(ValueError):
            self.comp.finalize_event(self.token, event_id)
        for game in self.comp.get_event(event_id)["games"]:
            if game["stage"] == "TIEBREAK":
                self.comp.record_result(self.token, event_id, game["id"], min(game["team_a"], game["team_b"]))
        self.assertEqual(self.comp.finalize_event(self.token, event_id), min(a, b, c))

    def test_group_final_waits_for_both_groups(self):
        for team_count in (6, 8):
            event_id = self.auction(team_count, "GROUP_STAGE")
            initial = self.comp.get_event(event_id)
            final = next(g for g in initial["games"] if g["stage"] == "FINAL")
            self.assertIsNone(final["team_a"])
            with self.assertRaises(ValueError):
                self.comp.record_result(self.token, event_id, final["id"], initial["teams"][0]["id"])
            for game in initial["games"]:
                if game["stage"] == "MAIN":
                    self.comp.record_result(self.token, event_id, game["id"], min(game["team_a"], game["team_b"]))
            final = next(g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "FINAL")
            self.assertIsNotNone(final["team_a"])
            self.assertIsNotNone(final["team_b"])
            self.comp.record_result(self.token, event_id, final["id"], final["team_a"])
            self.comp.finalize_event(self.token, event_id)

    def test_creator_and_admin_permissions(self):
        self.core.create_account(self.token, "host1", "testing-password-only")
        self.core.create_account(self.token, "host2", "testing-password-only")
        host1 = self.core.login("host1", "testing-password-only")
        host2 = self.core.login("host2", "testing-password-only")
        event_id = self.normal(token=host1)
        game = self.comp.get_event(event_id)["games"][0]
        with self.assertRaises(PermissionError):
            self.comp.record_result(host2, event_id, game["id"], game["team_a"])
        with self.assertRaises(PermissionError):
            self.comp.record_result(None, event_id, game["id"], game["team_a"])
        self.comp.record_result(host1, event_id, game["id"], game["team_a"])
        with self.assertRaises(PermissionError):
            self.comp.record_result(host1, event_id, game["id"], game["team_b"], reason="정정")
        self.comp.record_result(self.token, event_id, game["id"], game["team_b"], reason="관리자 정정")

    def test_atomic_result_and_award_rollback(self):
        event_id = self.normal()
        game = self.comp.get_event(event_id)["games"][0]
        with patch.object(self.comp, "_audit", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(RuntimeError):
                self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        self.assertEqual(self.core.list_games(), [])
        self.assertEqual(self.comp.get_event(event_id)["games"][0]["status"], "PENDING")
        auction = self.auction()
        self.finish(auction)
        with patch.object(self.comp, "_audit", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(RuntimeError):
                self.comp.finalize_event(self.token, auction)
        self.assertEqual(self.comp.get_event(auction)["status"], "PLAYING")
        self.assertTrue(all(m["award_units"] == 0 for m in self.core.list_members()))
        self.comp.finalize_event(self.token, auction)


if __name__ == "__main__":
    unittest.main()
