"""The shared profile route reads only approved, identity-bound database data."""
import html
import json
import os
from pathlib import Path
import tempfile
import unittest
from roly.member_ranks import save_riot_profile
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.lounge import Lounge
from roly.member_profile_page import profile_header
from roly.riot_profile import public_profile
from roly.ui import services, lounge_service


ROOT = Path(__file__).resolve().parents[1]


class ProfilePageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="roly-profile-page-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "profile.sqlite3"
        self.core = Core(self.path)
        Competition(self.core)
        Lounge(self.core)
        self.core.setup_admin("profile-admin", "synthetic-profile-password")
        self.token = self.core.login("profile-admin", "synthetic-profile-password")
        self.mid = self.core.join_member("ProfileMember#QA", "TOP", "JG")
        self.core.approve_member(self.token, self.mid, 123, notes="private-operator-note")
        member = self.core.get_member(self.mid)
        self.core.update_member(self.token, self.mid, member["riot_id"], "TOP", "JG", 123,
                                "profile fixture", clan_tier="클랜 다이아", current_tier="실버 4", current_tier_lp=9)
        self.payload = {"current_tier": "골드 2", "lp": 37, "summoner_level": 456,
                        "rank_wins": 8, "rank_losses": 2,
                        "profile_icon_url": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/profileicon/1.png",
                        "champions": [{"id": 1, "name": "애니", "points": 123456, "level": 17,
                                       "icon_url": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/champion/Annie.png"}],
                        "updated_at": "2026-09-08T02:03:04+00:00", "puuid": "private-puuid"}
        services.clear()
        lounge_service.clear()
        self.addCleanup(services.clear)
        self.addCleanup(lounge_service.clear)
        for replacement in (
            patch.dict(os.environ, {"ROLYMOLY_DATABASE_TARGET": str(self.path), "ROLYMOLY_DATA_DIR": folder.name}),
            patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("real Riot settings must not be read")),
            patch("roly.riot_api.RiotClient", side_effect=AssertionError("profile render must not request Riot")),
            patch("roly.storage_config._runtime_document", side_effect=AssertionError("real database settings must not be read")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def cache(self, *, canonical=None, payload=None):
        canonical = canonical or self.core.get_member(self.mid)["canonical_id"]
        with self.core.transaction() as db:
            if canonical != self.core.get_member(self.mid, db)["canonical_id"]:
                # Simulate a stale legacy rich cache without replacing the
                # current identity's normalized/manual rank.
                db.execute("INSERT INTO riot_profiles(member_id,canonical_id,payload,fetched_at) VALUES(?,?,?,?)",
                           (self.mid, canonical, json.dumps(self.payload), 1788832984.0))
                return
            save_riot_profile(db, self.mid, canonical, self.payload if payload is None else payload, 1788832984.0)

    def app(self, member_id, *, profile_database=None):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25)
        app.session_state.space = "운영 공간"
        app.session_state.db_path = str(self.path)
        app.session_state.token = None
        if profile_database is not None:
            app.session_state.profile_database = profile_database
        app.run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        app.query_params["member"] = str(member_id)
        app.switch_page("app_pages/profile.py").run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        self.assertFalse(app.error, [item.value for item in app.error])
        return app

    @staticmethod
    def header(app):
        return "\n".join(item.proto.body for item in app.get("html")
                         if 'class="roly-member-profile"' in item.proto.body)

    def test_direct_hidden_route_uses_cached_rank_real_record_and_native_champion_table(self):
        self.cache()
        app = self.app(self.mid)
        header = self.header(app)
        self.assertIn("ProfileMember", header)
        self.assertIn("골드 2", header)
        self.assertIn("37 LP", header)
        self.assertIn("레벨 456", header)
        self.assertIn("10경기 · 8승 2패 · 80%", header)
        self.assertNotIn("클랜 다이아", header)
        self.assertNotIn("KDA", header)
        self.assertNotIn("private-puuid", header)
        caption = next(item for item in app.caption if "전력점수" in item.value)
        self.assertIn("클랜 티어 클랜 다이아", caption.value)
        self.assertIn("전력점수 123 P", caption.value)
        self.assertIn("일반내전 0승 0패", caption.value)
        self.assertEqual(len(app.dataframe), 1)
        frame = app.dataframe[0]
        self.assertEqual(frame.value["챔피언"].tolist(), ["애니"])
        self.assertEqual(frame.value["숙련도 점수"].tolist(), [123456])
        self.assertEqual(frame.value["숙련도 레벨"].tolist(), [17])
        self.assertFalse(any("KDA" in column or "승률" in column for column in frame.value.columns))
        columns = json.loads(frame.proto.columns)
        self.assertEqual(columns["숙련도 점수"]["type_config"]["format"], "localized")
        self.assertTrue(any("KDA와 다른 지표" in item.value for item in app.caption))
        self.assertFalse(any("private-operator-note" in item.value for item in app.caption))

    def test_profile_renders_five_cached_champions_and_both_rank_panels_without_http(self):
        champions = [{"id": index, "name": f"저장 챔피언 {index}", "points": 90000 - index * 1000,
                      "level": 20 - index, "icon_url": "", "puuid": "private-champion-field"}
                     for index in range(1, 6)]
        payload = dict(self.payload, champions=champions, flex_current_tier="플래티넘 3", flex_lp=64,
                       flex_rank_wins=12, flex_rank_losses=7, api_key="private-cached-key")
        self.cache(payload=payload)
        app = self.app(self.mid)
        header = self.header(app)
        solo, flex = header.split('>솔로랭크<', 1)[1].split('>자유랭크<', 1)
        self.assertIn("골드 2", solo)
        self.assertIn("37 LP", solo)
        self.assertIn("10경기 · 8승 2패 · 80%", solo)
        self.assertIn("플래티넘 3", flex)
        self.assertIn("64 LP", flex)
        self.assertIn("19경기 · 12승 7패 · 63%", flex)
        self.assertEqual(len(app.dataframe), 1)
        table = app.dataframe[0].value
        self.assertEqual(table["챔피언"].tolist(), [champion["name"] for champion in champions])
        self.assertEqual(table["숙련도 점수"].tolist(), [champion["points"] for champion in champions])
        self.assertEqual(table["숙련도 레벨"].tolist(), [champion["level"] for champion in champions])
        self.assertEqual(len(table), 5)
        self.assertFalse(any("KDA" in column or "승률" in column for column in table.columns))
        rendered = header + table.to_json(force_ascii=False) + " ".join(item.value for item in app.caption)
        self.assertNotIn("private-", rendered)
        self.assertFalse(any((button.key or "").startswith("riot_refresh_") for button in app.button))

    def test_missing_rank_record_stays_empty_but_real_zero_is_displayed(self):
        member = self.core.get_member(self.mid)
        missing = {key: value for key, value in self.payload.items() if key not in ("rank_wins", "rank_losses", "summoner_level")}
        header = profile_header(member, public_profile(missing))
        self.assertNotIn("경기 ·", header)
        self.assertNotIn("레벨", header)
        zero = public_profile(dict(self.payload, rank_wins=0, rank_losses=0, summoner_level=0))
        header = profile_header(member, zero)
        self.assertIn("0경기 · 0승 0패", header)
        self.assertIn("레벨 0", header)
        self.assertNotIn("0%", header)
        invalid = public_profile(dict(self.payload, rank_wins=None, rank_losses=None, summoner_level=None))
        self.assertNotIn("경기 ·", profile_header(member, invalid))

    def test_names_are_html_escaped_and_clan_tier_remains_plain_caption(self):
        member = self.core.get_member(self.mid)
        name, tag, clan = '<svg onload="x">', '<b>QA</b>', '<img src=x onerror=x>'
        self.core.update_member(self.token, self.mid, name + "#" + tag, "TOP", "JG", 123,
                                "escaping fixture", clan_tier=clan)
        self.cache(payload=dict(self.payload, profile_icon_url='javascript:alert("x")'))
        app = self.app(self.mid)
        header = self.header(app)
        self.assertIn(html.escape(name), header)
        self.assertIn(html.escape(tag), header)
        self.assertNotIn("<svg", header)
        self.assertNotIn("<b>", header)
        self.assertNotIn("javascript:", header)
        self.assertNotIn(clan, header)
        caption = next(item for item in app.caption if "전력점수" in item.value)
        self.assertIn(clan, caption.value)
        self.assertFalse(caption.proto.allow_html)

    def test_solo_and_flex_use_separate_tiers_and_records_and_unknown_is_not_unranked(self):
        payload = dict(self.payload, flex_current_tier="에메랄드 3", flex_lp=56,
                       flex_rank_wins=3, flex_rank_losses=2)
        self.cache(payload=payload)
        header = self.header(self.app(self.mid))
        solo, flex = header.split('>솔로랭크<', 1)[1].split('>자유랭크<', 1)
        self.assertIn("골드 2", solo)
        self.assertIn("37 LP", solo)
        self.assertIn("10경기 · 8승 2패 · 80%", solo)
        self.assertIn("에메랄드 3", flex)
        self.assertIn("56 LP", flex)
        self.assertIn("5경기 · 3승 2패 · 60%", flex)
        self.assertNotIn("골드 2", flex)
        member = self.core.get_member(self.mid)
        unknown = profile_header(member, public_profile(self.payload)).split('>자유랭크<', 1)[1]
        self.assertIn("미입력", unknown)
        self.assertNotIn("언랭크", unknown)
        unranked = profile_header(member, public_profile(dict(payload, flex_current_tier="언랭크", flex_lp=None,
                                 flex_rank_wins=0, flex_rank_losses=0))).split('>자유랭크<', 1)[1]
        self.assertIn("언랭크", unranked)
        self.assertIn("0승 0패", unranked)
        self.assertNotIn("0%", unranked)

    def test_uncached_or_stale_identity_cache_never_fabricates_api_information(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                if stale:
                    self.cache(canonical="previousidentity#qa")
                app = self.app(self.mid)
                header = self.header(app)
                self.assertIn("실버 4", header)
                self.assertIn("수기 입력", header)
                self.assertNotIn("골드 2", header)
                self.assertNotIn("레벨 456", header)
                self.assertNotIn("경기 ·", header)
                self.assertFalse(app.dataframe)
                self.assertTrue(any("아직 저장된 Riot 정보가 없습니다" in item.value for item in app.caption))

    def test_unknown_pending_and_kicked_members_cannot_publish_profile(self):
        pending = self.core.join_member("UnapprovedProfile#QA", "MID", "SUP")
        self.cache()
        for member_id in (999999, pending, self.mid):
            with self.subTest(member_id=member_id):
                if member_id == self.mid:
                    self.core.kick_member(self.token, self.mid, "profile visibility fixture")
                app = self.app(member_id)
                self.assertFalse(self.header(app))
                self.assertFalse(app.dataframe)
                self.assertTrue(any("회원을 찾을 수 없습니다" in item.value or "현재 활동 중인 회원" in item.value
                                    for item in app.info))

    def test_profile_from_different_database_requires_reselection(self):
        self.cache()
        app = self.app(self.mid, profile_database="another-isolated-database.sqlite3")
        self.assertFalse(self.header(app))
        self.assertFalse(app.dataframe)
        self.assertTrue(any("이용 공간이 변경" in item.value for item in app.info))

    def test_team_history_restores_saved_identity_teams_and_sale_points_without_new_requests(self):
        comp = Competition(self.core)
        ids = [self.mid]
        for index in range(1, 20):
            member_id = self.core.join_member(f"ProfileHistory{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.token, member_id, 100)
            ids.append(member_id)
        normal_id = comp.create_normal(self.token, [
            {"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(ids[:10])
        ], title="Saved normal history")
        auction_id = comp.create_auction(self.token, ids, [ids[1], ids[5], ids[10], ids[15]],
                                         title="Saved auction history", format_name="TOURNAMENT")
        team = comp.get_event(auction_id)["teams"][0]
        comp.bid(self.token, auction_id, self.mid, team["id"], 42)
        comp.bid(self.token, auction_id, ids[3], team["id"], 0)
        self.core.update_member(self.token, self.mid, "CurrentProfileName#QA", "MID", "SUP", 200,
                                "rename after saved roster", clan_tier="새 클랜 티어",
                                current_tier="다이아몬드 1", current_tier_lp=88)
        original = comp.get_event(auction_id)
        from roly.member_records import member_records
        with patch("roly.member_profile_page.member_records", wraps=member_records) as loaded:
            app = self.app(self.mid)
        loaded.assert_called_once()
        history = next(frame.value for frame in app.dataframe if "낙찰 포인트" in frame.value.columns)
        self.assertEqual(len(history), 2)
        self.assertEqual(set(history["구분"]), {"일반내전", "경매"})
        self.assertEqual(set(history["당시 Riot ID"]), {"ProfileMember#QA"})
        self.assertEqual(set(history["당시 클랜 티어"]), {"클랜 다이아"})
        self.assertEqual(set(history["당시 현재 티어"]), {"실버 4 · 9 LP"})
        self.assertEqual(set(history["당시 전력"]), {123})
        sale = history.loc[history["구분"] == "경매"].iloc[0]
        self.assertEqual((sale["역할"], sale["팀"], sale["포지션"], sale["낙찰 포인트"]),
                         ("선수", team["name"], "탑", "42 P"))
        self.assertEqual(history.loc[history["구분"] == "일반내전", "낙찰 포인트"].tolist(), ["—"])
        self.assertIn("CurrentProfileName", self.header(app))
        self.assertNotIn("private-", history.to_json())
        self.assertFalse(any(button.label in ("회원 정보 수정", "닉네임 변경") for button in app.button))
        captain = self.app(ids[1])
        captain_history = next(frame.value for frame in captain.dataframe if "낙찰 포인트" in frame.value.columns)
        captain_sale = captain_history.loc[captain_history["구분"] == "경매"].iloc[0]
        self.assertEqual((captain_sale["역할"], captain_sale["팀"]), ("팀장", team["name"]))
        self.assertEqual(set(captain_history["낙찰 포인트"]), {"—"})
        unassigned = self.app(ids[2])
        unassigned_history = next(frame.value for frame in unassigned.dataframe if "낙찰 포인트" in frame.value.columns)
        unassigned_sale = unassigned_history.loc[unassigned_history["구분"] == "경매"].iloc[0]
        self.assertEqual((unassigned_sale["팀"], unassigned_sale["역할"]), ("미배정", "선수"))
        self.assertEqual(set(unassigned_history["낙찰 포인트"]), {"—"})
        free = self.app(ids[3])
        free_history = next(frame.value for frame in free.dataframe if "낙찰 포인트" in frame.value.columns)
        self.assertEqual(free_history.loc[free_history["구분"] == "경매", "낙찰 포인트"].tolist(), ["0 P"])
        self.assertEqual(comp.get_event(auction_id), original)
        self.assertEqual(comp.get_event(normal_id)["title"], "Saved normal history")
