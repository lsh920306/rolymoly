"""Native browser-payload retries and stale roster confirmation, temporary DB only."""
import sqlite3
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import patch

from roly.competition import Competition
from roly.core import Core
from roly.tournament import TournamentService
from roly.tournament_ui import participant_rows, profile_snapshot_columns
from tests import test_tournament_ui as fixtures


class CreationRequestUITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())
        self.case = fixtures.TournamentUITests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app, self.core = self.case.app, self.case.core
        self.comp, self.service, self.token = self.case.competition, self.case.service, self.case.token

    def widget(self, kind, label):
        found = [w for w in getattr(self.app, kind) if w.label == label]
        if kind == "button" and label == "경매 내전 만들기":
            found = [widget for widget in found if widget.proto.is_form_submitter]
        self.assertEqual(len(found), 1, label)
        return found[0]

    def healthy(self):
        self.assertFalse(self.app.exception, [x.message for x in self.app.exception])

    def event_count(self):
        return len(self.comp.list_events())

    def prepare_normal(self):
        self.app.switch_page("app_pages/normal.py").run()
        self.widget("text_input", "내전 이름").set_value("같은 제목")
        self.widget("multiselect", "참가 회원 검색·선택").set_value(self.case.ids[:10]).run()
        self.widget("checkbox", "전력점수로 팀 균형 맞추기").set_value(False)
        button = self.widget("button", "일반 내전 만들기")
        button.click()
        return self.app._tree.get_widget_states(), button.proto.id

    def test_normal_native_replay_lost_reply_and_new_draft_are_distinct(self):
        request, button_id = self.prepare_normal()
        draft = self.app.session_state["normal_creation_draft"]["request_key"]
        original = Competition.create_normal
        calls = []
        def commit_then_lose(instance, *args, **kwargs):
            event_id = original(instance, *args, **kwargs)
            calls.append(kwargs["request_key"])
            if len(calls) == 1:
                raise sqlite3.OperationalError("synthetic lost reply after commit")
            return event_id
        with patch.object(Competition, "create_normal", new=commit_then_lose):
            self.app._run(request)
            self.healthy()
            self.assertTrue(self.app.error)
            self.assertEqual(self.event_count(), 1)
            self.assertEqual(self.widget("button", "일반 내전 만들기").proto.id, button_id)
            self.assertEqual(self.widget("text_input", "내전 이름").value, "같은 제목")
            self.assertEqual(self.widget("multiselect", "참가 회원 검색·선택").value, self.case.ids[:10])
            self.assertEqual(self.app.session_state["normal_creation_draft"]["request_key"], draft)
            self.app._run(request)
            self.healthy()
            self.assertFalse(self.app.error)
            self.assertEqual(self.event_count(), 1)
            self.assertEqual(calls, [draft, draft])
        new_draft = self.app.session_state["normal_creation_draft"]["request_key"]
        self.assertNotEqual(new_draft, draft)
        self.assertEqual(self.widget("multiselect", "참가 회원 검색·선택").value, [])
        self.app._run(request)
        self.healthy()
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(self.widget("multiselect", "참가 회원 검색·선택").value, [])
        self.widget("text_input", "내전 이름").set_value("같은 제목")
        self.widget("multiselect", "참가 회원 검색·선택").set_value(self.case.ids[:10]).run()
        self.widget("checkbox", "전력점수로 팀 균형 맞추기").set_value(False)
        self.widget("button", "일반 내전 만들기").click().run()
        self.healthy()
        self.assertEqual(self.event_count(), 2)
        self.assertEqual({e["title"] for e in self.comp.list_events()}, {"같은 제목"})

    def test_auction_native_replay_keeps_six_team_body_and_does_not_fill_new_form(self):
        self.app.button(key="t_open_create").click().run()
        self.widget("segmented_control", "참가 인원").set_value(6).run()
        self.widget("text_input", "경매 이름").set_value("같은 경매")
        self.widget("text_area", "참가 안내 (선택)").set_value("재시도에도 보존할 안내")
        self.widget("selectbox", "경기 방식").set_value("GROUP_STAGE")
        self.widget("button", "경매 내전 만들기").click()
        request = self.app._tree.get_widget_states()
        draft = self.app.session_state["t_creation_draft"]["request_key"]
        original = TournamentService.create
        calls = []
        def commit_then_lose(instance, *args, **kwargs):
            event_id = original(instance, *args, **kwargs)
            calls.append(kwargs["request_key"])
            if len(calls) == 1:
                raise sqlite3.OperationalError("synthetic lost reply after commit")
            return event_id
        with patch.object(TournamentService, "create", new=commit_then_lose):
            self.app._run(request)
            self.healthy()
            self.assertTrue(self.app.error)
            self.assertEqual(self.widget("text_input", "경매 이름").value, "같은 경매")
            self.assertEqual(self.widget("text_area", "참가 안내 (선택)").value, "재시도에도 보존할 안내")
            self.assertEqual(self.widget("segmented_control", "참가 인원").value, 6)
            self.assertEqual(self.widget("selectbox", "경기 방식").value, "GROUP_STAGE")
            self.app._run(request)
            self.healthy()
            self.assertEqual(calls, [draft, draft])
        self.assertEqual(self.event_count(), 1)
        event = self.comp.get_event(self.comp.list_events()[0]["id"])
        self.assertEqual((event["team_count"], event["format"], event["description"]), (6, "GROUP_STAGE", "재시도에도 보존할 안내"))
        self.app.button(key="t_open_create").click().run()
        new_draft = self.app.session_state["t_creation_draft"]["request_key"]
        self.assertNotEqual(new_draft, draft)
        self.app._run(request)
        self.healthy()
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(self.widget("text_input", "경매 이름").value, "")
        self.assertEqual(self.widget("segmented_control", "참가 인원").value, 4)
        self.widget("text_input", "경매 이름").set_value("같은 경매")
        self.widget("button", "경매 내전 만들기").click().run()
        self.healthy()
        self.assertEqual(self.event_count(), 2)

    def test_old_confirm_payload_requires_explicit_latest_roster_review(self):
        eid = self.service.create(self.token, "확정 검증", self.case.starts_at, build_mode="AUCTION", team_count=4)
        self.service.open_recruitment(self.token, eid)
        self.service.set_participants(self.token, eid, self.case.assignments)
        self.app.session_state["focus_event"] = eid
        self.app.run()
        self.app.button(key=f"t_participants_confirm_{eid}").click()
        old_request = self.app._tree.get_widget_states()
        extra = self.core.join_member("Replacement#QA", "TOP", "JG")
        self.core.approve_member(self.token, extra, 999)
        current = self.service.get_event(eid)
        changed = [dict(p) for p in self.case.assignments]
        changed[0] = {"member_id": extra, "role": "TOP"}
        self.service.set_participants(self.token, eid, changed, expected_roster_token=current["roster_token"])
        self.app._run(old_request)
        self.healthy()
        self.assertEqual(self.service.get_event(eid)["status"], "RECRUITING")
        self.assertTrue(self.app.button(key=f"t_participants_confirm_{eid}").disabled)
        self.assertTrue(any("최신 명단" in w.value for w in self.app.warning))
        self.app.button(key=f"t_confirm_reload_{eid}").click().run()
        self.healthy()
        self.assertFalse(self.app.button(key=f"t_participants_confirm_{eid}").disabled)
        self.app.button(key=f"t_participants_confirm_{eid}").click().run()
        self.healthy()
        self.assertEqual(self.service.get_event(eid)["status"], "CAPTAIN_SELECTION")
        self.assertIn(extra, {p["member_id"] for p in self.service.get_event(eid)["players"]})

    def test_saved_tiers_reach_event_tables_and_csv_after_current_profile_changes(self):
        mid = self.case.ids[0]
        member = self.core.get_member(mid)
        self.core.update_member(self.token, mid, member["riot_id"], "TOP", "JG", member["base_score"], "initial profile",
                                clan_tier="당시 클랜", current_tier="골드 2", current_tier_lp=0)
        eid = self.comp.create_normal(self.token, self.case.assignments[:10], balanced=False)
        event = self.comp.get_event(eid)
        self.core.update_member(self.token, mid, "Current#QA", "SUP", "MID", member["base_score"], "later profile",
                                clan_tier="현재 클랜", current_tier="마스터", current_tier_lp=200)
        game = event["games"][0]
        gid = self.comp.record_result(self.token, eid, game["id"], game["team_a"])
        rows = participant_rows(event["players"])
        self.assertEqual(rows[0]["당시 클랜 티어"], "당시 클랜")
        self.assertEqual(rows[0]["당시 현재 티어"], "골드 2 · 0 LP")
        self.assertEqual(profile_snapshot_columns({})["당시 현재 티어"], "기록 없음")
        captured = []
        downloads = {}
        original = Core.csv_bytes
        import streamlit as st
        original_download = st.download_button
        def capture(rows):
            captured.extend(dict(row) for row in rows)
            return original(rows)
        def register_download(label, data, *args, **kwargs):
            downloads[kwargs["file_name"]] = data
            return original_download(label, data, *args, **kwargs)
        self.app.session_state["focus_event"] = eid
        self.app.session_state["records_kind"] = "NORMAL"
        with patch.object(Core, "csv_bytes", side_effect=capture), patch("streamlit.download_button", side_effect=register_download):
            self.app.session_state["events_detail_tab_NORMAL"] = "일반내전 명단"
            self.app.switch_page("app_pages/events.py").run()
            self.healthy()
            self.assertEqual(captured, [], "Rendering must not eagerly encode the roster CSV")
            roster_download = downloads[f"rolymoly_event_{eid}_roster.csv"]
            self.assertTrue(callable(roster_download))
            self.assertIn("당시 클랜", roster_download().decode("utf-8-sig"))
            self.app.session_state["events_detail_tab_NORMAL"] = "전체 경기 이력"
            self.app.run()
            self.widget("selectbox", "상세 기록을 볼 경기").set_value(gid).run()
            self.healthy()
            saved_count = len(captured)
            player_download = downloads[f"rolymoly_game_{gid}_players.csv"]
            self.assertTrue(callable(player_download))
            self.assertIn("골드 2", player_download().decode("utf-8-sig"))
            self.assertGreater(len(captured), saved_count)
        tier_rows = [row for row in captured if row.get("당시 클랜 티어") == "당시 클랜"]
        self.assertTrue(any("편성 당시 전력" in row for row in tier_rows))
        self.assertTrue(any("경기 직전 점수" in row for row in tier_rows))
        self.assertTrue(all(row["당시 현재 티어"] == "골드 2 · 0 LP" for row in tier_rows))
        tables = [table.value for table in self.app.dataframe if "경기 직전 점수" in table.value.columns]
        self.assertEqual(len(tables), 1)
        self.assertIn("골드 2 · 0 LP", list(tables[0]["당시 현재 티어"]))
        # A bounded synthetic page exercises navigation without creating 51
        # competitions. The actual database cursor contract is tested separately.
        import csv
        import io
        seed = next(row for row in self.core.list_games(kind="NORMAL") if row["id"] == gid)
        archive = [dict(seed, id=1000 - index) for index in range(51)]
        def page_rows(_core, kind=None, *, limit=None, before_id=None):
            self.assertEqual(kind, "NORMAL")
            rows = [row for row in archive if before_id is None or row["id"] < before_id]
            return rows if limit is None else rows[:limit]
        with patch.object(Core, "list_games", autospec=True, side_effect=page_rows) as query, \
             patch.object(Competition, "game_labels", return_value={}) as names, \
             patch("streamlit.download_button", side_effect=register_download):
            self.app.run()
            self.healthy()
            frame = next(table.value for table in self.app.dataframe if "경기 번호" in table.value.columns)
            self.assertEqual(frame["경기 번호"].tolist(), list(range(1000, 950, -1)))
            self.assertEqual(query.call_args.kwargs, {"kind": "NORMAL", "limit": 51, "before_id": None})
            self.assertEqual(names.call_args.args[0], list(range(1000, 950, -1)))
            self.widget("button", "이전 경기").click().run()
            self.healthy()
            frame = next(table.value for table in self.app.dataframe if "경기 번호" in table.value.columns)
            self.assertEqual(frame["경기 번호"].tolist(), [950])
            self.assertTrue(self.widget("button", "이전 경기").disabled)
            self.assertIsNone(self.widget("selectbox", "상세 기록을 볼 경기").value)
            all_csv = downloads["rolymoly_normal_game_history.csv"]()
            exported = list(csv.DictReader(io.StringIO(all_csv.decode("utf-8-sig"))))
            self.assertEqual([int(row["경기 번호"]) for row in exported], list(range(1000, 949, -1)))
            self.assertEqual(query.call_args.kwargs, {"kind": "NORMAL"})
            self.widget("button", "최근 경기").click().run()
            self.healthy()
            self.assertEqual(query.call_args.kwargs["before_id"], None)


if __name__ == "__main__":
    unittest.main()
