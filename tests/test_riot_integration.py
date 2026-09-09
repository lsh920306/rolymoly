"""API profiles remain separate from power scores and historical rosters."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from roly.member_ranks import save_riot_profile
from unittest.mock import patch

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.riot_profile import public_profile, member_profiles


PROFILE = {"puuid": "INTERNAL-ONLY", "current_tier": "다이아몬드 2", "lp": 73,
           "flex_current_tier": "플래티넘 1", "flex_lp": 18,
           "flex_rank_wins": 7, "flex_rank_losses": 3,
           "updated_at": "2026-09-08T01:00:00+00:00",
           "profile_icon_url": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/profileicon/1.png",
           "champions": [{"id": 22, "name": "애쉬", "points": 12000, "level": 5,
                          "icon_url": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/champion/Ashe.png"}]}


class RiotIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dir = tempfile.TemporaryDirectory(prefix="roly-riot-integration-")
        cls.base = Core(Path(cls.base_dir.name) / "base.sqlite3")
        cls.base.setup_admin("admin", "synthetic-admin-password")
        cls.token = cls.base.login("admin", "synthetic-admin-password")
        cls.comp = Competition(cls.base)
        cls.ids = []
        for i in range(20):
            mid = cls.base.join_member(f"RiotFixture{i}#QA", ROLES[i % 5], ROLES[(i + 1) % 5])
            cls.base.approve_member(cls.token, mid, 100)
            cls.ids.append(mid)
        cls.event = cls.comp.create_auction(cls.token, cls.ids, cls.ids[::5])
        with cls.base.transaction() as db:
            db.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (cls.event,))

    @classmethod
    def tearDownClass(cls):
        cls.base_dir.cleanup()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="roly-riot-case-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        with closing(self.base.connect()) as source, closing(sqlite3.connect(self.path)) as target:
            source.backup(target)
        self.core = Core(self.path)
        self.competition = Competition(self.core)
        self.live = LiveAuction(self.core, self.competition, clock=lambda: 2_000_000_000.0)
        self.live.configure(self.token, self.event, order=[mid for mid in self.ids if mid not in self.ids[::5]])
        self.live.start(self.token, self.event)
        self.addCleanup(self.live.stop_worker)
        self.mid = self.ids[1]

    def cache(self):
        member = self.core.get_member(self.mid)
        with self.core.transaction() as db:
            save_riot_profile(db, self.mid, member["canonical_id"], PROFILE, 2_000_000_000.0)

    def test_auction_reads_only_cache_without_changing_roster_or_score(self):
        before = self.competition.get_event(self.event)
        self.cache()
        with patch("roly.riot_api.RiotClient.account_by_riot_id", side_effect=AssertionError("HTTP in auction")):
            view = self.live.get_state(self.event)
        lot = view["current_lot"]
        self.assertEqual(lot["riot_profile"]["current_tier"], "다이아몬드 2")
        self.assertEqual((lot["riot_profile"]["flex_current_tier"], lot["riot_profile"]["flex_lp"]), ("플래티넘 1", 18))
        self.assertEqual((lot["riot_profile"]["flex_rank_wins"], lot["riot_profile"]["flex_rank_losses"]), (7, 3))
        self.assertEqual(lot["riot_profile"]["champions"][0]["name"], "애쉬")
        self.assertNotIn("INTERNAL-ONLY", json.dumps(view))
        self.assertNotIn("riot_cache_payload", lot)
        self.assertEqual(self.competition.get_event(self.event)["players"], before["players"])
        self.assertEqual(self.core.get_member(self.mid)["score"], 100)
        self.assertEqual(self.core.get_member(self.mid)["current_tier"], "다이아몬드 2")
        self.assertEqual(member_profiles(self.core, [self.mid])[self.mid], lot["riot_profile"])

    def test_renamed_identity_does_not_show_previous_accounts_profile(self):
        self.cache()
        self.core.update_member(self.token, self.mid, "NewIdentity#QA", "JG", "MID", 100, "verified identity change")
        member = self.core.get_member(self.mid)
        self.assertEqual((member["current_tier"], member["current_tier_lp"], member["current_tier_source"]), ("", None, "manual"))
        self.assertIsNone(self.live.get_state(self.event)["current_lot"]["riot_profile"])
        self.assertEqual(member_profiles(self.core, [self.mid]), {})
        self.core.update_member(self.token, self.mid, "RiotFixture1#QA", "JG", "MID", 100, "identity changed back")
        self.assertEqual(member_profiles(self.core, [self.mid]), {})
        self.assertIsNone(self.live.get_state(self.event)["current_lot"]["riot_profile"])

    def test_api_rank_cannot_be_overwritten_by_manual_member_edit(self):
        self.cache()
        before = self.core.get_member(self.mid)
        with self.assertRaisesRegex(ValueError, "직접 수정"):
            self.core.update_member(self.token, self.mid, before["riot_id"], "JG", "MID", 100, "bad override", current_tier="실버 1")
        self.core.update_member(self.token, self.mid, before["riot_id"], "JG", "MID", 100, "clan review", clan_tier="클랜 골드")
        member = self.core.get_member(self.mid)
        self.assertEqual((member["clan_tier"], member["current_tier"], member["score"]), ("클랜 골드", "다이아몬드 2", 100))

    def test_schema_upgrade_keeps_login_and_member_data(self):
        before = self.core.get_member(self.mid)
        with self.core.transaction() as db:
            for table in ("riot_profiles", "riot_jobs", "riot_rate_hits", "riot_rate_cooldowns"):
                db.execute(f"DROP TABLE {table}")
            db.execute("ALTER TABLE members DROP COLUMN current_tier_source")
        restored = Core(self.path)
        self.assertEqual(restored.get_member(self.mid), before)
        self.assertEqual(restored.session(self.token)["role"], "admin")
        self.assertEqual(member_profiles(restored, [self.mid]), {})

    def test_public_projection_rejects_bad_cache_and_removes_private_fields(self):
        profile = public_profile(PROFILE)
        self.assertNotIn("puuid", profile)
        self.assertEqual(profile["champions"][0]["points"], 12000)
        self.assertIsNone(public_profile("{"))
        self.assertIsNone(public_profile({"current_tier": "made-up"}))
        bad = dict(PROFILE, profile_icon_url="https://evil.example/private.png")
        self.assertEqual(public_profile(bad)["profile_icon_url"], "")
        self.assertIsNone(public_profile(dict(PROFILE, lp=float("nan"))))

    def test_flex_projection_keeps_unknown_and_unranked_distinct_and_sanitized(self):
        legacy = {key: value for key, value in PROFILE.items() if not key.startswith("flex_")}
        profile = public_profile(legacy)
        self.assertFalse(any(key.startswith("flex_") for key in profile))
        empty = public_profile(dict(PROFILE, flex_current_tier="언랭크", flex_lp=None,
                                    flex_rank_wins=0, flex_rank_losses=0, api_key="INTERNAL-KEY"))
        self.assertEqual((empty["flex_current_tier"], empty["flex_lp"], empty["flex_rank_wins"], empty["flex_rank_losses"]),
                         ("언랭크", None, 0, 0))
        self.assertNotIn("INTERNAL", json.dumps(empty))
        for malformed in ({"flex_current_tier": "not-a-tier"}, {"flex_current_tier": None},
                          {"flex_lp": True}, {"flex_lp": -1}, {"flex_lp": 10001},
                          {"flex_current_tier": "언랭크", "flex_lp": 0}):
            with self.subTest(fields=malformed):
                public = public_profile(dict(PROFILE, **malformed))
                self.assertEqual(public["current_tier"], PROFILE["current_tier"])
                self.assertFalse(any(key.startswith("flex_") for key in public))
        invalid_stats = public_profile(dict(PROFILE, flex_rank_wins=True, flex_rank_losses=-1))
        self.assertEqual(invalid_stats["flex_current_tier"], "플래티넘 1")
        self.assertNotIn("flex_rank_wins", invalid_stats)
        self.assertNotIn("flex_rank_losses", invalid_stats)

    def test_public_mastery_cache_keeps_up_to_five_without_synthetic_rows(self):
        for count in (0, 3, 5, 6):
            with self.subTest(count=count):
                champions = [{"id": index, "name": f"챔피언 {index}", "points": 1000 - index,
                              "level": index, "icon_url": "", "puuid": "INTERNAL-CHAMPION"}
                             for index in range(1, count + 1)]
                public = public_profile(dict(PROFILE, champions=champions))
                self.assertEqual([row["id"] for row in public["champions"]], list(range(1, min(count, 5) + 1)))
                self.assertEqual(public["flex_current_tier"], PROFILE["flex_current_tier"])
                self.assertNotIn("INTERNAL", json.dumps(public))
