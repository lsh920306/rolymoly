"""Native roster editing keeps corrections attached to member IDs."""
import json
import unittest

from streamlit.testing.v1 import AppTest


SOURCE = '''
import streamlit as st
from roly.roster_ui import roster_editor
roles = ["TOP", "JG", "MID", "AD", "SUP"]
members = [{"id": i + 1, "riot_id": f"선수{i + 1}#QA", "main_role": roles[i % 5], "score": 100 + i} for i in range(12)]
if st.session_state.get("updated_score"):
    members[0]["score"] = 250
if st.session_state.get("exclude_one"):
    members = [member for member in members if member["id"] != 1]
matching_members = members + [{"id": 99, "riot_id": "대기#QA", "status": "PENDING"}]
result = roster_editor(members, key="qa_roster", team_count=2, saved=st.session_state.get("saved_roster", ()), matching_members=matching_members)
st.json(result)
'''


class RosterUITests(unittest.TestCase):
    def setUp(self):
        self.app = AppTest.from_string(SOURCE).run()

    def healthy(self):
        self.assertFalse(self.app.exception, [error.message for error in self.app.exception])

    def roles(self):
        return {item["member_id"]: item["role"] for item in json.loads(self.app.json[0].value)}

    def edit(self, edits):
        editor = self.app.dataframe[0]
        states = self.app._tree.get_widget_states()
        value = next((item for item in states.widgets if item.id == editor.proto.id), None)
        if value is None:
            value = states.widgets.add()
            value.id = editor.proto.id
        value.string_value = json.dumps({"edited_rows": edits, "added_rows": [], "deleted_rows": []})
        self.app._run(states)
        self.healthy()

    def test_role_edits_survive_more_selections_reordering_and_removal(self):
        self.app.multiselect(key="qa_roster_members").set_value([1, 2]).run()
        self.edit({"0": {"배정 포지션": "SUP"}})
        self.assertEqual(self.roles(), {1: "SUP", 2: "JG"})
        self.app.multiselect(key="qa_roster_members").set_value([3, 1, 2]).run()
        self.assertEqual(self.roles(), {3: "MID", 1: "SUP", 2: "JG"})
        self.edit({"0": {"배정 포지션": "AD"}})
        self.assertEqual(self.roles()[3], "AD")
        self.app.multiselect(key="qa_roster_members").set_value([3, 2]).run()
        self.assertEqual(self.roles(), {3: "AD", 2: "JG"})
        self.app.multiselect(key="qa_roster_members").set_value([1, 2]).run()
        self.assertEqual(self.roles(), {1: "SUP", 2: "JG"})
        self.app.session_state["updated_score"] = True
        self.app.run()
        self.healthy()
        self.assertEqual(self.app.dataframe[0].value.iloc[0]["전력점수"], 250)
        self.assertEqual(self.roles(), {1: "SUP", 2: "JG"})

    def test_deactivated_selection_removed_and_shortage_visible(self):
        self.app.multiselect(key="qa_roster_members").set_value([1, 2]).run()
        self.app.session_state["exclude_one"] = True
        self.app.run()
        self.healthy()
        self.assertEqual(self.roles(), {2: "JG"})
        self.assertTrue(any("부족:" in item.value for item in self.app.caption))
        self.assertEqual(self.app.multiselect(key="qa_roster_members").value, [2])

    def test_new_saved_roles_replace_old_unsaved_editor_changes(self):
        self.app.session_state["saved_roster"] = [{"member_id": 1, "role": "TOP"}, {"member_id": 2, "role": "JG"}]
        self.app.run()
        self.edit({"0": {"배정 포지션": "SUP"}})
        self.assertEqual(self.roles(), {1: "SUP", 2: "JG"})
        self.app.session_state["saved_roster"] = [{"member_id": 1, "role": "MID"}, {"member_id": 2, "role": "JG"}]
        self.app.run()
        self.healthy()
        self.assertEqual(self.roles(), {1: "MID", 2: "JG"})
        self.app.run()
        self.assertEqual(self.roles(), {1: "MID", 2: "JG"})

    def test_pasted_matches_merge_with_draft_roles_and_show_rejected_rows(self):
        self.app.multiselect(key="qa_roster_members").set_value([1]).run()
        self.edit({"0": {"배정 포지션": "SUP"}})
        self.app.text_area(key="qa_roster_paste_text").set_value("선수2#qa\n선수2#QA\n대기#QA\n미등록#QA\n선수3").run()
        self.healthy()
        preview = self.app.dataframe[0].value
        self.assertEqual(list(preview["확인"]), ["일치", "명단 중복", "승인 전·탈퇴 회원", "등록 회원 없음", "Riot ID 형식 오류"])
        self.app.button(key="qa_roster_paste_apply").click().run()
        self.healthy()
        self.assertEqual(self.roles(), {1: "SUP", 2: "JG"})
        self.assertEqual(self.app.multiselect(key="qa_roster_members").value, [1, 2])

    def test_paste_cannot_add_more_than_capacity(self):
        self.app.multiselect(key="qa_roster_members").set_value(list(range(1, 11))).run()
        self.app.text_area(key="qa_roster_paste_text").set_value("선수11#QA\n선수12#QA").run()
        self.healthy()
        self.assertTrue(self.app.button(key="qa_roster_paste_apply").disabled)
        self.assertTrue(any("정원 10명" in row.value for row in self.app.warning))
        self.assertEqual(len(self.roles()), 10)

    def test_existing_textarea_accepts_numbered_notes_with_preview_and_keeps_db_roles(self):
        self.assertTrue(self.app.expander[0].proto.expanded)
        field = self.app.text_area(key="qa_roster_paste_text")
        self.assertEqual(field.label, "Riot ID 명단")
        self.assertIn("겨울#kr99 / d2 / ad mid", field.proto.placeholder)
        field.set_value("1. 선수1#qa / d2 / ad mid\n2) 선수2# QA/s/서폿\n3. 선수2#qa / M\n4. 대기#qa/d1").run()
        self.healthy()
        preview = self.app.dataframe[0].value
        self.assertEqual(list(preview["추출 Riot ID"]), ["선수1#QA", "선수2#QA", "선수2#QA", "대기#QA"])
        self.assertEqual(list(preview["확인"]), ["일치", "일치", "명단 중복", "승인 전·탈퇴 회원"])
        self.assertTrue(all(preview["사유"]))
        self.assertEqual(preview.iloc[0]["원문"], "1. 선수1#qa / d2 / ad mid")
        self.app.button(key="qa_roster_paste_apply").click().run()
        self.healthy()
        self.assertEqual(self.roles(), {1: "TOP", 2: "JG"})

    def test_shared_editor_applies_numbered_rosters_for_normal_and_auction_capacities(self):
        source = '''
import streamlit as st
from roly.roster_ui import roster_editor
roles = ["TOP", "JG", "MID", "AD", "SUP"]
members = [{"id": i+1, "riot_id": f"회원{i+1}#KR1", "main_role": roles[i%5], "score": 100, "status":"APPROVED"} for i in range(41)]
result = roster_editor(members, key="bulk", team_count=st.session_state["teams"], strict_roles=st.session_state["teams"] <= 4)
st.json(result)
'''
        for teams in (2, 4, 6, 8):
            with self.subTest(teams=teams):
                app = AppTest.from_string(source)
                app.session_state["teams"] = teams
                app.run()
                count = teams * 5
                text = "\n".join(f"{i}. 회원{i}#kr1 / d2 / ad mid" for i in range(1, count + 1))
                app.text_area(key="bulk_paste_text").set_value(text).run()
                app.button(key="bulk_paste_apply").click().run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertEqual(app.multiselect(key="bulk_members").value, list(range(1, count + 1)))
                result = json.loads(app.json[0].value)
                self.assertEqual([p["role"] for p in result], ["TOP", "JG", "MID", "AD", "SUP"] * teams)
                app.text_area(key="bulk_paste_text").set_value(f"{count + 1}. 회원{count + 1}#KR1 / sup").run()
                self.assertTrue(app.button(key="bulk_paste_apply").disabled)


if __name__ == "__main__":
    unittest.main()
