"""The supplied roster stays isolated while existing rehearsal flows work."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from roly.competition import Competition, ROLES
from roly.core import Core, identity
from roly import demo
from roly.demo import DEMO_DATA_VERSION, DEMO_RIOT_IDS, seed
from roly.live_auction import LiveAuction


EXPECTED_ROSTER = (
    "겨울#kr99", "Kging#kr1", "슬모띵#kr1", "마아먕고로룡#123",
    "야동초등학교#kr1", "메이쥐#kr0", "경 먀#kr1", "남자는티오피#kr1",
    "콩이바람이아빠#KR1", "화려한솔로#외로운청년", "평화파밍사랑#kr4",
    "야수#테토", "부리부리대만왕#kr2", "Siat#kr1", "수 빈#kr111",
    "치원#kr1", "들기름무빙#kr01", "홍시먹다체함#kr0", "라라루루#kr0",
    "정 현#kr2", "오도봉구#kr1", "건동김#KR1",
)


class DemoRosterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="roly-demo-roster-")
        self.addCleanup(directory.cleanup)
        self.core = Core(Path(directory.name) / "demo.sqlite3")
        self.credentials = []
        with patch.object(demo, "_load_riot_snapshot", return_value={}):
            self.token = seed(self.core, credentials=self.credentials)
        self.competition = Competition(self.core)
        self.members = sorted(self.core.list_members(True), key=lambda member: member["id"])

    def test_exact_roster_preserves_spaces_tags_and_approved_members_only(self):
        self.assertEqual(DEMO_RIOT_IDS, EXPECTED_ROSTER)
        self.assertEqual(tuple(member["riot_id"] for member in self.members), EXPECTED_ROSTER)
        self.assertEqual(len(self.core.list_members()), 22)
        self.assertTrue(all(member["status"] == "APPROVED" for member in self.members))
        self.assertTrue(all(member["current_tier"] == "" for member in self.members))
        self.assertTrue(all(member["clan_tier"] in demo.CURRENT_TIERS[2:] for member in self.members))
        self.assertGreaterEqual(DEMO_DATA_VERSION, 4)

    def test_four_normal_events_keep_valid_rosters_and_separate_awards(self):
        events = [event for event in self.competition.list_events() if event["kind"] == "NORMAL"]
        self.assertEqual(Counter(event["status"] for event in events), {"COMPLETED": 3, "READY": 1})
        covered = set()
        for event in events:
            detail = self.competition.get_event(event["id"])
            players = detail["players"]
            self.assertEqual(len({player["member_id"] for player in players}), 10)
            self.assertEqual(Counter(player["role"] for player in players), dict.fromkeys(ROLES, 2))
            for player in players:
                member = self.core.get_member(player["member_id"])
                self.assertEqual(player["riot_id"], member["riot_id"])
                self.assertEqual(player["role"], member["main_role"])
                covered.add(member["riot_id"])
        self.assertEqual(covered, set(EXPECTED_ROSTER))
        self.assertEqual(len(self.core.list_games()), 3)
        self.assertTrue(all(member["award_units"] == 0 for member in self.members))

    def test_six_demo_accounts_keep_captain_participant_and_bid_permissions(self):
        self.assertEqual(len(self.credentials), 6)
        self.assertEqual(len({credential["password"] for credential in self.credentials}), 6)
        self.assertEqual(self.core.session(self.token)["role"], "admin")
        auction = next(event for event in self.competition.list_events() if event["kind"] == "AUCTION")
        detail = self.competition.get_event(auction["id"])
        self.assertEqual(detail["status"], "AUCTION_READY")
        self.assertEqual(len(detail["teams"]), 4)
        self.assertEqual({player["riot_id"] for player in detail["players"]}, set(EXPECTED_ROSTER[:20]))
        expected_captains = {self.members[index]["id"] for index in (0, 5, 10, 15)}
        self.assertEqual({team["captain_id"] for team in detail["teams"]}, expected_captains)
        tokens = {}
        for credential in self.credentials[1:]:
            token = self.core.login(credential["username"], credential["password"])
            actor = self.core.session(token)
            self.assertEqual((actor["role"], actor["member_id"]), ("member", credential["member_id"]))
            tokens[credential["username"]] = token
        self.assertEqual({credential["member_id"] for credential in self.credentials[1:5]}, expected_captains)
        self.assertEqual(self.credentials[-1]["member_id"], self.members[1]["id"])
        clock = [2_000_000_000.0]
        live = LiveAuction(self.core, self.competition, clock=lambda: clock[0])
        live.configure(self.token, auction["id"], bid_seconds=10)
        live.start(self.token, auction["id"])
        lot = live.get_state(auction["id"])["current_lot"]
        with self.assertRaises(PermissionError):
            live.place_bid(tokens["demo_participant"], auction["id"], lot["id"], 10, str(uuid4()))
        live.place_bid(tokens["demo_captain_1"], auction["id"], lot["id"], 10, str(uuid4()))
        current = live.get_state(auction["id"])["current_lot"]
        self.assertEqual(current["highest_bid"], 10)

    def test_postgres_guard_refuses_seed_before_any_write(self):
        operating = Mock(is_postgres=True)
        with self.assertRaisesRegex(ValueError, "로컬 체험 저장소"):
            seed(operating)
        operating.setup_admin.assert_not_called()
        operating.join_member.assert_not_called()


class DemoSnapshotTests(unittest.TestCase):
    def test_clan_tier_offsets_clamp_at_boundaries_and_include_apex_tiers(self):
        for current, delta, expected in (
            ("아이언 4", -2, "아이언 4"), ("아이언 3", -2, "아이언 4"),
            ("골드 2", 1, "골드 1"), ("골드 4", -2, "실버 2"),
            ("다이아몬드 1", 1, "마스터"), ("마스터", -2, "다이아몬드 2"),
            ("마스터", 1, "그랜드마스터"), ("그랜드마스터", -2, "다이아몬드 1"),
            ("그랜드마스터", 1, "챌린저"), ("챌린저", 1, "챌린저"),
        ):
            with self.subTest(current=current, delta=delta), patch.object(demo.secrets, "choice", return_value=delta) as choose:
                self.assertEqual(demo._demo_clan_tier(current), expected)
                choose.assert_called_once_with((1, -2))
        with patch.object(demo.secrets, "choice", return_value="실버 4") as choose:
            self.assertEqual(demo._demo_clan_tier("언랭크"), "실버 4")
            self.assertEqual(demo._demo_clan_tier(""), "실버 4")
            self.assertEqual(choose.call_count, 2)
            self.assertTrue(all(call.args == (demo.CURRENT_TIERS[2:],) for call in choose.call_args_list))

    def test_missing_malformed_or_oversized_snapshot_is_optional(self):
        with tempfile.TemporaryDirectory(prefix="roly-demo-snapshot-file-") as temporary:
            path = Path(temporary) / "snapshot.json"
            with patch.object(demo, "DEMO_RIOT_SNAPSHOT", path):
                self.assertEqual(demo._load_riot_snapshot(), {})
                for content in ("not-json", "[]", '{"profiles":[]}', "[" * 2000, " " * 524289):
                    path.write_text(content, encoding="utf-8")
                    self.assertEqual(demo._load_riot_snapshot(), {})
                expected = {"generated_at": "2026-09-08T10:00:00+00:00", "profiles": {}}
                path.write_text(json.dumps(expected), encoding="utf-8")
                self.assertEqual(demo._load_riot_snapshot(), expected)

    def test_seed_applies_identity_bound_public_snapshot_before_games_once(self):
        stamp = "2026-09-08T10:00:00+00:00"
        profiles = {
            identity(EXPECTED_ROSTER[0])[1]: {"current_tier": "골드 2", "lp": 25,
                "puuid": "private-value-must-not-be-stored", "updated_at": stamp,
                "champions": [{"id": 22, "name": "애쉬", "points": 1000, "level": 4,
                               "icon_url": "https://untrusted.example/image.png"}]},
            identity(EXPECTED_ROSTER[1])[1]: {"current_tier": "마스터", "lp": 200, "rank_wins": 15,
                                            "rank_losses": 10, "summoner_level": 100},
            identity(EXPECTED_ROSTER[2])[1]: {"current_tier": "언랭크", "lp": None},
            identity(EXPECTED_ROSTER[3])[1]: {"current_tier": "골드 2", "lp": -1},
            identity(EXPECTED_ROSTER[4])[1]: {"current_tier": "골드 2", "updated_at": "invalid"},
            "unrelated#kr1": {"current_tier": "챌린저", "lp": 1000},
        }
        document = {"generated_at": stamp, "profiles": profiles,
                    "errors": {identity(EXPECTED_ROSTER[5])[1]: "not_found"}}
        offsets = iter((1, -2))
        def choose_tier(options):
            return next(offsets) if options == (1, -2) else "실버 4"
        with tempfile.TemporaryDirectory(prefix="roly-demo-snapshot-seed-") as temporary:
            core = Core(Path(temporary) / "demo.sqlite3")
            with patch.object(demo, "_load_riot_snapshot", return_value=document) as load, patch.object(
                demo.secrets, "choice", side_effect=choose_tier
            ) as choose, patch("roly.riot_api.load_riot_config", side_effect=AssertionError("No key needed")):
                seed(core)
                members = sorted(core.list_members(), key=lambda member: member["id"])
                self.assertEqual(len(members), 22)
                self.assertEqual([(m["current_tier"], m["current_tier_lp"], m["clan_tier"]) for m in members[:3]],
                                 [("골드 2", 25, "골드 1"), ("마스터", 200, "다이아몬드 2"), ("언랭크", None, "실버 4")])
                self.assertTrue(all(m["current_tier_source"] == "riot" for m in members[:3]))
                self.assertTrue(all(m["current_tier"] == "" and m["clan_tier"] == "실버 4" for m in members[3:]))
                for index, member in enumerate(members):
                    self.assertEqual(member["base_score"], [220, 300, 390, 150, 480][index // 5] + index % 5 * 5)
                    self.assertEqual(member["score"], member["base_score"] + 10 * (member["wins"] - member["losses"]))
                    self.assertEqual((member["main_role"], member["sub_role"]), (ROLES[index % 5], ROLES[(index + 1) % 5]))
                competition = Competition(core)
                for event in competition.list_events():
                    for player in competition.get_event(event["id"])["players"]:
                        member = core.get_member(player["member_id"])
                        self.assertEqual(player["current_tier_snapshot"], member["current_tier"])
                        self.assertEqual(player["clan_tier_snapshot"], member["clan_tier"])
                with closing(core.connect()) as db:
                    rows = list(db.execute("SELECT * FROM riot_profiles ORDER BY member_id"))
                    self.assertEqual(len(rows), 3)
                    self.assertNotIn("puuid", rows[0]["payload"])
                    saved = json.loads(rows[0]["payload"])
                    self.assertEqual(saved["champions"][0]["icon_url"], "")
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM riot_jobs").fetchone()[0], 0)
                before = [(member["id"], member["clan_tier"]) for member in members]
                with self.assertRaises(PermissionError):
                    seed(core)
                self.assertEqual([(member["id"], member["clan_tier"]) for member in sorted(core.list_members(), key=lambda m: m["id"])], before)
                self.assertEqual(load.call_count, 1)
                self.assertEqual(choose.call_count, 22)


if __name__ == "__main__":
    unittest.main()
