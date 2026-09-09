"""A result must use the roster reviewed before submission, on temporary DBs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from roly.competition import Competition, ROLES
from roly.core import Core


class ResultContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "result-context-password")
        cls.admin = cls.base.login("admin", "result-context-password")
        Competition(cls.base)
        cls.ids = []
        for index in range(20):
            mid = cls.base.join_member(f"Context{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            cls.base.approve_member(cls.admin, mid, 100)
            cls.ids.append(mid)
        cls.base.create_account(cls.admin, "host", "host-context-password", role="member", member_id=cls.ids[0])
        cls.host = cls.base.login("host", "host-context-password")

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.core = Core(Path(self.directory.name) / "result.sqlite3")
        with closing(self.core.connect()) as db:
            self.base._keeper.backup(db)
        self.comp = Competition(self.core)

    def assignments(self, ids=None):
        return [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(ids or self.ids[:10])]

    def test_stale_swap_result_and_correction_are_rejected_but_receipt_replays(self):
        event_id = self.comp.create_normal(self.host, self.assignments(), balanced=False)
        original = self.comp.get_event(event_id)
        game = original["games"][0]
        self.comp.swap_players(self.admin, event_id, self.ids[0], self.ids[5], "동시 선수 교환")
        changed = self.comp.get_event(event_id)
        with self.assertRaisesRegex(ValueError, "다른 화면"):
            self.comp.record_result(self.host, event_id, game["id"], game["team_a"],
                expected_roster_token=original["roster_token"])
        self.assertEqual(self.core.list_games(), [])
        self.assertEqual(self.comp.get_event(event_id), changed)
        result = self.comp.record_result(self.host, event_id, game["id"], game["team_a"],
            request_key="first-result", expected_roster_token=changed["roster_token"])
        confirmed = self.comp.get_event(event_id)
        # A lost response can be retried with the stale review token: no write.
        self.assertEqual(self.comp.record_result(self.host, event_id, game["id"], game["team_a"],
            request_key="retry-request", expected_roster_token=original["roster_token"]), result)
        self.assertEqual(self.comp.get_event(event_id), confirmed)
        winners = {player["member_id"] for player in changed["players"] if player["team_id"] == game["team_a"]}
        for mid in self.ids[:10]:
            self.assertEqual(self.core.get_member(mid)["score"], 110 if mid in winners else 90)
        with self.assertRaisesRegex(ValueError, "다른 화면"):
            self.comp.record_result(self.admin, event_id, game["id"], game["team_b"], reason="이전 화면 정정",
                expected_roster_token=changed["roster_token"])
        self.assertEqual(self.core.get_game(result)["revision"], 1)
        self.comp.record_result(self.admin, event_id, game["id"], game["team_b"], reason="최신 기록 확인 후 정정",
            expected_roster_token=confirmed["roster_token"])
        self.assertEqual(self.core.get_game(result)["revision"], 2)

    def test_other_auction_fixture_advancing_invalidates_old_write_not_same_receipt(self):
        event_id = self.comp.create_auction(self.host, self.ids, self.ids[::5], format_name="TOURNAMENT")
        for index, team in enumerate(self.comp.get_event(event_id)["teams"]):
            for mid in self.ids[index * 5 + 1:index * 5 + 5]:
                self.comp.bid(self.host, event_id, mid, team["id"], 0)
        self.comp.finalize_auction(self.host, event_id)
        original = self.comp.get_event(event_id)
        first, second = [game for game in original["games"] if game["team_a"] and game["team_b"]]
        result = self.comp.record_result(self.host, event_id, first["id"], first["team_a"],
            expected_roster_token=original["roster_token"])
        with self.assertRaisesRegex(ValueError, "다른 화면"):
            self.comp.record_result(self.admin, event_id, second["id"], second["team_a"],
                expected_roster_token=original["roster_token"])
        changed = self.comp.get_event(event_id)
        self.comp.record_result(self.admin, event_id, second["id"], second["team_a"],
            expected_roster_token=changed["roster_token"])
        before_retry = self.comp.get_event(event_id)
        self.assertEqual(self.comp.record_result(self.host, event_id, first["id"], first["team_a"],
            expected_roster_token=original["roster_token"]), result)
        self.assertEqual(self.comp.get_event(event_id), before_retry)
        self.assertEqual(len(self.core.list_games()), 2)
        self.assertTrue(all(member["score"] == 100 for member in self.core.list_members()))

    def test_first_result_and_roster_replacement_race_in_both_commit_orders(self):
        for first_operation in ("result", "replacement"):
            with self.subTest(first_operation=first_operation):
                event_id = self.comp.create_normal(self.host, self.assignments(), balanced=False)
                before = self.comp.get_event(event_id)
                game = before["games"][0]
                replacement = self.assignments([self.ids[10], *self.ids[1:10]])
                entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
                local = threading.local()
                original_authorize = self.comp._authorize

                def authorize(*args, **kwargs):
                    actor = original_authorize(*args, **kwargs)
                    if getattr(local, "first", False):
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError("first writer was never released")
                    return actor

                def operation(name, first):
                    local.first = first
                    if not first:
                        second_started.set()
                    try:
                        if name == "result":
                            self.comp.record_result(self.host, event_id, game["id"], game["team_a"],
                                expected_roster_token=before["roster_token"])
                        else:
                            self.comp.replace_normal_roster(self.admin, event_id, replacement, "실제 동시 명단 교체",
                                before["roster_token"], balanced=False)
                        return "SAVED"
                    except ValueError:
                        return "REJECTED"

                other_operation = "replacement" if first_operation == "result" else "result"
                with patch.object(self.comp, "_authorize", side_effect=authorize), ThreadPoolExecutor(max_workers=2) as workers:
                    first = workers.submit(operation, first_operation, True)
                    try:
                        self.assertTrue(entered.wait(10))
                        second = workers.submit(operation, other_operation, False)
                        self.assertTrue(second_started.wait(10))
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=10), "SAVED")
                    self.assertEqual(second.result(timeout=10), "REJECTED")
                after = self.comp.get_event(event_id)
                with self.core.read_snapshot() as db:
                    games = list(db.execute("SELECT id FROM games WHERE tournament_id=?", (str(event_id),)))
                    ledger_count = db.execute("SELECT COUNT(*) FROM score_ledger WHERE game_id IN (SELECT id FROM games WHERE tournament_id=?)", (str(event_id),)).fetchone()[0]
                if first_operation == "result":
                    self.assertEqual((after["status"], len(games), ledger_count), ("PLAYING", 1, 10))
                    self.assertEqual(after["players"], before["players"])
                else:
                    self.assertEqual((after["status"], len(games), ledger_count), ("READY", 0, 0))
                    self.assertEqual({p["member_id"] for p in after["players"]}, {p["member_id"] for p in replacement})
                    self.assertNotEqual(after["games"][0]["id"], game["id"])


if __name__ == "__main__":
    unittest.main()
