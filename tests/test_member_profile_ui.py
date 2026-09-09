"""Member editing uses the same DB identity across personal and admin screens."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition
from roly.ui import services


ROOT = Path(__file__).resolve().parents[1]
EDITOR = '''
import streamlit as st
from roly.core import Core
from roly.member_editor import member_edit_form
core = Core(st.session_state.db_path)
actor = core.session(st.session_state.token)
member_edit_form(core, st.session_state.token, actor, st.session_state.member_id, prefix="qa")
'''


class MemberProfileUITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="roly-profile-ui-")
        self.addCleanup(temp.cleanup)
        environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(services.clear)
        self.core = Core(Path(temp.name) / "rolymoly.sqlite3")
        self.comp = Competition(self.core)
        self.core.setup_admin("admin", "synthetic-admin-password")
        self.token = self.core.login("admin", "synthetic-admin-password")
        self.mid = self.core.register_member("member", "synthetic-member-password", "Member#QA", "TOP", "JG",
                                              request_key=uuid4().hex, current_tier="골드 2", current_tier_lp=37)["member_id"]
        self.core.approve_member(self.token, self.mid, 130, "PRIVATE-MEMBER-NOTES")
        self.member_token = self.core.login("member", "synthetic-member-password")

    def healthy(self, app):
        self.assertFalse(app.exception, [item.message for item in app.exception])

    def widget(self, app, kind, label):
        matches = [item for item in getattr(app, kind) if item.label == label]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def editor(self, token=None):
        app = AppTest.from_string(EDITOR, default_timeout=30)
        for key, value in {"db_path": self.core.db_path, "token": token or self.token, "member_id": self.mid}.items():
            app.session_state[key] = value
        app.run()
        self.healthy(app)
        return app

    def page(self, name, token):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        for key, value in {"space": "운영 공간", "db_path": self.core.db_path, "token": token}.items():
            app.session_state[key] = value
        app.run()
        app.switch_page(f"app_pages/{name}.py").run()
        self.healthy(app)
        return app

    def test_editor_saves_tiers_without_changing_score_or_login_identity(self):
        app = self.editor()
        self.widget(app, "text_input", "Riot ID").set_value("Renamed#QA")
        self.widget(app, "text_input", "클랜 티어").set_value("클랜 골드")
        self.widget(app, "selectbox", "현재 티어").set_value("플래티넘 3")
        self.widget(app, "number_input", "현재 LP (선택)").set_value(65)
        self.widget(app, "text_input", "정보 변경 사유").set_value("운영진 티어 확인")
        self.widget(app, "button", "회원 정보 저장").click().run()
        self.healthy(app)
        self.assertFalse(app.error)
        member = self.core.get_member(self.mid)
        self.assertEqual((member["clan_tier"], member["current_tier"], member["current_tier_lp"], member["score"]), ("클랜 골드", "플래티넘 3", 65, 130))
        actor = self.core.session(self.member_token)
        self.assertEqual((actor["member_id"], actor["username"], actor["display_name"]), (self.mid, "member", "Renamed#QA"))

    def test_stale_edit_is_blocked_and_reload_discards_old_fields(self):
        app = self.editor()
        self.widget(app, "text_input", "클랜 티어").set_value("old unsaved tier")
        self.widget(app, "number_input", "기본점수").set_value(999)
        self.widget(app, "text_input", "정보 변경 사유").set_value("old form")
        before = self.core.get_member(self.mid)
        self.core.update_member(self.token, self.mid, "OtherAdmin#QA", "MID", "SUP", 240, "다른 운영진 확인",
                                clan_tier="클랜 플래티넘", expected_updated_at=before["updated_at"])
        self.widget(app, "button", "회원 정보 저장").click().run()
        self.healthy(app)
        self.assertTrue(self.widget(app, "button", "회원 정보 저장").disabled)
        self.assertEqual(self.core.get_member(self.mid)["score"], 240)
        self.widget(app, "button", "최신 회원 정보 불러오기").click().run()
        self.healthy(app)
        self.assertEqual(self.widget(app, "text_input", "Riot ID").value, "OtherAdmin#QA")
        self.assertEqual(self.widget(app, "text_input", "클랜 티어").value, "클랜 플래티넘")
        self.assertEqual(self.widget(app, "number_input", "기본점수").value, 240)
        self.assertEqual(self.widget(app, "text_input", "정보 변경 사유").value, "")
        self.assertFalse(self.widget(app, "button", "회원 정보 저장").disabled)

    def test_member_cannot_edit_and_public_list_has_only_public_tiers(self):
        editor = self.editor(self.member_token)
        self.assertTrue(editor.error)
        self.assertFalse(editor.text_input)
        app = self.page("members", self.member_token)
        public_text = " ".join(item.value for item in [*app.markdown, *app.caption, *app.text])
        self.assertIn("골드 2", public_text)
        self.assertIn("37 LP", public_text)
        self.assertNotIn("PRIVATE", public_text)
        self.assertFalse(any(item.label == "회원 정보 수정" for item in app.button))
        app.button(key=f"member_profile_{self.mid}").click().run()
        self.healthy(app)
        self.assertFalse(any(item.label == "회원 정보 수정" for item in app.button))

    def test_member_row_profile_opens_shared_editor_for_exact_member_id(self):
        app = self.page("members", self.token)
        app.button(key=f"member_profile_{self.mid}").click().run()
        self.healthy(app)
        self.assertEqual(app.query_params.get("member"), [str(self.mid)])
        # Align AppTest's next client request after the server page redirect.
        app.switch_page("app_pages/profile.py").run()
        app.button(key=f"profile_edit_{self.mid}").click().run()
        self.healthy(app)
        self.assertEqual(self.widget(app, "text_input", "Riot ID").value, "Member#QA")
        self.assertEqual(self.widget(app, "selectbox", "현재 티어").value, "골드 2")

    def test_member_list_edit_opens_selected_member_without_riot_request(self):
        other = self.core.join_member("Another#QA", "SUP", "MID")
        self.core.approve_member(self.token, other, 240)
        with patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No API configuration needed")), \
                patch("roly.riot_api.RiotClient", side_effect=AssertionError("No HTTP needed")):
            app = self.page("members", self.token)
            app.text_input[0].set_value("Another#QA").run()
            app.button(key=f"member_edit_{other}").click().run()
            self.healthy(app)
            self.assertEqual(self.widget(app, "text_input", "Riot ID").value, "Another#QA")
            self.assertEqual(self.widget(app, "selectbox", "주 포지션").value, "SUP")
            self.assertEqual(self.widget(app, "selectbox", "부 포지션").value, "MID")
            self.assertFalse(self.widget(app, "text_input", "클랜 티어").disabled)
            self.assertFalse(self.widget(app, "button", "회원 정보 저장").disabled)
        self.assertEqual(self.core.get_member(self.mid)["riot_id"], "Member#QA")

    def test_personal_admin_edits_api_member_roles_and_clan_for_next_normal_snapshot(self):
        from roly.core import ROLES
        from roly.riot_profile import member_profiles
        account = self.core.session(self.member_token)
        self.core.set_account_role(self.token, account["id"], "admin")
        personal_admin = self.core.login("member", "synthetic-member-password")
        self.assertEqual(self.core.session(personal_admin)["member_id"], self.mid)
        champions = [{"id": index, "name": f"저장 챔피언 {index}", "points": 90000 - index * 1000,
                      "level": 20 - index, "icon_url": ""} for index in range(1, 6)]
        payload = {"current_tier": "골드 2", "lp": 37, "rank_wins": 8, "rank_losses": 2,
                   "flex_current_tier": "에메랄드 3", "flex_lp": 64, "flex_rank_wins": 12, "flex_rank_losses": 7,
                   "champions": champions, "profile_icon_url": "", "summoner_level": 100,
                   "updated_at": "2026-09-08T02:03:04+00:00", "puuid": "internal-only-fixture"}
        member = self.core.get_member(self.mid)
        with self.core.transaction() as db:
            db.execute("UPDATE members SET current_tier_source='riot',current_tier_updated_at=? WHERE id=?",
                       (payload["updated_at"], self.mid))
            db.execute("INSERT INTO riot_profiles(member_id,canonical_id,payload,fetched_at) VALUES(?,?,?,?)",
                       (self.mid, member["canonical_id"], json.dumps(payload), 1788832984.0))
        cached_before = member_profiles(self.core, [self.mid])[self.mid]
        ids = [self.mid]
        for index in range(1, 10):
            mid = self.core.join_member(f"NextRoster{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.token, mid, 130)
            ids.append(mid)
        assignments = [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(ids)]
        old_event = self.comp.create_normal(personal_admin, assignments, title="수정 전 명단", balanced=False)
        old_players = self.comp.get_event(old_event)["players"]
        with patch("roly.riot_ui.load_riot_config", side_effect=AssertionError("No real key in editor rehearsal")), \
                patch("roly.riot_api.RiotClient", side_effect=AssertionError("No HTTP in editor rehearsal")):
            app = self.page("members", personal_admin)
            app.button(key=f"member_profile_{self.mid}").click().run()
            self.healthy(app)
            app.switch_page("app_pages/profile.py").run()
            app.button(key=f"profile_edit_{self.mid}").click().run()
            self.healthy(app)
            self.assertTrue(self.widget(app, "selectbox", "현재 티어").disabled)
            self.assertTrue(self.widget(app, "number_input", "현재 LP (선택)").disabled)
            # AppTest reruns the entire page, unlike the browser's dialog-only
            # submit. Reuse the decorated-dialog harness for that next request.
            from tests.test_dialog_sessions import DIALOG_APP
            profile_app = app
            app = AppTest.from_string(DIALOG_APP, default_timeout=30)
            for key, value in {"test_db": self.core.db_path, "dialog_kind": "member", "event_id": None,
                               "dialog_token": personal_admin, "token": personal_admin, "member_id": self.mid,
                               "captured_actor": self.core.session(personal_admin)}.items():
                app.session_state[key] = value
            app.run()
            self.healthy(app)
            self.widget(app, "selectbox", "주 포지션").set_value("MID")
            self.widget(app, "selectbox", "부 포지션").set_value("SUP")
            self.widget(app, "text_input", "클랜 티어").set_value("클랜 마스터")
            self.widget(app, "text_input", "정보 변경 사유").set_value("주·부 포지션 및 클랜 티어 검토")
            self.widget(app, "button", "회원 정보 저장").click().run()
            self.healthy(app)
            self.assertFalse(app.error)
            app = profile_app.switch_page("app_pages/profile.py").run()
            self.healthy(app)
            self.assertEqual(app.dataframe[0].value["챔피언"].tolist(), [row["name"] for row in champions])
        updated = self.core.get_member(self.mid)
        self.assertEqual((updated["main_role"], updated["sub_role"], updated["clan_tier"]), ("MID", "SUP", "클랜 마스터"))
        self.assertEqual((updated["current_tier"], updated["current_tier_lp"], updated["current_tier_source"], updated["score"]),
                         ("골드 2", 37, "riot", 130))
        self.assertEqual(member_profiles(self.core, [self.mid])[self.mid], cached_before)
        self.assertEqual(self.comp.get_event(old_event)["players"], old_players)
        assignments[0]["role"], assignments[2]["role"] = "MID", "TOP"
        next_event = self.comp.create_normal(personal_admin, assignments, title="수정 후 명단", balanced=False)
        player = next(row for row in self.comp.get_event(next_event)["players"] if row["member_id"] == self.mid)
        self.assertEqual((player["role"], player["main_role_snapshot"], player["sub_role_snapshot"], player["clan_tier_snapshot"]),
                         ("MID", "MID", "SUP", "클랜 마스터"))
        self.assertEqual((player["current_tier_snapshot"], player["current_tier_lp_snapshot"], player["score"]),
                         ("골드 2", 37, 130))
        self.assertEqual(self.core.session(personal_admin)["member_id"], self.mid)

    def test_invalid_tier_lp_and_missing_reason_leave_member_unchanged(self):
        before = self.core.get_member(self.mid)
        app = self.editor()
        self.widget(app, "selectbox", "현재 티어").set_value("언랭크")
        self.widget(app, "text_input", "정보 변경 사유").set_value("rank correction")
        self.widget(app, "button", "회원 정보 저장").click().run()
        self.healthy(app)
        self.assertTrue(app.error)
        self.assertEqual(self.core.get_member(self.mid), before)
        self.widget(app, "selectbox", "현재 티어").set_value("골드 2")
        self.widget(app, "text_input", "정보 변경 사유").set_value("")
        self.widget(app, "button", "회원 정보 저장").click().run()
        self.assertTrue(app.error)
        self.assertEqual(self.core.get_member(self.mid), before)

    def test_signup_current_tier_is_saved_and_approval_retains_the_same_account(self):
        app = self.page("join", None)
        for label, value in (("사용할 로그인 아이디", "ranked-member"), ("사용할 비밀번호 (10자 이상)", "synthetic-ranked-password"),
                             ("비밀번호 확인", "synthetic-ranked-password"), ("Riot ID", "NewRank#QA")):
            self.widget(app, "text_input", label).set_value(value)
        self.widget(app, "selectbox", "현재 티어").set_value("마스터")
        self.widget(app, "number_input", "현재 LP (선택)").set_value(327)
        app.checkbox[0].check()
        self.widget(app, "button", "가입 신청하기").click().run()
        self.healthy(app)
        self.assertFalse(app.error)
        actor = self.core.session(app.session_state.token)
        member = self.core.get_member(actor["member_id"])
        self.assertEqual((member["current_tier"], member["current_tier_lp"], member["clan_tier"], member["base_score"]), ("마스터", 327, "", 0))
        self.core.approve_member(self.token, member["id"], 700)
        app.run()
        self.healthy(app)
        after = self.core.session(app.session_state.token)
        self.assertEqual((after["id"], after["member_id"], after["member_status"]), (actor["id"], member["id"], "APPROVED"))
        self.assertTrue(any("현재 티어 마스터" in item.value for item in app.markdown))


if __name__ == "__main__":
    unittest.main()
