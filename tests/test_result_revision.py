"""Reviewed auction-result revisions, preserved records and native dialog flow."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import patch

from roly.core import Core
from roly.competition import Competition, ROLES
from roly.result_revision import ResultRevisionService


class ResultRevisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "result-testing-password")
        cls.token = cls.base.login("admin", "result-testing-password")
        Competition(cls.base)
        cls.ids = []
        for i in range(30):
            mid = cls.base.join_member(f"Revision{i}#KR1", ROLES[i % 5], ROLES[(i + 1) % 5])
            cls.base.approve_member(cls.token, mid, 100 + i)
            cls.ids.append(mid)
        cls.base.create_account(cls.token, "otheradmin", "otheradmin-password", role="admin")
        cls.other = cls.base.login("otheradmin", "otheradmin-password")
        cls.base.create_account(cls.token, "host", "host-password-only")
        cls.host = cls.base.login("host", "host-password-only")

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        self.core = Core(":memory:")
        self.base._keeper.backup(self.core._keeper)
        self.comp = Competition(self.core)
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.revisions = ResultRevisionService(self.core, self.comp, clock=lambda: self.now)

    def tearDown(self):
        self.core._keeper.close()

    def event(self, format_name="TOURNAMENT", teams=4):
        event_id = self.comp.create_auction(self.token, self.ids[:teams * 5], self.ids[:teams * 5:5], format_name=format_name)
        for index, team in enumerate(self.comp.get_event(event_id)["teams"]):
            for member_id in self.ids[index * 5 + 1:index * 5 + 5]:
                self.comp.bid(self.token, event_id, member_id, team["id"], 0)
        self.comp.finalize_auction(self.token, event_id)
        return event_id

    def finish(self, event_id):
        while ready := [g for g in self.comp.get_event(event_id)["games"] if g["status"] == "PENDING" and g["team_a"] and g["team_b"]]:
            for game in ready:
                self.comp.record_result(self.token, event_id, game["id"], min(game["team_a"], game["team_b"]))

    def preview(self, event_id, game=None):
        game = game or next(g for g in self.comp.get_event(event_id)["games"] if g["status"] == "COMPLETED")
        opposite = game["team_b"] if game["winner_team_id"] == game["team_a"] else game["team_a"]
        return self.revisions.preview(self.token, event_id, game["id"], opposite, "승리 팀 오입력 확인")

    def snapshot(self, event_id):
        with closing(self.core.connect()) as conn:
            return {"games": [dict(r) for r in conn.execute("SELECT * FROM competition_games WHERE event_id=? ORDER BY id", (event_id,))],
                    "core": [self.core.get_game(r[0]) for r in conn.execute("SELECT id FROM games WHERE tournament_id=? ORDER BY id", (str(event_id),))],
                    "archives": [dict(r) for r in conn.execute("SELECT * FROM competition_game_archives WHERE event_id=? ORDER BY id", (event_id,))]}

    def test_tree_preview_rollback_archive_and_replay_custom_key(self):
        event_id = self.event()
        semis = self.comp.get_event(event_id)["games"][:2]
        for game in semis:
            self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        final = self.comp.get_event(event_id)["games"][-1]
        old_core_id = self.comp.record_result(self.token, event_id, final["id"], final["team_a"], request_key="same-client-key")
        before = self.snapshot(event_id)
        preview = self.preview(event_id)
        self.assertEqual(preview["void_count"], 1)
        self.assertEqual(before, self.snapshot(event_id))
        with patch.object(self.core, "correct_game", side_effect=ValueError("원장 오류 시뮬레이션")):
            with self.assertRaises(ValueError):
                self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        self.assertEqual(before, self.snapshot(event_id))
        result = self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        self.assertEqual(result["voided_core_game_ids"], [old_core_id])
        self.assertEqual(self.core.get_game(old_core_id)["status"], "VOID")
        replacement = self.comp.get_event(event_id)["games"][-1]
        self.assertEqual(replacement["attempt"], 2)
        self.assertEqual(replacement["status"], "PENDING")
        self.assertEqual(replacement["team_a"], semis[0]["team_b"])
        self.assertTrue(all(self.core.get_member(mid)["score"] == 100 + i for i, mid in enumerate(self.ids)))
        with closing(self.core.connect()) as conn:
            counts = [r[0] for r in conn.execute("SELECT COUNT(*) FROM game_players p JOIN games g ON g.id=p.game_id WHERE g.tournament_id=? AND g.status='CONFIRMED' GROUP BY p.member_id", (str(event_id),))]
            self.assertEqual(len(counts), 20)
            self.assertEqual(set(counts), {1})
        self.assertEqual(self.revisions.apply(self.token, preview["preview_token"], confirmed=True), result)
        replay_id = self.comp.record_result(self.token, event_id, replacement["id"], replacement["team_a"], request_key="same-client-key")
        self.assertNotEqual(replay_id, old_core_id)
        labels = self.comp.game_labels()
        self.assertEqual(labels[old_core_id]["team_a_name"], final["team_a_name"])
        self.assertEqual(labels[replay_id]["team_a_name"], replacement["team_a_name"])
        self.comp.finalize_event(self.token, event_id)
        self.assertEqual(sum(m["award_units"] for m in self.core.list_members()), 5)

    def test_ranking_resets_both_final_and_third_place(self):
        event_id = self.event("RANKING")
        self.finish(event_id)
        before = self.comp.get_event(event_id)
        preview = self.preview(event_id)
        self.assertEqual({g["stage"] for g in preview["affected_games"]}, {"FINAL", "THIRD_PLACE"})
        self.assertEqual(preview["void_count"], 2)
        self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        event = self.comp.get_event(event_id)
        self.assertEqual(event["final_rankings"], [])
        for game in event["games"]:
            if game["stage"] != "MAIN":
                self.assertEqual(game["status"], "PENDING")
                self.assertEqual(game["attempt"], 2)
        for game in before["games"]:
            if game["stage"] != "MAIN":
                self.assertEqual(self.core.get_game(game["core_game_id"])["status"], "VOID")
        self.finish(event_id)
        self.comp.finalize_event(self.token, event_id)
        self.assertEqual(len(self.comp.get_event(event_id)["final_rankings"]), 4)

    def test_stale_confirmation_expiry_actor_and_explicit_confirmation(self):
        event_id = self.event()
        first, second = self.comp.get_event(event_id)["games"][:2]
        self.comp.record_result(self.token, event_id, first["id"], first["team_a"])
        preview = self.preview(event_id)
        with self.assertRaises(ValueError):
            self.revisions.apply(self.token, preview["preview_token"])
        with self.assertRaises(ValueError):
            self.revisions.apply(self.other, preview["preview_token"], confirmed=True)
        with self.assertRaises(PermissionError):
            self.revisions.apply(self.host, preview["preview_token"], confirmed=True)
        self.comp.record_result(self.token, event_id, second["id"], second["team_a"])
        before = self.snapshot(event_id)
        with self.assertRaisesRegex(ValueError, "상태가 바뀌"):
            self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        self.assertEqual(before, self.snapshot(event_id))
        fresh = self.preview(event_id)
        self.now += timedelta(minutes=10)
        with self.assertRaisesRegex(ValueError, "시간이 지났"):
            self.revisions.apply(self.token, fresh["preview_token"], confirmed=True)
        fresh = self.preview(event_id)
        admin_id = self.core.session(self.token)["id"]
        self.core.set_account_role(self.other, admin_id, "organizer")
        with self.assertRaises(PermissionError):
            self.revisions.apply(self.token, fresh["preview_token"], confirmed=True)

    def test_normal_completed_and_already_awarded_are_protected(self):
        normal = self.comp.create_normal(self.token, [{"member_id": m, "role": ROLES[i % 5]} for i, m in enumerate(self.ids[:10])])
        game = self.comp.get_event(normal)["games"][0]
        self.comp.record_result(self.token, normal, game["id"], game["team_a"])
        with self.assertRaisesRegex(ValueError, "일반내전"):
            self.preview(normal)
        event_id = self.event()
        self.finish(event_id)
        preview = self.preview(event_id)
        event = self.comp.get_event(event_id)
        final = event["games"][-1]
        winner_ids = [p["member_id"] for p in event["players"] if p["team_id"] == final["winner_team_id"]]
        with self.core.transaction() as conn:
            self.core.award_tournament(self.token, event_id, winner_ids, 4, "early-award", conn=conn)
        with self.assertRaisesRegex(ValueError, "보상이 지급"):
            self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        with self.assertRaisesRegex(ValueError, "보상이 지급"):
            self.preview(event_id)
        complete = self.event()
        self.finish(complete)
        self.comp.finalize_event(self.token, complete)
        with self.assertRaisesRegex(ValueError, "종료된"):
            self.preview(complete)

    def cyclic_results(self, event_id, games, teams):
        a, b, c = teams[:3]
        cycle = {frozenset((a, b)): a, frozenset((b, c)): b, frozenset((a, c)): c}
        for game in games:
            pair = frozenset((game["team_a"], game["team_b"]))
            winner = cycle.get(pair)
            if winner is None:
                winner = next(t for t in pair if t in (a, b, c))
            self.comp.record_result(self.token, event_id, game["id"], winner)

    def test_league_tiebreaks_are_archived_and_recomputed(self):
        self._recompute_archived_league_tiebreaks()

    def test_legacy_fixture_schema_does_not_reuse_archived_game_ids(self):
        # Earlier databases used INTEGER PRIMARY KEY without AUTOINCREMENT.
        # Opening them with the current service must also protect replay keys.
        with closing(self.core.connect()) as conn:
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='competition_games'").fetchone()[0]
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE competition_games")
            conn.execute(schema.replace("AUTOINCREMENT", ""))
        self.comp = Competition(self.core)
        self.revisions = ResultRevisionService(self.core, self.comp, clock=lambda: self.now)
        self._recompute_archived_league_tiebreaks()

    def _recompute_archived_league_tiebreaks(self):
        event_id = self.event("LEAGUE")
        event = self.comp.get_event(event_id)
        teams = [t["id"] for t in event["teams"]]
        self.cyclic_results(event_id, event["games"], teams)
        self.comp.create_tiebreakers(self.token, event_id)
        self.finish(event_id)
        extras = [g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "TIEBREAK"]
        target = next(g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "MAIN" and {g["team_a"], g["team_b"]} == set(teams[:2]))
        preview = self.preview(event_id, target)
        self.assertEqual(preview["removed_tiebreak_count"], 3)
        self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        after = self.comp.get_event(event_id)
        self.assertFalse(any(g["stage"] == "TIEBREAK" for g in after["games"]))
        self.assertTrue(all(self.core.get_game(g["core_game_id"])["status"] == "VOID" for g in extras))
        self.assertEqual(next(row["team_id"] for row in after["standings"] if row["rank"] == 1), teams[1])
        self.assertTrue(all(g["core_game_id"] in self.comp.game_labels() for g in extras))
        # Restoring the earlier MAIN result recreates a three-way tie after the
        # first extra batch was removed. IDs and request keys must remain fresh.
        target = next(g for g in after["games"] if g["id"] == target["id"])
        again = self.preview(event_id, target)
        self.revisions.apply(self.token, again["preview_token"], confirmed=True)
        self.comp.create_tiebreakers(self.token, event_id)
        regenerated = [g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "TIEBREAK"]
        self.assertTrue({g["id"] for g in extras}.isdisjoint(g["id"] for g in regenerated))
        self.finish(event_id)
        new_results = [g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "TIEBREAK"]
        self.assertTrue({g["core_game_id"] for g in extras}.isdisjoint(g["core_game_id"] for g in new_results))
        self.assertTrue(all(self.core.get_game(g["core_game_id"])["status"] == "CONFIRMED" for g in new_results))
        self.comp.finalize_event(self.token, event_id)

    def test_group_correction_preserves_other_group_and_rebuilds_final(self):
        event_id = self.event("GROUP_STAGE", 6)
        original = self.comp.get_event(event_id)
        for group in ("A", "B"):
            games = [g for g in original["games"] if g["group_key"] == group]
            teams = sorted({t for g in games for t in (g["team_a"], g["team_b"])})
            self.cyclic_results(event_id, games, teams)
            self.comp.create_tiebreakers(self.token, event_id, group)
        self.finish(event_id)
        before = self.comp.get_event(event_id)
        target = next(g for g in before["games"] if g["stage"] == "MAIN" and g["group_key"] == "A")
        other_extras = [g for g in before["games"] if g["stage"] == "TIEBREAK" and g["group_key"] == "B"]
        preview = self.preview(event_id, target)
        self.assertEqual(preview["void_count"], 4)
        self.assertEqual(preview["removed_tiebreak_count"], 3)
        self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        after = self.comp.get_event(event_id)
        self.assertTrue(all(g in after["games"] for g in other_extras))
        final = next(g for g in after["games"] if g["stage"] == "FINAL")
        self.assertEqual(final["status"], "PENDING")
        self.assertIsNotNone(final["team_a"])
        self.assertIsNotNone(final["team_b"])
        self.finish(event_id)
        self.comp.finalize_event(self.token, event_id)

    def test_tiebreak_correction_removes_later_batches_only(self):
        event_id = self.event("LEAGUE")
        original = self.comp.get_event(event_id)
        teams = [t["id"] for t in original["teams"]]
        self.cyclic_results(event_id, original["games"], teams)
        self.comp.create_tiebreakers(self.token, event_id)
        first_batch = [g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "TIEBREAK"]
        self.cyclic_results(event_id, first_batch, teams)
        self.comp.create_tiebreakers(self.token, event_id)
        self.finish(event_id)
        target = next(g for g in self.comp.get_event(event_id)["games"] if g["id"] == first_batch[0]["id"])
        preview = self.preview(event_id, target)
        self.assertEqual(preview["removed_tiebreak_count"], 3)
        self.revisions.apply(self.token, preview["preview_token"], confirmed=True)
        remaining = [g for g in self.comp.get_event(event_id)["games"] if g["stage"] == "TIEBREAK"]
        self.assertEqual({g["id"] for g in remaining}, {g["id"] for g in first_batch})
        self.comp.finalize_event(self.token, event_id)


class ResultRevisionUITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())

    def test_native_dialog_requires_confirmation_and_keeps_void_history(self):
        from streamlit.testing.v1 import AppTest
        from roly.ui import services
        from roly.tournament import TournamentService
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="roly-revision-ui-") as temp:
            with patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temp}):
                app = AppTest.from_file(str(root / "app.py"), default_timeout=30).run()
                self.assertFalse(app.exception)
                core = Core(app.session_state["db_path"])
                comp = Competition(core)
                token = app.session_state["token"]
                draft = TournamentService(core, comp).create(token, "대진 없는 경매 초안", "2026-10-02T20:00:00+09:00", build_mode="AUCTION", team_count=4)
                app.session_state["focus_event"] = draft
                app.switch_page("app_pages/events.py").run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertTrue(any("경매 메뉴에서" in item.value for item in app.info))
                members = core.list_members()
                by_role = {r: [m["id"] for m in members if m["main_role"] == r] for r in ROLES}
                ids = [by_role[role][i] for i in range(4) for role in ROLES]
                event_id = comp.create_auction(token, ids, ids[::5], title="결과 정정 화면", format_name="RANKING")
                for i, team in enumerate(comp.get_event(event_id)["teams"]):
                    for mid in ids[i * 5 + 1:i * 5 + 5]:
                        comp.bid(token, event_id, mid, team["id"], 0)
                comp.finalize_auction(token, event_id)
                while ready := [g for g in comp.get_event(event_id)["games"] if g["status"] == "PENDING" and g["team_a"] and g["team_b"]]:
                    for game in ready:
                        comp.record_result(token, event_id, game["id"], game["team_a"])
                app.session_state["focus_event"] = event_id
                app.switch_page("app_pages/events.py").run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                first = comp.get_event(event_id)["games"][0]
                next(box for box in app.selectbox if box.label == "정정 후 승리 팀").select(first["team_b"]).run()
                next(box for box in app.text_input if box.label == "결과 정정 사유").set_value("화면 확인 후 정정").run()
                next(b for b in app.button if b.label == "정정 영향 미리보기").click().run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertTrue(app.button(key="events_revision_apply").disabled)
                self.assertEqual(sum(g["status"] == "COMPLETED" for g in comp.get_event(event_id)["games"]), 4)
                next(c for c in app.checkbox if "무효 처리할 경기" in c.label).check().run()
                app.button(key="events_revision_apply").click().run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertEqual(sum(g["status"] == "PENDING" for g in comp.get_event(event_id)["games"]), 2)
                voided = [g for g in core.list_games() if g["status"] == "VOID" and str(g["tournament_id"]) == str(event_id)]
                self.assertEqual(len(voided), 2)
                app.session_state["events_detail_tab_AUCTION"] = "전체 경기 이력"
                app.run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                history = next(t.value for t in app.dataframe if "진행자" in t.value.columns)
                self.assertEqual(sum(history["상태"] == "무효"), 2)
                self.assertTrue(all("팀" in name for name in history.loc[history["상태"] == "무효", "승리팀"]))
                services.clear()


if __name__ == "__main__":
    unittest.main()
