"""Preparation cards show assigned members and bounded cached Riot data only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.competition import Competition
from roly.core import Core, ROLES
from roly.lounge import Lounge
from roly.team_cards import cached_team_profiles, preparation_cards_data, team_cards_html
from roly.tournament import TournamentService
from roly.ui import services, lounge_service


ROOT = Path(__file__).resolve().parents[1]
CARDS_APP = """
import streamlit as st
from roly.tournament_ui import preparation_team_cards
preparation_team_cards(st.session_state.qa_event, st.session_state.qa_core)
"""


class PreparationTeamCardTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="roly-preparation-cards-")
        self.addCleanup(temporary.cleanup)
        self.core = Core(Path(temporary.name) / "isolated.sqlite3")
        self.competition = Competition(self.core)
        self.service = TournamentService(self.core, self.competition)
        Lounge(self.core)
        self.core.setup_admin("admin", "synthetic-card-password")
        self.token = self.core.login("admin", "synthetic-card-password")
        self.ids = []
        for index in range(20):
            main, sub = ROLES[index % 5], ROLES[(index + 1) % 5]
            member_id = self.core.join_member(f"CardMember{index}#QA", main, sub)
            self.core.approve_member(self.token, member_id, 170)
            self.ids.append(member_id)
        first = self.core.get_member(self.ids[0])
        self.core.update_member(self.token, first["id"], first["riot_id"], "TOP", "JG", 170,
                                "snapshot fixture", clan_tier="클랜 실버", current_tier="실버 4", current_tier_lp=12)
        self.event_id = self.service.create(self.token, "Preparation", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                                            build_mode="AUCTION", team_count=4)
        self.service.open_recruitment(self.token, self.event_id)
        self.service.set_participants(self.token, self.event_id,
                                      [{"member_id": member_id, "role": ROLES[index % 5]} for index, member_id in enumerate(self.ids)])
        self.service.confirm_participants(self.token, self.event_id)
        self.service.set_captains(self.token, self.event_id, self.ids[::5])
        self.event = self.service.get_event(self.event_id)
        self.payload = {"current_tier": "골드 2", "lp": 37, "updated_at": "2026-09-09T00:00:00+00:00",
                        "profile_icon_url": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/profileicon/1.png",
                        "puuid": "PRIVATE-PUUID", "api_key": "PRIVATE-KEY",
                        "champions": [{"id": index, "name": f"StoredChampion{index}", "points": 100000 - index,
                                       "level": index, "icon_url": "https://ddragon.leagueoflegends.com/cdn/16.17.1/img/champion/Annie.png"}
                                      for index in range(1, 6)]}
        self.cache()
        services.clear()
        lounge_service.clear()
        self.addCleanup(services.clear)
        self.addCleanup(lounge_service.clear)
        for replacement in (
            patch.dict(os.environ, {"ROLYMOLY_APP_ENVIRONMENT": "local", "ROLYMOLY_DATA_DIR": temporary.name,
                                    "ROLYMOLY_DATABASE_TARGET": self.core.db_path}),
            patch("roly.riot_ui.riot_service", return_value=None),
            patch("roly.riot_api._http_get", side_effect=AssertionError("No HTTP")),
            patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No live settings")),
            patch("roly.storage_config._runtime_document", side_effect=AssertionError("No database settings")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def cache(self):
        member = self.core.get_member(self.ids[0])
        with self.core.transaction() as db:
            db.execute("INSERT INTO riot_profiles(member_id,canonical_id,payload,fetched_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(member_id) DO UPDATE SET canonical_id=excluded.canonical_id,payload=excluded.payload",
                       (member["id"], member["canonical_id"], json.dumps(self.payload), 100.0))

    def healthy(self, app):
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def cards_app(self):
        app = AppTest.from_string(CARDS_APP, default_timeout=30)
        app.session_state.qa_event = self.event
        app.session_state.qa_core = self.core
        return self.healthy(app.run())

    @staticmethod
    def rendered(app):
        return "\n".join(item.proto.body for item in app.get("html") if 'class="roly-preparation-teams"' in item.proto.body)

    def test_real_preparation_route_maps_four_captains_points_and_cached_masteries(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        for key, value in {"space": "운영 공간", "db_path": self.core.db_path, "token": self.token,
                           "focus_event": self.event_id}.items():
            app.session_state[key] = value
        self.healthy(app.run())
        self.healthy(app.switch_page("app_pages/auction.py").run())
        rendered = self.rendered(app)
        for index, team in enumerate(self.event["teams"]):
            self.assertIn(f'data-team-id="{team["id"]}"', rendered)
            self.assertIn(f'>Team {index + 1}</h3>', rendered)
            self.assertIn(f'data-member-id="{team["captain_id"]}"', rendered)
        self.assertEqual(rendered.count('class="prep-count">1/5'), 4)
        self.assertEqual(rendered.count('class="prep-captain-badge"'), 4)
        self.assertEqual(rendered.count('class="prep-mastery"'), 5)
        self.assertEqual(rendered.count('class="prep-main-role"'), 4)
        self.assertEqual(rendered.count('class="prep-sub-role"'), 4)
        self.assertNotIn("경매로 팀원을 배정합니다", rendered)
        self.assertNotIn('class="prep-roles"', rendered)
        self.assertIn("골드 2", rendered)
        self.assertIn("클랜 실버", rendered)
        self.assertIn("포인트", rendered)
        self.assertNotIn("잔액", rendered)
        self.assertNotIn("승률", rendered)
        self.assertNotIn("드래그", rendered)
        self.assertNotIn("PRIVATE-", rendered)
        self.assertEqual(self.service.get_event(self.event_id), self.event)

    def test_bulk_cache_read_is_one_select_and_does_not_mutate_roster_or_cache(self):
        original_connect, statements = self.core.connect, []
        def connect():
            db = original_connect()
            db.set_trace_callback(statements.append)
            return db
        with patch.object(self.core, "connect", side_effect=connect) as opened:
            profiles = cached_team_profiles(self.core, self.event)
        opened.assert_called_once_with()
        self.assertEqual(len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]), 1)
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements))
        self.assertEqual(set(profiles), {self.ids[0]})
        data = preparation_cards_data(self.event, profiles)
        self.assertEqual([card["people"][0]["member_id"] for card in data], self.ids[::5])
        self.assertEqual([card["points"] for card in data], [f"{team['remaining']:,} P" for team in self.event["teams"]])
        self.assertEqual([slot["code"] for slot in data[0]["slots"] if slot["occupied"]], ["TOP"])

    def test_new_identity_cache_never_attaches_to_an_old_roster_name(self):
        member = self.core.get_member(self.ids[0])
        self.core.update_member(self.token, member["id"], "ChangedIdentity#QA", "TOP", "JG", 170, "rename")
        self.cache()
        self.assertEqual(cached_team_profiles(self.core, self.event), {})
        rendered = self.rendered(self.cards_app())
        self.assertIn("CardMember0#QA", rendered)
        self.assertIn("실버 4", rendered)
        self.assertNotIn("골드 2", rendered)
        self.assertNotIn("ChangedIdentity", rendered)
        self.assertNotIn('class="prep-mastery"', rendered)

    def test_personal_main_sub_snapshots_are_separate_from_assigned_team_position(self):
        event = deepcopy(self.event)
        event["teams"][0]["players"][0]["role"] = "MID"
        cards = preparation_cards_data(event)
        person = cards[0]["people"][0]
        self.assertEqual((person["main_role_label"], person["sub_role_label"]), ("탑", "정글"))
        self.assertEqual([slot["code"] for slot in cards[0]["slots"] if slot["occupied"]], ["MID"])
        rendered = team_cards_html(cards)
        self.assertIn('class="prep-main-role">주 탑</span>', rendered)
        self.assertIn('class="prep-sub-role">부 정글</span>', rendered)
        event["teams"][0]["players"][0].update(main_role_snapshot=None, sub_role_snapshot=None)
        unknown = preparation_cards_data(event)[0]["people"][0]
        self.assertEqual((unknown["main_role_label"], unknown["sub_role_label"]), ("기록 없음", "기록 없음"))

    def test_html_escapes_names_and_discards_private_fields_and_untrusted_urls(self):
        event = deepcopy(self.event)
        name = '<img src=x onerror="x">#QA'
        event["teams"][0]["players"][0]["riot_id"] = name
        event["teams"][0]["name"] = '<svg onload="x">'
        profile = deepcopy(self.payload)
        profile["profile_icon_url"] = "javascript:alert(1)"
        profile["champions"][0].update(name='<b>unsafe</b>', icon_url="https://untrusted.example/asset.png")
        rendered = team_cards_html(preparation_cards_data(event, {self.ids[0]: profile}))
        self.assertIn(html.escape(name), rendered)
        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", rendered)
        self.assertNotIn("<svg", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("untrusted.example", rendered)
        self.assertNotIn("PRIVATE-", rendered)

    def test_filled_and_six_eight_teams_show_members_without_empty_slot_controls(self):
        filled = deepcopy(self.event)
        for index, team in enumerate(filled["teams"]):
            team["players"] = deepcopy(self.event["players"][index * 5:index * 5 + 5])
        cards = preparation_cards_data(filled)
        self.assertTrue(all(card["count"] == 5 and card["empty"] == 0 for card in cards))
        rendered = team_cards_html(cards)
        self.assertEqual(rendered.count('class="prep-person"'), 20)
        self.assertEqual(rendered.count('class="prep-main-role"'), 20)
        self.assertEqual(rendered.count('class="prep-sub-role"'), 20)
        self.assertNotIn('class="prep-roles"', rendered)
        self.assertNotIn("경매로 팀원을 배정합니다", rendered)
        for count in (6, 8):
            bigger = dict(self.event, teams=[deepcopy(self.event["teams"][index % 4]) for index in range(count)])
            self.assertEqual(len(preparation_cards_data(bigger)), count)
            self.assertIn(f'>Team {count}</h3>', team_cards_html(preparation_cards_data(bigger)))
        css = (ROOT / "static" / "preparation_teams.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", css)
        self.assertIn("@media (max-width: 720px)", css)

    def test_database_outage_keeps_snapshot_cards_and_does_not_show_raw_error(self):
        with patch.object(self.core, "connect", side_effect=sqlite3.OperationalError("PRIVATE-DB-HOST")):
            app = self.cards_app()
        self.assertTrue(any("명단 정보로 표시" in item.value for item in app.caption))
        rendered = self.rendered(app)
        self.assertEqual(rendered.count('class="prep-team prep-team-'), 4)
        self.assertIn("실버 4", rendered)
        self.assertNotIn("PRIVATE-DB-HOST", rendered + " ".join(item.value for item in app.caption))
