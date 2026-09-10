"""Reviewed normal-roster swaps, isolated concurrent requests and lost replies."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition, ROLES


class NormalSwapConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())
        directory = TemporaryDirectory(prefix="roly-normal-swap-")
        self.addCleanup(directory.cleanup)
        self.core = Core(Path(directory.name) / "isolated.sqlite3")
        self.core.setup_admin("admin", "synthetic-swap-password")
        self.admin = self.core.login("admin", "synthetic-swap-password")
        self.comp = Competition(self.core)
        self.ids = []
        for index in range(10):
            member = self.core.join_member(f"Swap{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, member, 200)
            self.ids.append(member)
        for name, member in (("host", self.ids[0]), ("viewer", self.ids[1])):
            self.core.create_account(self.admin, name, "synthetic-swap-password", role="member", member_id=member)
        self.host = self.core.login("host", "synthetic-swap-password")
        self.viewer = self.core.login("viewer", "synthetic-swap-password")
        self.event_id = self.comp.create_normal(self.host,
            [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(self.ids)],
            balanced=False, format_name="SINGLE")
        self.before = self.comp.get_event(self.event_id)
        self.first, self.second = self.ids[0], self.ids[5]
        self.body = {"first_member_id": self.first, "second_member_id": self.second,
                     "reason": "reviewed balance", "expected_roster_token": self.before["roster_token"],
                     "request_id": str(uuid4())}

    def snapshot(self):
        return self.comp.get_event(self.event_id)

    def assert_swapped_once(self):
        after = self.snapshot()
        prior = {p["member_id"]: p["team_id"] for p in self.before["players"]}
        actual = {p["member_id"]: p["team_id"] for p in after["players"]}
        expected = dict(prior)
        expected[self.first], expected[self.second] = prior[self.second], prior[self.first]
        self.assertEqual(actual, expected)
        self.assertEqual(sum(row["action"] == "SWAP" for row in after["audit"]), 1)
        self.assertEqual(self.core.list_games(), [])
        self.assertTrue(all(member["score"] == 200 for member in self.core.list_members()))
        return after

    def test_two_operators_with_same_roster_only_swap_once(self):
        gate = Barrier(2)

        def submit(token):
            gate.wait(timeout=10)
            try:
                self.comp.swap_players(token, self.event_id, **dict(self.body, request_id=str(uuid4())))
                return "saved"
            except ValueError as error:
                self.assertIn("최신 명단", str(error))
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(submit, (self.host, self.admin)))
        self.assertCountEqual(replies, ["saved", "stale"])
        self.assert_swapped_once()

    def test_simultaneous_identical_requests_return_one_receipt(self):
        gate = Barrier(2)

        def submit(_):
            gate.wait(timeout=10)
            return self.comp.swap_players(self.host, self.event_id, **self.body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(submit, range(2)))
        self.assertCountEqual([reply["replayed"] for reply in replies], [False, True])
        self.assert_swapped_once()

    def test_committed_retry_survives_new_service_and_later_game_progress(self):
        receipt = self.comp.swap_players(self.host, self.event_id, **self.body)
        event = self.assert_swapped_once()
        game = event["games"][0]
        self.comp.record_result(self.host, self.event_id, game["id"], game["team_a"])
        progressed = self.snapshot()
        replacement = Competition(Core(self.core.db_path))
        replay = replacement.swap_players(self.host, self.event_id, **self.body)
        self.assertEqual(replay, dict(receipt, replayed=True))
        self.assertEqual(self.snapshot(), progressed)

    def test_request_reuse_rejects_changed_payload_and_other_operator(self):
        self.comp.swap_players(self.host, self.event_id, **self.body)
        after = self.assert_swapped_once()
        for changes in ({"reason": "changed"}, {"first_member_id": self.ids[1]},
                        {"expected_roster_token": after["roster_token"]}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "같은 요청 번호"):
                self.comp.swap_players(self.host, self.event_id, **dict(self.body, **changes))
        with self.assertRaisesRegex(ValueError, "같은 요청 번호"):
            self.comp.swap_players(self.admin, self.event_id, **self.body)
        with self.assertRaises(PermissionError):
            self.comp.swap_players(self.viewer, self.event_id, **self.body)
        self.assertEqual(self.snapshot(), after)

    def test_restoring_same_lineup_does_not_make_old_review_valid(self):
        for _ in range(2):
            self.comp.swap_players(self.admin, self.event_id, self.first, self.second)
        after = self.snapshot()
        self.assertNotEqual(after["roster_token"], self.before["roster_token"])
        with self.assertRaisesRegex(ValueError, "최신 명단"):
            self.comp.swap_players(self.host, self.event_id, **self.body)
        self.assertEqual(self.snapshot(), after)

    def test_guarded_calls_require_both_review_and_nonzero_uuid(self):
        for changes in ({"request_id": None}, {"request_id": "invalid"}, {"request_id": str(uuid4()).replace("-", "x")},
                        {"request_id": "00000000-0000-0000-0000-000000000000"},
                        {"expected_roster_token": None}, {"expected_roster_token": ""}, {"reason": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.comp.swap_players(self.host, self.event_id, **dict(self.body, **changes))
        self.assertEqual(self.snapshot(), self.before)

    def page(self, token):
        app = AppTest.from_string('import runpy\nrunpy.run_path("app_pages/events.py", run_name="__main__")', default_timeout=30)
        app.session_state["db_path"] = self.core.db_path
        app.session_state["token"] = token
        app.session_state["focus_event"] = self.event_id
        app.session_state["events_detail_tab_NORMAL"] = "일반내전 명단"
        app.run()
        self.assertFalse(app.exception, [item.message for item in app.exception])
        return app

    def test_two_open_pages_reject_stale_button_then_offer_explicit_reload(self):
        host, admin = self.page(self.host), self.page(self.admin)
        host.button(key=f"events_swap_{self.event_id}").click().run()
        self.assertFalse(host.exception)
        after = self.assert_swapped_once()
        admin.button(key=f"events_swap_{self.event_id}").click().run()
        self.assertFalse(admin.exception)
        self.assertTrue(admin.button(key=f"events_swap_{self.event_id}").disabled)
        self.assertTrue(any("최신 교환 명단" in item.value for item in admin.warning))
        self.assertEqual(self.snapshot(), after)
        admin.button(key=f"events_swap_reload_{self.event_id}").click().run()
        self.assertFalse(admin.exception)
        self.assertFalse(admin.button(key=f"events_swap_{self.event_id}").disabled)
        self.assertEqual(self.snapshot(), after)

    def test_ui_recovers_lost_success_reply_without_repeating_swap(self):
        app = self.page(self.host)
        original = Competition.swap_players

        def lost_reply(competition, *args, **kwargs):
            original(competition, *args, **kwargs)
            raise sqlite3.OperationalError("simulated committed response loss")

        with patch.object(Competition, "swap_players", autospec=True, side_effect=lost_reply):
            app.button(key=f"events_swap_{self.event_id}").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        after = self.assert_swapped_once()
        app.run()
        self.assertTrue(app.button(key=f"events_swap_{self.event_id}").disabled)
        app.button(key=f"events_swap_retry_{self.event_id}").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertEqual(self.snapshot(), after)
        self.assertFalse(any((button.key or "").startswith("events_swap_retry_") for button in app.button))


if __name__ == "__main__":
    unittest.main()
