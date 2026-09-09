"""Real demo identities and settlement, isolated from operating data."""
from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.competition import Competition, ROLES
from roly.core import Core
from roly.demo import seed
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from roly.ui import services

ROOT = Path(__file__).resolve().parents[1]


class DemoAccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="roly-demo-review-")
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.directory.name})
        env.start()
        self.addCleanup(env.stop)
        worker = patch.object(LiveAuction, "ensure_worker")
        worker.start()
        self.addCleanup(worker.stop)
        self.addCleanup(services.clear)

    def healthy(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])

    def test_account_switch_authentication_failure_and_operating_isolation(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.healthy(app)
        demo_path = app.session_state["db_path"]
        self.assertTrue(Path(demo_path).is_relative_to(self.directory.name))
        core = Core(demo_path)
        credentials = app.session_state["demo_accounts"]
        self.assertEqual(len(credentials), 6)
        self.assertEqual(len({entry["password"] for entry in credentials}), 6)
        rendered = "\n".join(str(element.value) for kind in ("caption", "text", "markdown") for element in getattr(app, kind))
        self.assertTrue(all(entry["password"] not in rendered for entry in credentials))
        app.selectbox(key="demo_account_choice").set_value("demo_captain_2").run()
        self.healthy(app)
        captain_token = app.session_state["token"]
        captain = core.session(captain_token)
        self.assertEqual((captain["role"], captain["member_id"]), ("member", credentials[2]["member_id"]))
        with self.assertRaises(PermissionError):
            core.adjust_score(captain_token, credentials[2]["member_id"], 1, "권한 검증")
        app.selectbox(key="demo_account_choice").set_value("demo").run()
        self.healthy(app)
        self.assertEqual(core.session(app.session_state["token"])["role"], "admin")
        self.assertIsNone(core.session(captain_token))
        app.selectbox(key="demo_account_choice").set_value("demo_participant").run()
        app.switch_page("app_pages/auction.py").run()
        self.healthy(app)
        self.assertEqual(core.session(app.session_state["token"])["member_id"], credentials[-1]["member_id"])
        self.assertFalse(any(button.label in ("경매 시작", "경매 설정 저장", "경매 대회 생성") or (button.key or "").startswith("live_bid_") for button in app.button))
        app.selectbox(key="demo_account_choice").set_value("demo").run()
        self.healthy(app)
        admin_token = app.session_state["token"]
        changed = [dict(entry) for entry in credentials]
        changed[1]["password"] = "incorrect-demo-password"
        app.session_state["demo_accounts"] = changed
        app.run()
        app.selectbox(key="demo_account_choice").set_value("demo_captain_1").run()
        self.healthy(app)
        self.assertTrue(app.error)
        self.assertEqual(app.session_state["token"], admin_token)
        self.assertEqual(core.session(app.session_state["token"])["role"], "admin")
        app.session_state["demo_accounts"] = credentials
        app.selectbox(key="space").set_value("운영 공간").run()
        self.healthy(app)
        self.assertIsNone(app.session_state["token"])
        self.assertFalse(any(select.key == "demo_account_choice" for select in app.selectbox))
        operating = Core(app.session_state["db_path"])
        self.assertFalse(operating.has_admin())
        self.assertEqual(operating.list_members(True), [])
        with closing(operating.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
        app.selectbox(key="space").set_value("체험 공간").run()
        self.healthy(app)
        self.assertEqual(app.session_state["db_path"], demo_path)
        self.assertEqual(core.session(app.session_state["token"])["role"], "admin")
        other = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.healthy(other)
        self.assertNotEqual(other.session_state["db_path"], demo_path)
        self.assertNotEqual(other.session_state["demo_accounts"][0]["password"], credentials[0]["password"])

    def test_demo_auction_settlement_award_and_normal_score_are_separate(self):
        core = Core(Path(self.directory.name) / "demo-flow.sqlite3")
        credentials = []
        admin_token = seed(core, credentials=credentials)
        competition = Competition(core)
        preparation = TournamentService(core, competition)
        event = next(event for event in competition.list_events() if event["kind"] == "AUCTION")
        event_id = event["id"]
        prepared = competition.get_event(event_id)
        self.assertEqual(prepared["status"], "AUCTION_READY")
        self.assertEqual((core.policy()["mode"], core.policy()["k"]), ("fixed", 10))
        scores = {member["id"]: member["score"] for member in core.list_members()}
        original_teams = prepared["teams"]
        target_team = {player["member_id"]: original_teams[index // 5]["id"] for index, player in enumerate(sorted(prepared["players"], key=lambda player: player["member_id"]))}
        captain_tokens = {entry["member_id"]: core.login(entry["username"], entry["password"]) for entry in credentials[1:]}
        team_tokens = {team["id"]: captain_tokens[team["captain_id"]] for team in original_teams}
        clock = [2_000_000_000.0]
        live = LiveAuction(core, competition, clock=lambda: clock[0])
        live.configure(admin_token, event_id, bid_seconds=5)
        live.start(admin_token, event_id)
        lot = live.get_state(event_id)["current_lot"]
        with self.assertRaises(PermissionError):
            live.place_bid(admin_token, event_id, lot["id"], 5, str(uuid4()))
        with self.assertRaises(PermissionError):
            live.place_bid(captain_tokens[credentials[-1]["member_id"]], event_id, lot["id"], 5, str(uuid4()))
        for _ in range(16):
            lot = live.get_state(event_id)["current_lot"]
            receipt = live.place_bid(team_tokens[target_team[lot["member_id"]]], event_id, lot["id"], 5, str(uuid4()))
            self.assertEqual(receipt["closes_at"], min(lot["closes_at"] + 5, clock[0] + 5))
            clock[0] = receipt["closes_at"]
            live.settle_due()
            state = live.get_state(event_id)
            if state["status"] != "COMPLETED":
                clock[0] = state["next_at"]
                live.settle_due()
        self.assertEqual(live.get_state(event_id)["status"], "COMPLETED")
        after_auction = competition.get_event(event_id)
        for before, team in zip(original_teams, after_auction["teams"]):
            self.assertEqual(team["remaining"], before["remaining"] - 20)
            self.assertEqual({player["role"] for player in team["players"]}, set(ROLES))
        preparation.build_bracket(admin_token, event_id)
        preparation.confirm_bracket(admin_token, event_id)
        for _ in range(3):
            current = competition.get_event(event_id)
            fixture = next(game for game in current["games"] if game["status"] == "PENDING" and game["team_a"] and game["team_b"])
            competition.record_result(admin_token, event_id, fixture["id"], fixture["team_a"])
        self.assertEqual({member["id"]: member["score"] for member in core.list_members()}, scores)
        self.assertTrue(all(member["award_units"] == 0 for member in core.list_members()))
        winner = competition.finalize_event(admin_token, event_id)
        competition.finalize_event(admin_token, event_id)
        winners = {player["member_id"] for player in competition.get_event(event_id)["players"] if player["team_id"] == winner}
        self.assertEqual({member["id"] for member in core.list_members() if member["award_units"] == 1}, winners)
        self.assertEqual(len(winners), 5)
        self.assertEqual({member["id"]: member["score"] for member in core.list_members()}, scores)
        normal = next(event for event in competition.list_events() if event["kind"] == "NORMAL" and event["status"] == "READY")
        fixture = competition.get_event(normal["id"])["games"][0]
        competition.record_result(admin_token, normal["id"], fixture["id"], fixture["team_a"])
        for player in competition.get_event(normal["id"])["players"]:
            delta = 10 if player["team_id"] == fixture["team_a"] else -10
            self.assertEqual(core.get_member(player["member_id"])["score"], scores[player["member_id"]] + delta)


if __name__ == "__main__":
    unittest.main()
