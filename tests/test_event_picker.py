"""Native wire-label regressions for programmatic competition selection."""
import os
from pathlib import Path
import tempfile
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]


def replay_label(app, label):
    states = app._tree.get_widget_states()
    box = next(box for box in app.selectbox if box.key in ("selection", "events_selection_AUCTION"))
    for widget in states.widgets:
        if widget.id == box.proto.id:
            widget.string_value = label
    app._run(states)


class EventPickerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())

    def test_auction_page_duplicate_titles_have_distinct_native_wire_values(self):
        from roly.core import Core
        from roly.competition import Competition
        from roly.live_auction import LiveAuction
        from roly.tournament import TournamentService
        from roly.ui import services

        with tempfile.TemporaryDirectory(prefix="roly-auction-picker-") as temp, \
                patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temp}), \
                patch.object(LiveAuction, "ensure_worker"):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            self.assertFalse(app.exception)
            core = Core(app.session_state["db_path"])
            comp = Competition(core)
            service = TournamentService(core, comp)
            token = app.session_state["token"]
            first = service.create(token, "Repeated auction", "2026-10-01T20:00:00+09:00", "First roster", build_mode="AUCTION", team_count=4)
            second = service.create(token, "Repeated auction", "2026-10-02T20:00:00+09:00", "Second roster", build_mode="AUCTION", team_count=6)
            app.session_state["focus_event"] = second
            app.switch_page("app_pages/auction.py").run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            options = app.selectbox(key="auction_event").options
            first_label, second_label = f"Repeated auction · #{first}", f"Repeated auction · #{second}"
            self.assertIn(first_label, options)
            self.assertIn(second_label, options)
            self.assertNotEqual(first_label, second_label)
            for event_id, label, description in ((first, first_label, "First roster"), (second, second_label, "Second roster"), (first, first_label, "First roster")):
                states = app._tree.get_widget_states()
                picker_id = app.selectbox(key="auction_event").proto.id
                for widget in states.widgets:
                    if widget.id == picker_id:
                        widget.string_value = label
                app._run(states)
                self.assertFalse(app.exception, [error.message for error in app.exception])
                self.assertEqual(app.selectbox(key="auction_event").value, event_id)
                self.assertTrue(any(item.value == description for item in app.markdown))
            next(button for button in app.button if button.label == "참가자 선택 시작").click().run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            self.assertEqual(comp.get_event(first)["status"], "RECRUITING")
            self.assertEqual(comp.get_event(second)["status"], "DRAFT")
            self.assertEqual(app.selectbox(key="auction_event").value, first)
            services.clear()

    def test_focus_status_title_changes_and_stale_labels_keep_valid_event_id(self):
        app = AppTest.from_string('''
import streamlit as st
from roly.ui import event_picker
if "events" not in st.session_state:
    st.session_state.events = [{"id":2,"title":"Repeated title","status":"DRAFT"},
                               {"id":1,"title":"Repeated title","status":"READY"}]
chosen = event_picker(st.session_state.events, label="Pick", key="selection")
st.session_state["chosen"] = chosen
st.text_input("Other interaction", key="other")
''').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["chosen"], 2)
        app.session_state["focus_event"] = 1
        app.run()
        old_label = app.selectbox(key="selection").proto.raw_value
        self.assertEqual(app.session_state["chosen"], 1)
        self.assertEqual(len(set(app.selectbox(key="selection").options)), 2, "Duplicate titles need distinct wire labels")
        for status in ("PLAYING", "COMPLETED"):
            app.session_state["events"][1]["status"] = status
            app.run()
            replay_label(app, old_label)
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["chosen"], 1)
            self.assertEqual(app.selectbox(key="selection").proto.raw_value, old_label)
        # Capture the real previous browser payload before changing server data;
        # AppTest otherwise tries to format its old option list using the new title.
        previous_state = app._tree.get_widget_states()
        app.session_state["events"][1]["title"] = "Renamed title"
        app._run(previous_state)
        replay_label(app, old_label)
        self.assertEqual(app.session_state["chosen"], 1)
        self.assertEqual(app.selectbox(key="selection").proto.raw_value, "Renamed title · #1")
        # Pre-upgrade labels have no immutable suffix. Preserve the last valid
        # event rather than silently jumping to the newest one.
        replay_label(app, "Repeated title · 경기 준비")
        self.assertEqual(app.session_state["chosen"], 1)
        replay_label(app, "Unknown event · #999")
        self.assertEqual(app.session_state["chosen"], 1)
        app.selectbox(key="selection").select(2).run()
        self.assertEqual(app.session_state["chosen"], 2, "Explicit user selection must still work")
        app.session_state["focus_event"] = 1
        app.run()
        self.assertEqual(app.session_state["chosen"], 1)
        app.session_state["events"] = [app.session_state["events"][0]]
        replay_label(app, old_label)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["chosen"], 2, "Removed events must fall back to a current option")

    def test_records_result_controls_stay_on_focused_auction_until_completion(self):
        from roly.core import Core, ROLES
        from roly.competition import Competition
        from roly.live_auction import LiveAuction
        from roly.tournament import TournamentService
        from roly.ui import services

        with tempfile.TemporaryDirectory(prefix="roly-picker-ui-") as temp, \
                patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temp}), \
                patch.object(LiveAuction, "ensure_worker"):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            self.assertFalse(app.exception)
            core = Core(app.session_state["db_path"])
            comp = Competition(core)
            token = app.session_state["token"]
            by_role = {role: [m["id"] for m in core.list_members() if m["main_role"] == role] for role in ROLES}
            members = [by_role[role][index] for index in range(4) for role in ROLES]
            target = comp.create_auction(token, members, members[::5], title="Same auction title", format_name="LEAGUE")
            for index, team in enumerate(comp.get_event(target)["teams"]):
                for member_id in members[index * 5 + 1:index * 5 + 5]:
                    comp.bid(token, target, member_id, team["id"], 0)
            comp.finalize_auction(token, target)
            newer = TournamentService(core, comp).create(token, "Same auction title", "2026-10-01T20:00:00+09:00", build_mode="AUCTION", team_count=4)
            app.session_state["focus_event"] = newer
            app.switch_page("app_pages/events.py").run()
            self.assertEqual(app.selectbox(key="events_selection_AUCTION").value, newer)
            app.session_state["focus_event"] = target
            app.run()
            frozen_label = app.selectbox(key="events_selection_AUCTION").proto.raw_value
            for _ in range(6):
                self.assertFalse(app.exception, [error.message for error in app.exception])
                self.assertEqual(app.selectbox(key="events_selection_AUCTION").value, target)
                game = next(g for g in comp.get_event(target)["games"] if g["status"] == "PENDING")
                app.selectbox(key=f"events_pending_{target}").select(game["id"]).run()
                app.selectbox(key=f"events_winner_{game['id']}").select(min(game["team_a"], game["team_b"])).run()
                next(b for b in app.button if (b.key or "").startswith("FormSubmitter:events_result_form_")).click()
                replay_label(app, frozen_label)
                self.assertEqual(app.selectbox(key="events_selection_AUCTION").value, target)
                self.assertEqual(comp.get_event(newer)["status"], "DRAFT")
            app.button(key=f"events_finish_{target}").click()
            replay_label(app, frozen_label)
            self.assertFalse(app.exception, [error.message for error in app.exception])
            self.assertEqual(comp.get_event(target)["status"], "COMPLETED")
            self.assertEqual(app.selectbox(key="events_selection_AUCTION").value, target)
            self.assertEqual(comp.get_event(newer)["games"], [])
            core_game_id = comp.get_event(target)["games"][0]["core_game_id"]
            app.session_state["events_detail_tab_AUCTION"] = "전체 경기 이력"
            app.run()
            app.selectbox(key="events_history_detail_AUCTION").select(core_game_id).run()
            self.assertEqual(app.selectbox(key="events_selection_AUCTION").value, target)
            self.assertTrue(any("경기 직전 점수" in table.value.columns for table in app.dataframe))
            services.clear()


if __name__ == "__main__":
    unittest.main()
