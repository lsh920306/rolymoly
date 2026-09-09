"""Refresh controls stay responsive and never expose or use real test keys."""
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition
from roly.riot_api import RiotConfig
from roly.riot_ui import riot_service
from roly.ui import services


CONTROLS = '''
import streamlit as st
from roly.core import Core
from roly.riot_ui import refresh_control
core=Core(st.session_state.path)
refresh_control(core, st.session_state.get("token"), list(range(1,46)), key="qa")
'''
EDITOR = '''
import streamlit as st
from roly.member_editor import member_edit_form
from roly.core import Core
core=Core(st.session_state.path)
member_edit_form(core,st.session_state.token,core.session(st.session_state.token),st.session_state.mid,prefix="qa")
'''
PICKER = '''
import streamlit as st
from roly.core import Core
from roly.riot_ui import member_refresh_picker
core=Core(st.session_state.path)
member_refresh_picker(core, st.session_state.get("token"), core.list_members(), key="qa_picker")
'''
RECORD = '''
import streamlit as st
from roly.core import Core
from roly.member_records import show_member_record
core=Core(st.session_state.path)
show_member_record(core, st.session_state.mid)
'''


class FakeSync:
    config = RiotConfig()

    def __init__(self):
        self.queued, self.reads = [], []
        self.profiles = {}

    def enqueue(self, token, ids, force=False):
        self.queued.append((ids, force))
        return len(ids)

    def get_profiles(self, ids):
        self.reads.append(ids)
        return {mid: self.profiles.get(mid, {"current_tier": "골드 2", "fetched_at": None, "status": "QUEUED"}) for mid in ids}


class RiotUITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="roly-riot-ui-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.core = Core(self.path)
        Competition(self.core)
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.token = self.core.login("admin", "synthetic-admin-password")
        self.addCleanup(services.clear)

    def app(self, script, token):
        app = AppTest.from_string(script, default_timeout=15)
        app.session_state.path = str(self.path)
        app.session_state.token = token
        return app

    def member(self, name):
        mid = self.core.join_member(name + "#QA", "TOP", "JG")
        self.core.approve_member(self.token, mid, 100)
        return mid

    def test_only_button_refreshes_large_list_and_reruns_never_enqueue(self):
        sync = FakeSync()
        with patch("roly.riot_ui.riot_service", return_value=sync), patch("roly.riot_sync.start_worker"):
            app = self.app(CONTROLS, self.token).run()
            self.assertFalse(app.exception)
            self.assertFalse(sync.queued)
            self.assertTrue(any("Riot 조회 0/45명" in item.value for item in app.caption))
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(sync.queued)
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual([len(ids) for ids, force in sync.queued], [40, 5])
            self.assertEqual([force for ids, force in sync.queued], [True, True])
            self.assertTrue(all(len(ids) <= 40 for ids in sync.reads))

    def test_anonymous_observer_cannot_enqueue(self):
        sync = FakeSync()
        with patch("roly.riot_ui.riot_service", return_value=sync):
            app = self.app(CONTROLS, None).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button[0].disabled)
        self.assertFalse(sync.queued)

    def test_api_member_editor_disables_only_api_tier_fields(self):
        from roly.member_ranks import save_riot_profile
        mid = self.core.join_member("RankedMember#QA", "TOP", "JG")
        self.core.approve_member(self.token, mid, 100)
        with self.core.transaction() as db:
            save_riot_profile(db, mid, self.core.get_member(mid, db)["canonical_id"],
                              {"current_tier": "골드 2", "lp": 31}, 100.0)
        app = self.app(EDITOR, self.token)
        app.session_state.mid = mid
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(next(item for item in app.selectbox if item.label == "현재 티어").disabled)
        self.assertTrue(next(item for item in app.number_input if item.label == "현재 LP (선택)").disabled)
        self.assertFalse(next(item for item in app.text_input if item.label == "클랜 티어").disabled)

    def test_disposable_sqlite_does_not_load_or_send_operating_api_key(self):
        with patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("real key loaded")):
            self.assertIsNone(riot_service(self.core))

    def test_preparation_picker_selects_one_member_and_never_refreshes_on_selection(self):
        first, second = self.member("MemberFirst"), self.member("MemberSecond")
        sync = FakeSync()
        with patch("roly.riot_ui.riot_service", return_value=sync), patch("roly.riot_sync.start_worker"):
            app = self.app(PICKER, self.token).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button)
            self.assertFalse(sync.queued)
            app.selectbox(key="riot_member_picker_qa_picker").set_value(second).run()
            self.assertFalse(app.exception)
            self.assertFalse(sync.queued)
            app.button(key=f"riot_refresh_qa_picker_{second}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(sync.queued, [((second,), True)])
            self.assertTrue(all(list(ids) == [second] for ids in sync.reads))
            app.run()
            self.assertEqual(len(sync.queued), 1)
            app.selectbox(key="riot_member_picker_qa_picker").set_value(first).run()
            self.assertEqual(len(sync.queued), 1)

    def test_auction_picker_uses_member_id_not_participation_row_id(self):
        first, second = self.member("AuctionFirst"), self.member("AuctionSecond")
        script = '''
import streamlit as st
from roly.core import Core
from roly.riot_ui import member_refresh_picker
core=Core(st.session_state.path)
member_refresh_picker(core, st.session_state.token, st.session_state.players, key="auction_qa")
'''
        sync = FakeSync()
        with patch("roly.riot_ui.riot_service", return_value=sync), patch("roly.riot_sync.start_worker"):
            app = self.app(script, self.token)
            app.session_state.players = [{"id": 9001, "member_id": first, "riot_id": "AuctionFirst#QA"},
                                         {"id": 9002, "member_id": second, "riot_id": "AuctionSecond#QA"}]
            app.run()
            app.selectbox(key="riot_member_picker_auction_qa").set_value(first).run()
            app.button(key=f"riot_refresh_auction_qa_{first}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(sync.queued, [((first,), True)])

    def test_member_record_dialog_has_one_refresh_and_updates_profile_without_app_rerun(self):
        mid = self.member("ProfilePopup")
        sync = FakeSync()
        with patch("roly.riot_ui.riot_service", return_value=sync), patch("roly.riot_sync.start_worker"):
            app = self.app(RECORD, self.token)
            app.session_state.mid = mid
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(sync.queued)
            self.assertEqual(sum(widget.label == "Riot 정보 갱신" for widget in app.button), 1)
            app.button(key=f"riot_refresh_record_{mid}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(sync.queued, [((mid,), True)])
            sync.profiles[mid] = {"current_tier": "다이아몬드 4", "lp": 0,
                "fetched_at": 2000000000, "updated_at": "2033-05-18T03:33:20+00:00", "status": "DONE",
                "champions": [{"id": 22, "name": "애쉬", "points": 1000, "level": 5, "icon_url": ""}],
                "profile_icon_url": ""}
            with patch("roly.riot_ui.st.rerun", side_effect=AssertionError("popup closed by app rerun")):
                app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any("다이아몬드 4" in item.value for item in app.markdown))
            self.assertTrue(any(item.value == "애쉬" for item in app.text))
            self.assertTrue(any("최근 조회 완료" in item.value for item in app.caption))
            self.assertEqual(len(sync.queued), 1)

    def test_revoked_session_in_open_member_record_disables_refresh(self):
        mid = self.member("ExpiredPopup")
        sync = FakeSync()
        with patch("roly.riot_ui.riot_service", return_value=sync), patch("roly.riot_sync.start_worker"):
            app = self.app(RECORD, self.token)
            app.session_state.mid = mid
            app.run()
            self.core.logout(self.token)
            app.button(key=f"riot_refresh_record_{mid}").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key=f"riot_refresh_record_{mid}").disabled)
            self.assertFalse(sync.queued)

    def test_each_member_row_opens_only_that_profile_then_refreshes_one_member(self):
        self.member("RowFirst")
        self.member("RowSecond")
        sync = FakeSync()
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {"ROLYMOLY_DATABASE_TARGET": str(self.path),
                                     "ROLYMOLY_DATA_DIR": str(self.path.parent)}), \
                patch("roly.riot_ui.riot_service", return_value=sync), patch("roly.riot_sync.start_worker"):
            app = AppTest.from_file(str(root / "app.py"), default_timeout=25)
            app.session_state.space = "운영 공간"
            app.session_state.db_path = str(self.path)
            app.session_state.token = self.token
            app.run()
            app.switch_page("app_pages/members.py").run()
            self.assertFalse(app.exception)
            self.assertFalse(any(widget.label in ("Riot 정보 갱신", "이 회원 갱신") for widget in app.button))
            self.assertFalse(sync.queued)
            members = self.core.list_members()
            self.assertEqual(sum(widget.label == "프로필 상세보기" for widget in app.button), len(members))
            self.assertFalse(app.dataframe)
            selected_id = members[1]["id"]
            app.button(key=f"member_profile_{selected_id}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["profile_origin"], "members")
            query = app.query_params.get("member")
            self.assertEqual(query[0] if isinstance(query, list) else query, str(selected_id))
            self.assertFalse(sync.queued)
            self.assertEqual(sum(widget.label == "갱신하기" for widget in app.button), 1)
            # The server redirect above is verified. AppTest needs its next
            # client request aligned with the newly selected hidden page.
            app.switch_page("app_pages/profile.py").run()
            app.button(key=f"riot_refresh_profile_{selected_id}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(sync.queued, [((selected_id,), True)])
