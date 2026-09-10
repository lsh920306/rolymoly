"""Hidden UI work and deferred exports use only disposable local data."""
from pathlib import Path
import tempfile
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition
from roly.ui import deferred_csv, services


ROOT = Path(__file__).resolve().parents[1]


class LazyPageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())
        temporary = tempfile.TemporaryDirectory(prefix="roly-lazy-pages-")
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "local.sqlite3")
        self.core = Core(self.path)
        Competition(self.core)
        self.core.setup_admin("admin", "synthetic-lazy-password")
        self.token = self.core.login("admin", "synthetic-lazy-password")
        self.mid = self.core.join_member("LazyMember#QA", "TOP", "JG")
        self.core.approve_member(self.token, self.mid, 100)
        self.addCleanup(services.clear)

    def app(self, page):
        app = AppTest.from_file(str(ROOT / "app_pages" / f"{page}.py"), default_timeout=15)
        app.session_state.db_path = self.path
        app.session_state.token = self.token
        return app

    def clean(self, app):
        self.assertFalse(app.exception, [e.message for e in app.exception])

    def test_admin_join_does_not_read_other_tabs_or_build_csv(self):
        app = self.app("admin")
        with patch.object(Core, "list_accounts", side_effect=AssertionError("hidden accounts")), \
             patch.object(Core, "audit_log", side_effect=AssertionError("hidden audit")), \
             patch.object(Core, "list_adjustments", side_effect=AssertionError("hidden adjustments")), \
             patch.object(Core, "policy_history", side_effect=AssertionError("hidden policy history")), \
             patch.object(Core, "csv_bytes", side_effect=AssertionError("eager CSV")):
            app.run()
            self.clean(app)
        self.assertEqual(app.session_state.admin_active_tab, "가입 승인")
        self.assertFalse(any(item.key == "admin_account_id" for item in app.selectbox))

    def test_admin_closed_histories_and_kick_preview_do_not_query(self):
        app = self.app("admin")
        app.session_state.admin_active_tab = "회원·점수"
        app.session_state.admin_member_id = self.mid
        with patch.object(Core, "list_adjustments", side_effect=AssertionError("closed history")), \
             patch.object(Core, "member_active_events", side_effect=AssertionError("closed kick preview")):
            app.run()
            self.clean(app)
        app.session_state.admin_adjustment_history = True
        with patch.object(Core, "list_adjustments", return_value=[]) as query:
            app.run()
            self.clean(app)
            query.assert_called_once()
        app.session_state.admin_adjustment_history = False
        app.session_state[f"admin_membership_panel_{self.mid}"] = True
        with patch.object(Core, "member_active_events", return_value=[]) as query:
            app.run()
            self.clean(app)
            query.assert_called_once()

    def test_admin_policy_history_reads_only_when_open(self):
        app = self.app("admin")
        app.session_state.admin_active_tab = "점수 정책"
        with patch.object(Core, "policy_history", side_effect=AssertionError("closed policy history")):
            app.run()
            self.clean(app)
        app.session_state.admin_policy_history = True
        with patch.object(Core, "policy_history", return_value=[]) as query:
            app.run()
            self.clean(app)
            query.assert_called_once()

    def test_empty_event_result_tab_does_not_read_global_history(self):
        app = self.app("events")
        with patch.object(Core, "list_games", side_effect=AssertionError("hidden games")), \
             patch.object(Competition, "game_labels", side_effect=AssertionError("hidden labels")):
            app.run()
            self.clean(app)
        app.session_state.events_detail_tab_NORMAL = "전체 경기 이력"
        with patch.object(Core, "list_games", return_value=[]) as query:
            app.run()
            self.clean(app)
            query.assert_called_once()

    def test_member_csv_table_is_not_built_during_page_render(self):
        app = self.app("members")
        with patch("roly.ui.member_table", side_effect=AssertionError("eager export table")), \
             patch.object(Core, "csv_bytes", side_effect=AssertionError("eager CSV")):
            app.run()
            self.clean(app)
        self.assertTrue(app.get("download_button"))

    def test_deferred_export_runs_factory_once_per_request_and_checks_revocation(self):
        rows = [{"회원": "synthetic", "점수": 100}]
        factory = Mock(return_value=rows)
        with patch.object(Core, "csv_bytes", return_value=b"synthetic-export") as encode:
            public = deferred_csv(self.core, factory)
            private = deferred_csv(self.core, factory, token=self.token, admin=True)
            factory.assert_not_called()
            encode.assert_not_called()
            self.assertEqual(public(), b"synthetic-export")
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(private(), b"synthetic-export")
            self.assertEqual(factory.call_count, 2)
            self.core.logout(self.token)
            with self.assertRaises(PermissionError):
                private()
            self.assertEqual(factory.call_count, 2)


class RiotIdleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())
        self.core = Mock(db_path="disposable", is_postgres=False)
        self.core.session.return_value = {"role": "member", "member_status": "APPROVED"}
        self.sync = Mock()
        self.profile = {"current_tier": "골드 2", "fetched_at": 100, "updated_at": "2026-09-10T00:00:00Z", "status": "DONE"}
        self.sync.get_profiles.side_effect = lambda ids: {mid: dict(self.profile) for mid in ids}
        self.sync.enqueue.return_value = 1
        self.app = AppTest.from_string('''
import streamlit as st
from roly.riot_ui import refresh_control
refresh_control(st.session_state.core, st.session_state.token, st.session_state.ids, key="idle", rerun_on_update=False)
''', default_timeout=10)
        self.app.session_state.core = self.core
        self.app.session_state.token = "synthetic-token-only"
        self.app.session_state.ids = [1]

    def test_idle_checks_wait_thirty_seconds_and_external_pending_resumes_fast_checks(self):
        with patch("roly.riot_ui.riot_service", return_value=self.sync), patch("roly.riot_ui.monotonic", return_value=0) as clock:
            self.app.run()
            self.assertFalse(self.app.exception)
            self.assertEqual(self.sync.get_profiles.call_count, 1)
            for now in (3, 6, 29):
                clock.return_value = now
                self.app.run()
            self.assertEqual(self.sync.get_profiles.call_count, 1)
            self.assertEqual(self.core.session.call_count, 1)
            self.profile["status"] = "QUEUED"
            clock.return_value = 30
            self.app.run()
            self.assertEqual(self.sync.get_profiles.call_count, 2)
            clock.return_value = 33
            self.profile.update(status="DONE", updated_at="2026-09-10T00:01:00Z")
            self.app.run()
            self.assertEqual(self.sync.get_profiles.call_count, 3)
            self.assertTrue(any("최근 조회 완료" in item.value for item in self.app.caption))
            clock.return_value = 36
            self.app.run()
            self.assertEqual(self.sync.get_profiles.call_count, 3)

    def test_idle_click_rechecks_revoked_session_before_enqueue(self):
        with patch("roly.riot_ui.riot_service", return_value=self.sync), patch("roly.riot_ui.monotonic", return_value=0), \
             patch("roly.riot_sync.start_worker"):
            self.app.run()
            self.core.session.return_value = None
            self.app.button(key="riot_refresh_idle").click().run()
            self.assertFalse(self.app.exception)
            self.assertTrue(self.app.button(key="riot_refresh_idle").disabled)
            self.sync.enqueue.assert_not_called()

    def test_changed_token_or_member_does_not_reuse_idle_snapshot(self):
        with patch("roly.riot_ui.riot_service", return_value=self.sync), patch("roly.riot_ui.monotonic", return_value=0):
            self.app.run()
            self.app.session_state.ids = [2]
            self.app.run()
            self.app.session_state.token = "different-synthetic-token"
            self.app.run()
            self.assertFalse(self.app.exception)
            self.assertEqual(self.sync.get_profiles.call_count, 3)
            self.assertEqual(len(self.app.session_state["_riot_poll_cache"]), 1)


if __name__ == "__main__":
    unittest.main()
