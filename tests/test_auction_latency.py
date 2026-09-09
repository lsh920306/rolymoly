"""Avoid redundant remote reads without caching auction state or permissions."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from roly.competition import Competition
from tests import test_live_auction_ui


class AuctionLatencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_live_auction_ui.LiveAuctionUITests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Even an accidentally changed UI service must not load the real key.
        self.enterContext(patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("Unexpected Riot config access")))

    def test_active_page_and_pause_resume_each_use_one_fresh_view(self):
        f = self.fixture
        f.start()
        app = f.app(f.admin)
        original_state = f.live.get_state

        def snapshot_only(event_id, *, conn=None):
            self.assertIsNotNone(conn, "The UI performed a second standalone state read")
            return original_state(event_id, conn=conn)

        with patch.object(Competition, "get_event", side_effect=AssertionError("Unused full event read")), \
                patch.object(f.live, "get_state", side_effect=snapshot_only) as state_read, \
                patch.object(f.live, "get_view", wraps=f.live.get_view) as view_read:
            app.run()
            f.healthy(app)
            self.assertEqual(view_read.call_count, 1)
            self.assertEqual(state_read.call_count, 1)
            for button_key, wanted_status in (("live_pause", "PAUSED"), ("live_resume", "RUNNING")):
                view_read.reset_mock()
                state_read.reset_mock()
                app.button(key=button_key).click().run()
                f.healthy(app)
                self.assertEqual(view_read.call_count, 1)
                self.assertEqual(state_read.call_count, 1)
                with closing(f.core.connect()) as db:
                    self.assertEqual(db.execute("SELECT status FROM live_sessions WHERE event_id=?", (f.event_id,)).fetchone()[0], wanted_status)
            with closing(f.core.connect()) as db:
                actions = [row[0] for row in db.execute("SELECT type FROM live_events WHERE event_id=? AND type IN ('PAUSE','RESUME') ORDER BY id", (f.event_id,))]
            self.assertEqual(actions, ["PAUSE", "RESUME"])

    def test_optional_action_result_skips_reads_and_keeps_authorization_and_deadline(self):
        f = self.fixture
        f.live.configure(f.admin, f.event_id, bid_seconds=15)
        with patch.object(f.live, "get_state", side_effect=AssertionError("Discarded action result was loaded")):
            self.assertIsNone(f.live.start(f.admin, f.event_id, return_state=False))
            f.clock_value += 3
            with self.assertRaises(PermissionError):
                f.live.pause(f.tokens[0], f.event_id, return_state=False)
            self.assertIsNone(f.live.pause(f.admin, f.event_id, return_state=False))
            f.clock_value += 50
            with self.assertRaises(PermissionError):
                f.live.resume(f.tokens[0], f.event_id, return_state=False)
            self.assertIsNone(f.live.resume(f.admin, f.event_id, return_state=False))
        state = f.state()
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(state["current_lot"]["remaining_seconds"], 12)
        self.assertEqual(state["bids"], [])
        self.assertEqual(f.live.pause(f.admin, f.event_id)["status"], "PAUSED")
        self.assertEqual(f.live.resume(f.admin, f.event_id)["status"], "RUNNING")
        f.core.logout(f.admin)
        with self.assertRaises(PermissionError):
            f.live.pause(f.admin, f.event_id, return_state=False)
        self.assertEqual(f.state()["status"], "RUNNING")

    def test_native_overview_dismiss_returns_to_live_controls_and_resume_commits_once(self):
        f = self.fixture
        f.start()
        app = f.app(f.admin)
        app.button(key="live_pause").click().run()
        f.click(app, "전체 팀 보기")
        dialog = app.get("dialog")[0]
        states = app._tree.get_widget_states()
        # Exercise Streamlit's real on_dismiss callback, rather than clearing
        # the session flag ourselves. This is not a browser timing benchmark.
        trigger = next((item for item in states.widgets if item.id == dialog.proto.id), None)
        if trigger is None:
            trigger = states.widgets.add()
            trigger.id = dialog.proto.id
        trigger.trigger_value = True
        with patch.object(Competition, "get_event", side_effect=AssertionError("Dismiss fetched the full event again")), \
                patch.object(f.live, "get_view", wraps=f.live.get_view) as read:
            app._run(states)
            f.healthy(app)
            self.assertEqual(read.call_count, 1)
        self.assertNotIn("live_overview_event", app.session_state)
        self.assertFalse(app.get("dialog"))
        self.assertFalse(app.button(key="live_resume").disabled)
        app.button(key="live_resume").click().run()
        f.healthy(app)
        self.assertEqual(f.state()["status"], "RUNNING")
        with closing(f.core.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_events WHERE event_id=? AND type='RESUME'", (f.event_id,)).fetchone()[0], 1)

    def test_failed_live_read_does_not_fall_back_to_legacy_and_recovers(self):
        f = self.fixture
        f.start()
        app = f.app(f.admin)
        with patch.object(Competition, "get_event", side_effect=AssertionError("Failed view triggered legacy reads")), \
                patch.object(f.live, "get_view", side_effect=sqlite3.OperationalError("synthetic private connection details")):
            app.run()
            f.healthy(app)
        self.assertTrue(any("경매 상태를 불러오지 못했습니다" in item.value for item in app.error))
        self.assertFalse(any("private connection" in item.value for item in app.error))
        self.assertFalse(any("기존 경매입니다" in item.value for item in app.caption))
        app.run()
        f.healthy(app)
        self.assertTrue(app.button(key="live_pause"))

    def test_reset_between_event_listing_and_view_returns_to_preparation(self):
        f = self.fixture
        f.start()
        original_view = f.live.get_view
        reset_done = False

        def reset_before_view(token, event_id):
            nonlocal reset_done
            if not reset_done:
                from uuid import uuid4
                preview = f.live.preview_reset(f.admin, event_id)
                f.live.reset(f.admin, event_id, reason="조회 사이 초기화", request_id=str(uuid4()), expected_fingerprint=preview["fingerprint"])
                reset_done = True
            return original_view(token, event_id)

        with patch.object(f.live, "get_view", side_effect=reset_before_view):
            app = f.app(f.admin)
        self.assertTrue(reset_done)
        self.assertFalse(app.button(key=f"live_start_{f.event_id}").disabled)
        self.assertFalse(any(button.key == "live_pause" for button in app.button))
        self.assertEqual(f.state()["status"], "READY")

    def test_event_selection_and_revoked_login_use_the_selected_fresh_snapshot(self):
        f = self.fixture
        f.start()
        first_id = f.event_id
        second_id = f.prepare_event(f.admin)
        f.live.configure(f.admin, second_id, bid_seconds=30)
        f.live.start(f.admin, second_id)
        app = f.app(f.tokens[0])
        self.assertEqual(app.selectbox(key="auction_event").value, first_id)
        with patch.object(f.live, "get_view", wraps=f.live.get_view) as read:
            app.selectbox(key="auction_event").set_value(second_id).run()
            f.healthy(app)
            self.assertEqual(read.call_args.args[1], second_id)
        selected_lot = f.live.get_state(second_id)["current_lot"]["id"]
        self.assertTrue(app.button(key=f"live_bid_{selected_lot}"))
        f.core.logout(f.tokens[0])
        app.run()
        f.healthy(app)
        self.assertFalse(any((button.key or "").startswith("live_bid_") for button in app.button))
        self.assertFalse(any(button.key in ("live_pause", "live_resume") for button in app.button))
        self.assertEqual(f.live.get_state(second_id)["bids"], [])


if __name__ == "__main__":
    unittest.main()
