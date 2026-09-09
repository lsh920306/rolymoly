"""Member profile changes never rewrite confirmed event/game metadata."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from streamlit.testing.v1 import AppTest

from roly.competition import Competition, ROLES, auction_budget
from roly.core import Core
from roly.member_records import member_records, saved_tier
from roly.tournament import TournamentService


class MemberSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "snapshot-admin-password")
        cls.admin = cls.base.login("admin", "snapshot-admin-password")
        Competition(cls.base)
        cls.ids = []
        for index in range(25):
            mid = cls.base.join_member(f"Snapshot{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            cls.base.approve_member(cls.admin, mid, 100)
            cls.ids.append(mid)
        member = cls.base.get_member(cls.ids[0])
        cls.base.update_member(cls.admin, member["id"], member["riot_id"], member["main_role"], member["sub_role"],
            member["base_score"], "최초 티어", clan_tier="클랜 A", current_tier="골드 2", current_tier_lp=0)

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.core = Core(Path(self.directory.name) / "snapshots.sqlite3")
        with closing(self.core.connect()) as db:
            self.base._keeper.backup(db)
        self.comp = Competition(self.core)
        self.service = TournamentService(self.core, self.comp)

    def assignments(self, count=20, ids=None):
        return [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(ids or self.ids[:count])]

    def update(self, member_id=None, name="Changed#QA", clan="클랜 B", tier="다이아몬드 1", lp=88):
        member = self.core.get_member(member_id or self.ids[0])
        self.core.update_member(self.admin, member["id"], name, member["main_role"], member["sub_role"], member["base_score"],
            "회원 이름·티어 변경", clan_tier=clan, current_tier=tier, current_tier_lp=lp, expected_updated_at=member["updated_at"])

    def player(self, event_id, member_id=None):
        return next(p for p in self.comp.get_event(event_id)["players"] if p["member_id"] == (member_id or self.ids[0]))

    def assert_snapshot(self, player, name="Snapshot0#QA", clan="클랜 A", tier="골드 2", lp=0):
        self.assertEqual((player["riot_id"], player["clan_tier_snapshot"], player["current_tier_snapshot"], player["current_tier_lp_snapshot"]),
                         (name, clan, tier, lp))

    def test_normal_ten_twenty_frozen_profile_survives_rename_results_and_correction(self):
        originals = {}
        for count in (10, 20):
            event_id = self.comp.create_normal(self.admin, self.assignments(count), balanced=False, format_name="TOURNAMENT")
            originals[event_id] = self.comp.get_event(event_id)
            self.assert_snapshot(self.player(event_id))
        self.update()
        self.assertEqual(self.core.get_member(self.ids[0])["score"], 100)
        recorded_ids = []
        for event_id, original in originals.items():
            self.assertEqual(self.comp.get_event(event_id)["roster_token"], original["roster_token"])
            self.assertEqual(self.comp.get_event(event_id)["policy_snapshot"], original["policy_snapshot"])
            self.assert_snapshot(self.player(event_id))
            team = self.player(event_id)["team_id"]
            game = next(g for g in original["games"] if team in (g["team_a"], g["team_b"]) and g["team_a"] and g["team_b"])
            game_id = self.comp.record_result(self.admin, event_id, game["id"], team, expected_roster_token=original["roster_token"])
            recorded_ids.append(game_id)
            player = next(p for p in self.core.get_game(game_id)["players"] if p["member_id"] == self.ids[0])
            self.assert_snapshot(player)
            self.assertEqual(player["delta"], 10)
        self.assertEqual(self.core.get_member(self.ids[0])["score"], 120)
        first_event_id = next(iter(originals))
        event = self.comp.get_event(first_event_id)
        game = event["games"][0]
        losing_team = game["team_b"] if game["winner_team_id"] == game["team_a"] else game["team_a"]
        self.comp.record_result(self.admin, first_event_id, game["id"], losing_team, reason="승리팀 정정", expected_roster_token=event["roster_token"])
        corrected = next(p for p in self.core.get_game(recorded_ids[0])["players"] if p["member_id"] == self.ids[0])
        self.assert_snapshot(corrected)
        self.assertEqual((corrected["delta"], self.core.get_member(self.ids[0])["score"]), (-10, 100))
        history = member_records(self.core, self.ids[0])
        self.assertEqual(history["member"]["riot_id"], "Changed#QA")
        for row in history["games"] + history["tournaments"]:
            self.assert_snapshot(row)

    def test_unplayed_normal_replacement_takes_fresh_profile_and_keeps_policy_score(self):
        event_id = self.comp.create_normal(self.admin, self.assignments(20), balanced=False)
        before = self.comp.get_event(event_id)
        self.update(member_id=self.ids[20], name="Replacement#QA", clan="교체 C", tier="마스터", lp=125)
        replacement = self.assignments(ids=[self.ids[20], *self.ids[1:20]])
        self.comp.replace_normal_roster(self.admin, event_id, replacement, "참가자 교체", before["roster_token"], balanced=False)
        after = self.comp.get_event(event_id)
        self.assert_snapshot(self.player(event_id, self.ids[20]), "Replacement#QA", "교체 C", "마스터", 125)
        self.assertEqual(after["policy_snapshot"], before["policy_snapshot"])
        self.assertNotEqual(after["roster_token"], before["roster_token"])
        self.assertTrue(all(p["score"] == 100 for p in after["players"]))
        self.assertEqual(self.core.list_games(), [])
        self.assertNotIn(self.ids[0], [p["member_id"] for p in after["players"]])
        self.update(member_id=self.ids[20], name="AfterReplacement#QA", clan="최신 D", tier="언랭크", lp=None)
        self.assert_snapshot(self.player(event_id, self.ids[20]), "Replacement#QA", "교체 C", "마스터", 125)

    def test_auction_confirmation_refreshes_profile_then_freezes_captain_valuation(self):
        event_id = self.service.create(self.admin, "티어 확정 경매", "2030-01-01T20:00:00+09:00", build_mode="AUCTION", team_count=4)
        self.service.open_recruitment(self.admin, event_id)
        self.service.set_participants(self.admin, event_id, self.assignments())
        self.assert_snapshot(self.player(event_id))
        self.update()
        self.service.confirm_participants(self.admin, event_id)
        self.assert_snapshot(self.player(event_id), "Changed#QA", "클랜 B", "다이아몬드 1", 88)
        confirmed = self.comp.get_event(event_id)
        self.update(name="Latest#QA", clan="클랜 C", tier="챌린저", lp=1000)
        self.assertEqual(self.comp.get_event(event_id)["roster_token"], confirmed["roster_token"])
        self.service.set_captains(self.admin, event_id, self.ids[:20:5])
        self.assert_snapshot(self.player(event_id), "Changed#QA", "클랜 B", "다이아몬드 1", 88)
        teams = self.comp.get_event(event_id)["teams"]
        self.assertTrue(all(t["budget"] == auction_budget(100) for t in teams))
        self.assertTrue(all(p["score"] == 100 for p in self.comp.get_event(event_id)["players"]))
        self.assertEqual(self.comp.get_event(event_id)["policy_snapshot"], confirmed["policy_snapshot"])
        self.assertEqual(self.core.list_games(), [])
        self.assertEqual(sum(m["award_units"] for m in self.core.list_members()), 0)

    def test_legacy_null_tiers_remain_unknown_and_blank_snapshot_is_not_replaced(self):
        event_id = self.comp.create_normal(self.admin, self.assignments(10), balanced=False)
        blank_member = self.ids[1]
        self.assertEqual(self.player(event_id, blank_member)["current_tier_snapshot"], "")
        self.update(member_id=blank_member, name="NewTier#QA", clan="새 티어", tier="플래티넘 3", lp=20)
        event = self.comp.get_event(event_id)
        game = event["games"][0]
        game_id = self.comp.record_result(self.admin, event_id, game["id"], game["team_a"])
        player = next(p for p in self.core.get_game(game_id)["players"] if p["member_id"] == blank_member)
        self.assert_snapshot(player, "Snapshot1#QA", "", "", None)
        # Simulate a pre-profile schema using only this disposable database.
        with self.core.transaction() as db:
            for column in ("clan_tier_snapshot", "current_tier_snapshot", "current_tier_lp_snapshot"):
                db.execute(f"ALTER TABLE competition_players DROP COLUMN {column}")
            db.execute("UPDATE game_players SET clan_tier_snapshot=NULL,current_tier_snapshot=NULL,current_tier_lp_snapshot=NULL WHERE game_id=? AND member_id=?", (game_id, blank_member))
        Competition(self.core)
        history = member_records(self.core, blank_member)
        self.assertIsNone(history["tournaments"][0]["current_tier_snapshot"])
        self.assertIsNone(history["games"][0]["current_tier_snapshot"])
        self.assertEqual(saved_tier(None), "기록 없음")
        self.assertEqual(saved_tier(""), "미등록")
        self.assertEqual(saved_tier("골드 2", 0), "골드 2 · 0 LP")
        self.assertEqual(self.comp.get_event(event_id)["games"][0]["core_game_id"], game_id)
        self.assertEqual(self.core.get_game(game_id)["revision"], 1)

    def test_member_record_dialog_displays_saved_name_and_tiers_after_rename(self):
        event_id = self.comp.create_normal(self.admin, self.assignments(10), balanced=False)
        game = self.comp.get_event(event_id)["games"][0]
        self.comp.record_result(self.admin, event_id, game["id"], game["team_a"])
        self.update()
        app = AppTest.from_string("""
import streamlit as st
from roly.core import Core
from roly.member_records import show_member_record
show_member_record(Core(st.session_state.path), st.session_state.member_id)
""", default_timeout=30)
        app.session_state["path"] = self.core.db_path
        app.session_state["member_id"] = self.ids[0]
        app.run()
        self.assertFalse(app.exception, [error.message for error in app.exception])
        self.assertEqual(len(app.dataframe), 2)
        for table in app.dataframe:
            self.assertEqual(table.value.iloc[0]["당시 Riot ID"], "Snapshot0#QA")
            self.assertEqual(table.value.iloc[0]["당시 클랜 티어"], "클랜 A")
            self.assertEqual(table.value.iloc[0]["당시 현재 티어"], "골드 2 · 0 LP")
        self.assertTrue(any(row.value == "Changed#QA" for row in app.subheader))


if __name__ == "__main__":
    unittest.main()
