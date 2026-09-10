"""Read routing contracts for Push overview; all I/O is replaced locally."""
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from roly import auction_ui


class SessionState(dict):
    __getattr__ = dict.__getitem__


class AuctionOverviewDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.session = SessionState(db_path="isolated-db", token="test-session", live_overview_event=7)
        self.st = SimpleNamespace(session_state=self.session, caption=Mock())
        self.enterContext(patch.object(auction_ui, "st", self.st))

    def test_direct_overview_initializes_once_without_timed_fragment(self):
        direct = {"epoch": "public-generation", "event_id": 7, "ws_url": "/api/auction/ws"}
        with patch("roly.auction_http.transport_config", return_value=direct), \
                patch.object(auction_ui, "refresh_team_overview") as refresh, \
                patch.object(auction_ui, "_poll_team_overview") as poll:
            auction_ui.team_overview.__wrapped__(7)
        refresh.assert_called_once_with(7, direct=direct)
        poll.assert_not_called()

    def test_fallback_overview_retains_legacy_refresh(self):
        with patch("roly.auction_http.transport_config", return_value=None), \
                patch.object(auction_ui, "refresh_team_overview") as refresh, \
                patch.object(auction_ui, "_poll_team_overview") as poll:
            auction_ui.team_overview.__wrapped__(7)
        poll.assert_called_once_with(7)
        refresh.assert_not_called()

    def test_postgres_uses_shared_view_and_retains_tuple_and_error_contract(self):
        live = SimpleNamespace(core=SimpleNamespace(is_postgres=True), get_view=Mock(side_effect=AssertionError("full read")))
        expected = ({"member_id": 2}, {"teams": []})
        with patch("roly.auction_state.shared_view", create=True, return_value=expected) as shared:
            self.assertIs(auction_ui._display_view(live, "test-session", 7), expected)
            shared.assert_called_once_with(live, "test-session", 7)
        with patch("roly.auction_state.shared_view", create=True, side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(sqlite3.OperationalError):
                auction_ui._display_view(live, "test-session", 7)

    def test_local_view_preserves_injected_clock_and_existing_get_view_contract(self):
        expected = (None, None)
        live = SimpleNamespace(core=SimpleNamespace(is_postgres=False), get_view=Mock(return_value=expected))
        with patch("roly.auction_state.shared_view", create=True, side_effect=AssertionError("local cache")):
            self.assertIs(auction_ui._display_view(live, "test-session", 7), expected)
        live.get_view.assert_called_once_with("test-session", 7)

    def test_initial_overview_uses_current_actor_and_only_public_epoch(self):
        actor, state = {"member_id": 2}, {"teams": []}
        direct = {"epoch": "public-generation", "storage_key": "private-storage", "event_id": 7}
        with patch.object(auction_ui, "live_service", return_value=object()), \
                patch.object(auction_ui, "_display_view", return_value=(actor, state)) as read, \
                patch.object(auction_ui, "render_overview") as render:
            auction_ui.refresh_team_overview(7, direct=direct)
        self.assertEqual(read.call_count, 1)
        args, kwargs = render.call_args
        self.assertIs(args[0], state)
        self.assertEqual(kwargs["member_id"], 2)
        self.assertEqual(kwargs["live_epoch"], "public-generation")
        self.assertNotIn("private-storage", repr(kwargs))
        self.assertNotIn("test-session", repr(kwargs))

    def test_closed_dialog_and_revoked_session_do_not_display_stale_teams(self):
        with patch.object(auction_ui, "live_service", return_value=object()), \
                patch.object(auction_ui, "_display_view", return_value=(None, {"teams": [{"name": "old"}]})) as read, \
                patch.object(auction_ui, "render_overview") as render:
            auction_ui.refresh_team_overview(7, direct={"epoch": "epoch"})
            render.assert_not_called()
            self.assertEqual(read.call_count, 1)
            self.assertTrue(self.st.caption.called)
            self.session.pop("live_overview_event")
            auction_ui.refresh_team_overview(7)
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
