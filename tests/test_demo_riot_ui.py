"""Versioned demo isolation and opt-in Riot configuration, without real HTTP."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from roly import riot_ui, ui
from roly.competition import Competition, ROLES
from roly.core import Core
from roly.demo import DEMO_DATA_VERSION, DEMO_RIOT_IDS
from roly.live_auction import LiveAuction
from roly.riot_api import RiotConfig
from roly.storage_config import ConfigError, SUPABASE_TARGET


ROOT = Path(__file__).resolve().parents[1]
FAKE_CONFIG = RiotConfig(api_key="synthetic-riot-test-key", allow_demo=True)


def database_fingerprint(core):
    with closing(core.connect()) as db:
        return sha256("\n".join(db.iterdump()).encode()).hexdigest()


class DemoVersionTransitionTests(unittest.TestCase):
    def test_old_session_moves_to_new_demo_without_mutating_old_or_operating_data(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-demo-version-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(ui.services.clear)
        self.addCleanup(ui.lounge_service.clear)
        data_root = Path(temporary.name)
        demo_id = "previous-session"
        old_path = data_root / f"demo-{demo_id}.sqlite3"
        old_core = Core(old_path)
        password = "synthetic-old-demo-password"
        old_core.setup_admin("demo", password, "이전 체험 운영진")
        old_token = old_core.login("demo", password)
        old_ids = []
        for index in range(40):
            member_id = old_core.join_member(f"Previous{index}#DEMO", ROLES[index % 5], ROLES[(index + 1) % 5])
            old_core.approve_member(old_token, member_id, 100)
            old_ids.append(member_id)
        old_core.join_member("PreviousPending#DEMO", "TOP", "JG")
        old_credentials = [{"username": "demo", "password": password, "member_id": None, "label": "이전 관리자"}]
        for index, member_id in enumerate([*old_ids[:20:5], old_ids[1]]):
            username = f"previous_{index}"
            old_core.create_account(old_token, username, password, role="member", member_id=member_id)
            old_credentials.append({"username": username, "password": password,
                                    "member_id": member_id, "label": username})
        operating_path = data_root / "operating.sqlite3"
        operating = Core(operating_path)
        operating.setup_admin("operating", "synthetic-operating-password")
        operating.join_member("OperatingOnly#QA", "MID", "SUP")
        old_before, operating_before = database_fingerprint(old_core), database_fingerprint(operating)
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        for key, value in {"space": "체험 공간", "demo_id": demo_id, "db_path": str(old_path),
                           "token": old_token, "demo_token": old_token,
                           "demo_accounts": old_credentials,
                           "live_pending_1": {"previous": True}}.items():
            app.session_state[key] = value
        with patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": str(data_root)}), patch.object(
            ui, "operating_database", return_value=str(operating_path)
        ), patch("roly.storage_config._runtime_document", side_effect=AssertionError("real settings read")), patch.object(
            riot_ui, "load_riot_config", side_effect=AssertionError("real Riot config read")
        ), patch.object(LiveAuction, "ensure_worker"):
            app.run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            new_path = str(data_root / f"demo-v{DEMO_DATA_VERSION}-{demo_id}.sqlite3")
            self.assertEqual(app.session_state["db_path"], new_path)
            self.assertEqual(app.session_state["demo_id"], demo_id)
            self.assertNotEqual(app.session_state["token"], old_token)
            self.assertEqual(app.session_state["token"], app.session_state["demo_token"])
            self.assertNotIn("live_pending_1", app.session_state)
            new_core = Core(new_path)
            self.assertIsNone(new_core.session(old_token))
            self.assertEqual(new_core.session(app.session_state["token"])["role"], "admin")
            self.assertEqual({member["riot_id"] for member in new_core.list_members(True)}, set(DEMO_RIOT_IDS))
            self.assertEqual(len(new_core.list_members()), 22)
            accounts = app.session_state["demo_accounts"]
            self.assertEqual(len(accounts), 6)
            self.assertTrue(all(account["password"] != password for account in accounts))
            auction = next(event for event in Competition(new_core).list_events() if event["kind"] == "AUCTION")
            app.button(key=f"home_event_{auction['id']}").click().run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            self.assertEqual(app.selectbox(key="auction_event").value, auction["id"])
            detail = Competition(new_core).get_event(auction["id"])
            self.assertEqual((len(detail["players"]), len(detail["teams"])), (20, 4))
            self.assertEqual({team["captain_id"] for team in detail["teams"]},
                             {account["member_id"] for account in accounts[1:5]})
            token = app.session_state["token"]
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["token"], token)
            self.assertEqual(app.session_state["db_path"], new_path)
        self.assertEqual(database_fingerprint(old_core), old_before)
        self.assertEqual(database_fingerprint(operating), operating_before)
        self.assertEqual(len(old_core.list_members(True)), 41)
        self.assertIsNotNone(old_core.session(old_token))


class DemoRiotServiceTests(unittest.TestCase):
    def setUp(self):
        self.core = SimpleNamespace(db_path="test-demo.sqlite3", is_postgres=False)
        stop = patch("roly.riot_sync.stop_demo_workers")
        self.stop_demo = stop.start()
        self.addCleanup(stop.stop)

    def authorized_state(self, path=None):
        return {"space": "체험 공간", "demo_riot_database": path or self.core.db_path,
                "demo_riot_rate_target": SUPABASE_TARGET}

    def test_unapproved_sqlite_contexts_never_read_config_or_create_services(self):
        states = ({}, {"space": "체험 공간"},
                  {**self.authorized_state(), "space": "운영 공간"},
                  {**self.authorized_state(), "demo_riot_database": "other-demo.sqlite3"},
                  {**self.authorized_state(), "demo_riot_rate_target": "operating.sqlite3"},
                  {**self.authorized_state(), "demo_riot_rate_target": "supabase://other"},
                  {**self.authorized_state(), "demo_riot_rate_target": None})
        for state in states:
            with self.subTest(state=state), patch.object(riot_ui.st, "session_state", state), patch.object(
                riot_ui, "load_riot_config", side_effect=AssertionError("real key read")
            ) as config, patch.object(riot_ui, "_service") as service:
                self.assertIsNone(riot_ui.riot_service(self.core))
                config.assert_not_called()
                service.assert_not_called()

    def test_approved_demos_and_operation_share_rate_target_but_keep_separate_caches(self):
        cores = (self.core, SimpleNamespace(db_path="another-demo.sqlite3", is_postgres=False),
                 SimpleNamespace(db_path=SUPABASE_TARGET, is_postgres=True))
        sentinel = object()
        with patch.object(riot_ui, "load_riot_config", return_value=FAKE_CONFIG) as config, patch.object(
            riot_ui, "_service", return_value=sentinel
        ) as service, patch("roly.riot_sync.stop_workers_except_key") as stop:
            for core in cores:
                with patch.object(riot_ui.st, "session_state", self.authorized_state(core.db_path)):
                    self.assertIs(riot_ui.riot_service(core), sentinel)
            self.assertEqual(config.call_count, 3)
            self.assertEqual(stop.call_count, 3)
            expected_hash = sha256(FAKE_CONFIG.api_key.encode()).hexdigest()
            self.assertEqual([call.args for call in service.call_args_list],
                             [(core.db_path, expected_hash, SUPABASE_TARGET, core, FAKE_CONFIG) for core in cores])

    def test_missing_key_stops_existing_workers_without_constructing_service(self):
        disabled = RiotConfig()
        with patch.object(riot_ui.st, "session_state", self.authorized_state()), patch.object(
            riot_ui, "load_riot_config", return_value=disabled
        ), patch("roly.riot_sync.stop_workers_except_key") as stop, patch.object(riot_ui, "_service") as service:
            self.assertIsNone(riot_ui.riot_service(self.core))
            stop.assert_called_once_with(disabled)
            service.assert_not_called()

    def test_invalid_config_stops_existing_workers_and_returns_safe_error(self):
        with patch.object(riot_ui.st, "session_state", self.authorized_state()), patch.object(
            riot_ui, "load_riot_config", side_effect=ConfigError("설정 형식 확인")
        ), patch("roly.riot_sync.stop_workers_except_key") as stop, patch.object(riot_ui, "_service") as service:
            with self.assertRaisesRegex(ConfigError, "설정 형식 확인"):
                riot_ui.riot_service(self.core)
            self.assertFalse(stop.call_args.args[0].enabled)
            service.assert_not_called()

    def test_service_uses_shared_rate_core_and_never_initializes_real_postgres(self):
        riot_ui._service.clear()
        self.addCleanup(riot_ui._service.clear)
        rate_core, sync = object(), Mock()
        with patch.object(riot_ui, "_rate_core", return_value=rate_core) as rate, patch(
            "roly.riot_sync.RiotSync", return_value=sync
        ) as factory:
            result = riot_ui._service(self.core.db_path, "synthetic-key-hash", SUPABASE_TARGET, self.core, FAKE_CONFIG)
            self.assertIs(result, sync)
            rate.assert_called_once_with(SUPABASE_TARGET)
            factory.assert_called_once_with(self.core, config=FAKE_CONFIG, rate_core=rate_core)
            sync.ensure_worker.assert_called_once_with()

    def test_deployed_demo_hides_refresh_when_api_is_disabled_without_loading_postgres(self):
        script = '''
import streamlit as st
from types import SimpleNamespace
from roly.riot_ui import refresh_control
core = SimpleNamespace(db_path="test-demo.sqlite3", is_postgres=False)
refresh_control(core, None, [1], key="deployed_demo")
'''
        app = AppTest.from_string(script, default_timeout=15)
        for key, value in self.authorized_state().items():
            app.session_state[key] = value
        with patch.object(riot_ui, "load_riot_config", return_value=RiotConfig(api_key="synthetic-riot-test-key")) as config, patch.object(
            riot_ui, "_service"
        ) as service, patch.object(riot_ui, "_rate_core") as rate, patch("roly.riot_sync.stop_workers_except_key"):
            app.run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            config.assert_called_once_with()
            self.stop_demo.assert_called_once_with()
            service.assert_not_called()
            rate.assert_not_called()
            self.assertFalse(app.button)
            self.assertFalse(app.caption)


class RiotServiceOutageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-riot-outage-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "profile.sqlite3"
        self.core = Core(self.path)
        Competition(self.core)
        self.core.setup_admin("outage-admin", "synthetic-admin-password")
        self.token = self.core.login("outage-admin", "synthetic-admin-password")
        self.mid = self.core.join_member("SavedProfile#QA", "TOP", "JG")
        self.core.approve_member(self.token, self.mid, 240)
        self.addCleanup(ui.services.clear)
        self.addCleanup(ui.lounge_service.clear)
        self.private_error = "postgresql://hidden-db-user:hidden-db-password@example.invalid/private-schema"
        for replacement in (
            patch.object(riot_ui, "riot_service", side_effect=sqlite3.OperationalError(self.private_error)),
            patch.object(riot_ui, "load_riot_config", side_effect=AssertionError("No real Riot configuration")),
            patch("roly.storage_config._runtime_document", side_effect=AssertionError("No real DB configuration")),
            patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temporary.name}),
            patch.object(ui, "operating_database", return_value=str(self.path)),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def set_cache(self, cached):
        with self.core.transaction() as db:
            db.execute("DELETE FROM riot_profiles WHERE member_id=?", (self.mid,))
            if cached:
                member = self.core.get_member(self.mid, db)
                payload = {"current_tier": "골드 2", "lp": 35, "updated_at": "2026-09-08T02:00:00+00:00",
                           "champions": [{"id": 22, "name": "애쉬", "points": 12345, "level": 10, "icon_url": ""}]}
                db.execute("INSERT INTO riot_profiles(member_id,canonical_id,payload,fetched_at) VALUES(?,?,?,?)",
                           (self.mid, member["canonical_id"], json.dumps(payload), 1788832800.0))

    def healthy(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])
        rendered = "\n".join(str(item.value) for kind in ("caption", "text", "markdown", "warning", "error")
                             for item in getattr(app, kind))
        self.assertNotIn("hidden-db-password", rendered)
        self.assertNotIn("private-schema", rendered)
        self.assertTrue(any("Riot 갱신 서버에 연결하지 못했습니다." in item.value for item in app.caption))

    def test_actual_profile_route_keeps_empty_and_cached_profiles_after_service_failure(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                self.set_cache(cached)
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25)
                app.session_state.space = "운영 공간"
                app.session_state.db_path = str(self.path)
                app.session_state.token = self.token
                app.run()
                self.assertFalse(app.exception, [error.message for error in app.exception])
                app.query_params["member"] = str(self.mid)
                app.switch_page("app_pages/profile.py").run()
                self.healthy(app)
                self.assertTrue(any(item.value == "주력 챔피언" for item in app.subheader))
                self.assertTrue(any("전력점수 240 P" in item.value for item in app.caption))
                self.assertTrue(any(button.label == "회원 정보 수정" for button in app.button))
                self.assertFalse(any(button.label == "갱신하기" for button in app.button))
                if cached:
                    self.assertTrue(any("골드 2" in item.proto.body for item in app.get("html")))
                    self.assertTrue(any("챔피언" in table.value.columns and "애쉬" in table.value["챔피언"].tolist()
                                        for table in app.dataframe))
                else:
                    self.assertIn("아직 저장된 Riot 정보가 없습니다.", [item.value for item in app.caption])

    def test_individual_control_falls_back_to_cache_and_leaves_other_input_usable(self):
        script = '''
import streamlit as st
from roly.core import Core
from roly.riot_ui import refresh_control
core = Core(st.session_state.path)
refresh_control(core, st.session_state.token, [st.session_state.mid], key="outage_detail", show_details=True)
st.text_input("내전 이름", key="unrelated_title")
'''
        for cached in (False, True):
            with self.subTest(cached=cached):
                self.set_cache(cached)
                app = AppTest.from_string(script, default_timeout=15)
                app.session_state.path = str(self.path)
                app.session_state.token = self.token
                app.session_state.mid = self.mid
                app.run()
                self.healthy(app)
                if cached:
                    self.assertTrue(any("골드 2" in item.value for item in app.markdown))
                    self.assertTrue(any(item.value == "애쉬" for item in app.text))
                else:
                    self.assertIn("Riot API 조회 전입니다.", [item.value for item in app.caption])
                app.text_input(key="unrelated_title").set_value("이름 수정 가능").run()
                self.healthy(app)
                self.assertEqual(app.text_input(key="unrelated_title").value, "이름 수정 가능")

    def test_preparation_picker_failure_does_not_expose_error_or_block_creation_form(self):
        script = '''
import streamlit as st
from roly.core import Core
from roly.riot_ui import member_refresh_picker
core = Core(st.session_state.path)
member_refresh_picker(core, st.session_state.token, core.list_members(), key="outage_picker")
with st.form("creation"):
    st.text_input("내전 이름", key="creation_title")
    st.form_submit_button("내전 생성")
'''
        app = AppTest.from_string(script, default_timeout=15)
        app.session_state.path = str(self.path)
        app.session_state.token = self.token
        app.run()
        self.healthy(app)
        self.assertFalse(any(item.label == "조회할 회원" for item in app.selectbox))
        self.assertTrue(any(button.label == "내전 생성" and not button.disabled for button in app.button))
        app.text_input(key="creation_title").set_value("경매 준비 유지")
        app.button[0].click().run()
        self.healthy(app)
        self.assertEqual(app.text_input(key="creation_title").value, "경매 준비 유지")


if __name__ == "__main__":
    unittest.main()
