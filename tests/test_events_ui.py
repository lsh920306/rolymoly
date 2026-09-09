"""Exercise the real multipage entrypoint with disposable demonstration data."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.competition import Competition, ROLES
from roly.tournament import TournamentService


ROOT = Path(__file__).resolve().parents[1]


class EventsUITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="roly-events-ui-")
        self.environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.directory.name})
        self.environment.start()
        self.at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assert_clean()
        self.at.switch_page("app_pages/events.py").run()
        self.assert_clean()
        self.core = Core(self.at.session_state["db_path"])
        self.comp = Competition(self.core)
        self.token = self.at.session_state["token"]

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def assert_clean(self):
        self.assertFalse(self.at.exception, [error.message for error in self.at.exception])

    def choose(self, event_id):
        self.at.session_state["focus_event"] = event_id
        self.at.run()
        self.assert_clean()
        kind = self.comp.get_event(event_id)["kind"]
        self.assertEqual(self.at.selectbox(key=f"events_selection_{kind}").value, event_id)

    def switch_kind(self, label):
        # AppTest 1.63 exposes tabs as blocks; browser tab clicks set this state.
        self.at.session_state["events_kind_tab"] = label
        self.at.run()
        self.assert_clean()

    def submit(self, prefix):
        next(button for button in self.at.button if (button.key or "").startswith(prefix)).click().run()
        self.assert_clean()

    def selected_winner(self, game):
        reloads = [button for button in self.at.button if (button.key or "").startswith("events_result_reload_")]
        if reloads:
            reloads[0].click().run()
            self.assert_clean()
        self.at.selectbox(key=f"events_winner_{game['id']}").select(game["team_a"]).run()
        self.submit("FormSubmitter:events_result_form_")

    def members(self, count=20):
        members = self.core.list_members()
        by_role = {role: [m for m in members if m["main_role"] == role] for role in ROLES}
        return [by_role[role][index] for index in range(count // 5) for role in ROLES]

    def new_normal(self, format_name="TOURNAMENT"):
        return self.comp.create_normal(self.token,
            [{"member_id": m["id"], "role": m["main_role"]} for m in self.members()],
            title="화면 테스트", format_name=format_name)

    def test_swap_result_finalization_named_history_and_public_read(self):
        event = next(item for item in self.comp.list_events() if item["kind"] == "NORMAL" and item["status"] == "READY")
        self.choose(event["id"])
        before = self.comp.get_event(event["id"])
        first = self.at.selectbox(key=f"events_swap_first_{event['id']}").value
        previous_team = next(p["team_id"] for p in before["players"] if p["member_id"] == first)
        self.at.button(key=f"events_swap_{event['id']}").click().run()
        self.assert_clean()
        after = self.comp.get_event(event["id"])
        self.assertNotEqual(next(p["team_id"] for p in after["players"] if p["member_id"] == first), previous_team)
        game = after["games"][0]
        self.at.button(key=f"events_result_reload_{event['id']}_{game['id']}").click().run()
        self.submit("FormSubmitter:events_result_form_")
        self.assertTrue(self.at.error)
        self.selected_winner(game)
        self.assertEqual(self.comp.get_event(event["id"])["status"], "PLAYING")
        core_game_id = self.comp.get_event(event["id"])["games"][0]["core_game_id"]
        self.at.button(key=f"events_finish_{event['id']}").click().run()
        self.assert_clean()
        self.assertEqual(self.comp.get_event(event["id"])["status"], "COMPLETED")
        self.at.selectbox(key="events_history_detail_NORMAL").select(core_game_id).run()
        self.assert_clean()
        table = next(table.value for table in self.at.dataframe if "경기 직전 점수" in table.value.columns)
        self.assertEqual(len(table), 10)
        self.assertEqual(set(table["팀"]), {team["name"] for team in after["teams"]})
        self.at.session_state["token"] = None
        self.at.run()
        self.assert_clean()
        self.assertFalse(any((b.key or "").startswith("FormSubmitter:events_result_form_") for b in self.at.button))

    def test_normal_roster_stale_screen_requires_explicit_reload(self):
        event_id = self.new_normal()
        self.choose(event_id)
        original = self.comp.get_event(event_id)
        base_key = f"events_roster_base_{event_id}_{self.core.session(self.token)['id']}"
        first, second = [p for p in original["players"] if p["role"] == "TOP"][:2]
        self.comp.swap_players(self.token, event_id, first["member_id"], second["member_id"], "동시 편성 변경")
        changed = self.comp.get_event(event_id)
        self.assertNotEqual(original["roster_token"], changed["roster_token"])
        self.at.run()
        self.assert_clean()
        self.assertEqual(self.at.session_state[base_key]["token"], original["roster_token"])
        self.assertTrue(self.at.button(key=f"events_roster_save_{event_id}").disabled)
        self.at.button(key=f"events_roster_reload_{event_id}").click().run()
        self.assert_clean()
        self.assertEqual(self.at.session_state[base_key]["token"], changed["roster_token"])
        self.assertFalse(self.at.button(key=f"events_roster_save_{event_id}").disabled)

    def test_structured_audit_rows_show_readable_summaries_and_preserve_raw_records(self):
        event_id = self.new_normal()
        entries = [
            ("RESET", {"reason": "초기화 확인", "sold_count": 2, "request_id": "internal-request",
                       "payload_hash": "internal-hash", "result": {"refund_total": 42, "member_ids": [11, 12]},
                       "before": {"snapshot": "internal-snapshot"}}),
            ("SALE_CORRECTION", {"reason": "금액과 팀 정정", "before": {"team_id": 1, "team_name": "이전 팀", "amount": 42},
                                 "after": {"team_id": 2, "team_name": "정정 팀", "amount": 0}, "result": {"member_id": 11}}),
            ("SALE_CORRECTION", {"reason": "낙찰 취소 확인", "before": {"team_id": 2, "team_name": "정정 팀", "amount": 0},
                                 "after": {"team_id": None, "team_name": None, "amount": 0}, "result": {"member_id": 11}}),
            ("NORMAL_ROSTER_REPLACE", {"reason": "확정 명단 변경", "balanced": True,
                "before": {"players": [{"member_id": 11, "riot_id": "유지#QA", "role": "TOP"},
                                         {"member_id": 12, "riot_id": "제외#QA", "role": "JG"}]},
                "after": [{"member_id": 11, "riot_id": "유지#QA", "role": "JG"},
                          {"member_id": 13, "riot_id": "추가#QA", "role": "TOP"}]}),
            ("PREPARATION_REOPENED", {"reason": "준비 다시 확인", "players": [{"member_id": 11}, {"member_id": 12}],
                                     "live_settings": {"snapshot": "internal-snapshot"}}),
            ("SWAP", {"summary": "같은 포지션 선수 교환 완료", "request_id": "internal-request", "result": {}}),
            ("BID", "이전 평문 낙찰 기록 · 선수 11 · 25 포인트"),
            ("RESET", '{"reason":"형식 미확인","result":null}'),
            ("PREPARATION_REOPENED", '{"reason":"옛 형식","players":"원문 보존"}'),
        ]
        with self.core.transaction() as db:
            actor = self.core.session(self.token, db)
            for action, detail in entries:
                self.comp._audit(db, event_id, actor, action,
                                 json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail)
        before = self.comp.get_event(event_id)["audit"]
        self.choose(event_id)
        table = next(frame.value for frame in self.at.dataframe if list(frame.value.columns) == ["시각", "작업", "내용"])
        contents = "\n".join(table["내용"])
        for expected in ("낙찰 2명 해제", "42 P 환불", "선수 2명 경매 준비", "선수 #11", "이전 팀 42 P → 정정 팀 0 P",
                         "낙찰 취소·재경매 대기", "합류: 추가#QA", "제외: 제외#QA", "포지션 변경 1명",
                         "참가 명단 2명 보존", "사유: 준비 다시 확인", "같은 포지션 선수 교환 완료"):
            self.assertIn(expected, contents)
        self.assertTrue({"경매 초기화", "낙찰 정정", "일반내전 명단 재편성", "참가 명단 다시 준비", "선수 교환"}.issubset(set(table["작업"])))
        self.assertNotIn("internal-", contents)
        for _, detail in entries:
            if isinstance(detail, str):
                self.assertIn(detail, table["내용"].tolist())
        self.assertEqual(self.comp.get_event(event_id)["audit"], before)

    def test_old_normal_result_form_rejects_swap_then_explicit_reload_scores_current_roster(self):
        event_id = self.new_normal()
        self.choose(event_id)
        original = self.comp.get_event(event_id)
        game = original["games"][0]
        base_key = f"events_result_base_{event_id}_{game['id']}_{self.core.session(self.token)['id']}"
        self.at.selectbox(key=f"events_winner_{game['id']}").select(game["team_a"]).run()
        first = next(p for p in original["players"] if p["team_id"] == game["team_a"] and p["role"] == "TOP")
        second = next(p for p in original["players"] if p["team_id"] == game["team_b"] and p["role"] == "TOP")
        scores = {p["member_id"]: self.core.get_member(p["member_id"])["score"] for p in original["players"]}
        self.comp.swap_players(self.token, event_id, first["member_id"], second["member_id"], "다른 화면에서 선수 교환")
        changed = self.comp.get_event(event_id)
        # Submit the formerly enabled button without a prior refresh.
        self.submit("FormSubmitter:events_result_form_")
        self.assertEqual(self.at.session_state[base_key], original["roster_token"])
        self.assertTrue(any("최신 경기" in row.value for row in self.at.warning))
        self.assertTrue(next(button for button in self.at.button if (button.key or "").startswith("FormSubmitter:events_result_form_")).disabled)
        self.assertEqual(self.comp.get_event(event_id)["roster_token"], changed["roster_token"])
        self.assertEqual({mid: self.core.get_member(mid)["score"] for mid in scores}, scores)
        self.at.button(key=f"events_result_reload_{event_id}_{game['id']}").click().run()
        self.assert_clean()
        self.assertEqual(self.at.session_state[base_key], changed["roster_token"])
        self.assertIsNone(self.at.selectbox(key=f"events_winner_{game['id']}").value)
        self.selected_winner(game)
        recorded = next(g for g in self.comp.get_event(event_id)["games"] if g["id"] == game["id"])
        self.assertEqual(recorded["status"], "COMPLETED")
        detail = self.core.get_game(recorded["core_game_id"])
        winner_ids = {p["member_id"] for p in detail["players"] if p["team"] == "A"}
        self.assertIn(second["member_id"], winner_ids)
        self.assertNotIn(first["member_id"], winner_ids)
        self.assertEqual(self.core.get_member(second["member_id"])["score"], scores[second["member_id"]] + 10)
        self.assertEqual(self.core.get_member(first["member_id"])["score"], scores[first["member_id"]] - 10)

    def test_old_auction_result_form_requires_reload_after_other_game_advances(self):
        ids = [m["id"] for m in self.members()]
        event_id = self.comp.create_auction(self.token, ids, ids[::5], format_name="TOURNAMENT")
        for index, team in enumerate(self.comp.get_event(event_id)["teams"]):
            for mid in ids[index * 5 + 1:index * 5 + 5]:
                self.comp.bid(self.token, event_id, mid, team["id"], 0)
        self.comp.finalize_auction(self.token, event_id)
        self.choose(event_id)
        original = self.comp.get_event(event_id)
        first, second = [g for g in original["games"] if g["team_a"] and g["team_b"]]
        self.at.selectbox(key=f"events_pending_{event_id}").select(second["id"]).run()
        self.at.selectbox(key=f"events_winner_{second['id']}").select(second["team_a"]).run()
        self.comp.record_result(self.token, event_id, first["id"], first["team_a"])
        changed = self.comp.get_event(event_id)
        self.submit("FormSubmitter:events_result_form_")
        self.assertTrue(self.at.button(key=f"events_result_reload_{event_id}_{second['id']}").label)
        self.assertEqual(self.comp.get_event(event_id)["roster_token"], changed["roster_token"])
        self.selected_winner(second)
        self.assertEqual(sum(g["status"] == "COMPLETED" for g in self.comp.get_event(event_id)["games"]), 2)

    def test_correction_requires_reason_and_protects_finished_final(self):
        event_id = self.new_normal()
        semifinals = [game for game in self.comp.get_event(event_id)["games"] if game["round"] == 1]
        for game in semifinals:
            self.comp.record_result(self.token, event_id, game["id"], game["team_a"])
        self.choose(event_id)
        first = semifinals[0]
        next(box for box in self.at.selectbox if box.label == "정정 후 승리 팀").select(first["team_b"]).run()
        self.submit("FormSubmitter:events_correction_form_")
        self.assertTrue(self.at.error)
        next(box for box in self.at.text_input if box.label == "결과 정정 사유").set_value("승리팀 오입력").run()
        self.submit("FormSubmitter:events_correction_form_")
        final = self.comp.get_event(event_id)["games"][-1]
        self.assertEqual(final["team_a"], first["team_b"])
        self.selected_winner(final)
        self.at.button(key=f"events_correction_reload_{event_id}_{first['id']}").click().run()
        next(box for box in self.at.selectbox if box.label == "정정 후 승리 팀").select(first["team_a"]).run()
        next(box for box in self.at.text_input if box.label == "결과 정정 사유").set_value("후속 경기 이후 수정 시도").run()
        self.submit("FormSubmitter:events_correction_form_")
        self.assertTrue(any("분쟁" in error.value for error in self.at.error))
        self.assertEqual(self.comp.get_event(event_id)["games"][-1]["winner_team_id"], final["team_a"])

    def test_three_way_tie_additional_games_and_completion(self):
        event_id = self.new_normal("LEAGUE")
        event = self.comp.get_event(event_id)
        a, b, c, d = [team["id"] for team in event["teams"]]
        cycle = {frozenset((a, b)): a, frozenset((b, c)): b, frozenset((a, c)): c}
        for game in event["games"]:
            pair = frozenset((game["team_a"], game["team_b"]))
            winner = next(team for team in pair if team != d) if d in pair else cycle[pair]
            self.comp.record_result(self.token, event_id, game["id"], winner)
        self.choose(event_id)
        self.at.button(key=f"events_tiebreak_{event_id}_").click().run()
        self.assert_clean()
        self.assertEqual(sum(g["stage"] == "TIEBREAK" for g in self.comp.get_event(event_id)["games"]), 3)
        while pending := [g for g in self.comp.get_event(event_id)["games"] if g["status"] == "PENDING"]:
            game = pending[0]
            self.at.selectbox(key=f"events_pending_{event_id}").select(game["id"]).run()
            self.at.selectbox(key=f"events_winner_{game['id']}").select(min(game["team_a"], game["team_b"])).run()
            self.submit("FormSubmitter:events_result_form_")
        self.at.button(key=f"events_finish_{event_id}").click().run()
        self.assert_clean()
        self.assertEqual(self.comp.get_event(event_id)["status"], "COMPLETED")

    def test_auction_awards_and_unplayed_cancellation(self):
        member_ids = [m["id"] for m in self.members()]
        event_id = self.comp.create_auction(self.token, member_ids, member_ids[::5], title="경매 화면 테스트", format_name="TOURNAMENT")
        for index, team in enumerate(self.comp.get_event(event_id)["teams"]):
            for member in member_ids[index * 5 + 1:index * 5 + 5]:
                self.comp.bid(self.token, event_id, member, team["id"], 0)
        self.comp.finalize_auction(self.token, event_id)
        self.choose(event_id)
        while pending := [g for g in self.comp.get_event(event_id)["games"] if g["status"] == "PENDING" and g["team_a"] and g["team_b"]]:
            game = pending[0]
            self.at.selectbox(key=f"events_pending_{event_id}").select(game["id"]).run()
            self.selected_winner(game)
        self.at.button(key=f"events_finish_{event_id}").click().run()
        self.assert_clean()
        final = self.comp.get_event(event_id)
        self.assertEqual(final["status"], "COMPLETED")
        winner_ids = [p["member_id"] for team in final["teams"] if team["id"] == final["winner_team_id"] for p in team["players"]]
        self.assertTrue(all(self.core.get_member(member)["award_units"] == 1 for member in winner_ids))
        unplayed_id = self.new_normal()
        self.choose(unplayed_id)
        self.submit("FormSubmitter:events_cancel_form_")
        self.assertTrue(self.at.error)
        next(box for box in self.at.text_input if box.label == "일반내전 취소 사유").set_value("참가자 일정 변경").run()
        self.submit("FormSubmitter:events_cancel_form_")
        self.assertEqual(self.comp.get_event(unplayed_id)["status"], "CANCELLED")

    def test_kind_tabs_keep_event_and_history_selection_separate(self):
        normal = next(event for event in self.comp.list_events() if event["kind"] == "NORMAL" and event["status"] == "COMPLETED")
        self.choose(normal["id"])
        normal_game = self.comp.get_event(normal["id"])["games"][0]["core_game_id"]
        self.at.selectbox(key="events_history_detail_NORMAL").select(normal_game).run()
        self.assertEqual(self.at.session_state["events_kind_tab"], "일반내전")
        normal_options = self.at.selectbox(key="events_selection_NORMAL").options
        self.assertTrue(all("경매" not in option for option in normal_options))

        members = [member["id"] for member in self.members()]
        auction_id = self.comp.create_auction(self.token, members, members[::5], title="격리된 경매 기록", format_name="TOURNAMENT")
        for index, team in enumerate(self.comp.get_event(auction_id)["teams"]):
            for member in members[index * 5 + 1:index * 5 + 5]:
                self.comp.bid(self.token, auction_id, member, team["id"], 0)
        self.comp.finalize_auction(self.token, auction_id)
        first = self.comp.get_event(auction_id)["games"][0]
        self.comp.record_result(self.token, auction_id, first["id"], first["team_a"])
        auction_game = self.comp.get_event(auction_id)["games"][0]["core_game_id"]
        self.choose(auction_id)
        self.assertEqual(self.at.session_state["events_kind_tab"], "경매")
        self.assertFalse(any(box.key == "events_selection_NORMAL" for box in self.at.selectbox))
        self.at.selectbox(key="events_history_detail_AUCTION").select(auction_game).run()
        history = next(table.value for table in self.at.dataframe if "경기 번호" in table.value.columns)
        self.assertEqual(set(history["종류"]), {"경매"})
        self.assertEqual(set(history["경기 번호"]), {auction_game})

        self.switch_kind("일반내전")
        self.assertEqual(self.at.selectbox(key="events_selection_NORMAL").value, normal["id"])
        self.assertEqual(self.at.selectbox(key="events_history_detail_NORMAL").value, normal_game)
        normal_history = next(table.value for table in self.at.dataframe if "경기 번호" in table.value.columns)
        self.assertEqual(set(normal_history["종류"]), {"일반내전"})
        self.assertNotIn(auction_game, normal_history["경기 번호"].tolist())
        self.switch_kind("경매")
        self.assertEqual(self.at.selectbox(key="events_selection_AUCTION").value, auction_id)
        self.assertEqual(self.at.selectbox(key="events_history_detail_AUCTION").value, auction_game)
        self.assertFalse(any(widget.label == "경기 종류" for widget in self.at.get("button_group")))

    def test_admin_can_close_unfinished_auction_preserving_public_records(self):
        self.core.create_account(self.token, "unfinished_owner", "unfinished-owner-password", role="organizer")
        owner_token = self.core.login("unfinished_owner", "unfinished-owner-password")
        member_ids = [member["id"] for member in self.members()]
        event_id = self.comp.create_auction(owner_token, member_ids, member_ids[::5],
            title="참가자 이탈로 중단할 경매", format_name="TOURNAMENT")
        for index, team in enumerate(self.comp.get_event(event_id)["teams"]):
            for member_id in member_ids[index * 5 + 1:index * 5 + 5]:
                self.comp.bid(owner_token, event_id, member_id, team["id"], 1)
        self.comp.finalize_auction(owner_token, event_id)
        first = self.comp.get_event(event_id)["games"][0]
        self.comp.record_result(owner_token, event_id, first["id"], first["team_a"])
        before = self.comp.get_event(event_id)
        core_game_id = before["games"][0]["core_game_id"]
        core_game_before = self.core.get_game(core_game_id)
        award_units_before = {member_id: self.core.get_member(member_id)["award_units"] for member_id in member_ids}
        remaining = next(game for game in before["games"] if game["status"] == "PENDING" and game["team_a"] and game["team_b"])
        departed_id = next(player["member_id"] for player in before["players"] if player["team_id"] == remaining["team_a"])
        self.core.kick_member(self.token, departed_id, "참가자가 클랜을 탈퇴함")

        self.at.session_state["token"] = owner_token
        self.choose(event_id)
        self.assertFalse(any(button.label == "기록을 보존하고 중단 종료" for button in self.at.button))
        self.assertTrue(any("관리자에게 요청" in message.value for message in self.at.caption))
        self.at.selectbox(key=f"events_pending_{event_id}").select(remaining["id"]).run()
        self.selected_winner(remaining)
        self.assertTrue(self.at.error)
        self.assertEqual(self.comp.get_event(event_id)["status"], "PLAYING")

        self.at.session_state["token"] = self.token
        self.choose(event_id)
        self.submit("FormSubmitter:events_close_unfinished_form_")
        self.assertTrue(self.at.error)
        self.assertEqual(self.comp.get_event(event_id)["status"], "PLAYING")
        next(box for box in self.at.text_input if box.label == "중단 종료 사유").set_value("참가자 이탈로 남은 경기 진행 불가").run()
        self.submit("FormSubmitter:events_close_unfinished_form_")
        closed = self.comp.get_event(event_id)
        self.assertEqual(closed["status"], "CANCELLED")
        self.assertIsNone(closed["winner_team_id"])
        for field in ("games", "teams", "players"):
            self.assertEqual(closed[field], before[field], field)
        self.assertEqual(self.core.get_game(core_game_id), core_game_before)
        self.assertEqual({member_id: self.core.get_member(member_id)["award_units"] for member_id in member_ids}, award_units_before)
        self.assertTrue(any(row["action"] == "CLOSE_UNFINISHED" and "참가자 이탈" in row["detail"] for row in closed["audit"]))
        audit_table = next(table.value for table in self.at.dataframe if list(table.value.columns) == ["시각", "작업", "내용"])
        summary = audit_table.loc[audit_table["작업"] == "기록 보존·중단 종료", "내용"].iloc[0]
        self.assertIn("확정 1경기 보존", summary)
        self.assertIn("남은 2경기 중단", summary)
        self.assertIn("사유: 참가자 이탈로 남은 경기 진행 불가", summary)
        self.assertNotIn('"core_games"', summary)
        self.assertEqual(self.at.session_state["events_kind_tab"], "경매")

        self.at.session_state["token"] = None
        self.at.run()
        self.assert_clean()
        self.assertTrue(any("중단 종료된 경매" in message.value and "우승 보상은 지급하지 않습니다" in message.value for message in self.at.info))
        self.assertTrue(any(table.value["상태"].tolist().count("완료") == 1 for table in self.at.dataframe if "첫 번째 팀" in table.value.columns))
        self.assertFalse(any(button.label == "기록을 보존하고 중단 종료" or (button.key or "").startswith("FormSubmitter:events_result_form_") for button in self.at.button))
        self.at.selectbox(key="events_history_detail_AUCTION").select(core_game_id).run()
        self.assert_clean()
        roster = next(table.value for table in self.at.dataframe if "경기 직전 점수" in table.value.columns)
        self.assertEqual(len(roster), 10)
        self.assertEqual(set(roster["팀"]), {team["name"] for team in before["teams"] if team["id"] in (first["team_a"], first["team_b"])})
        self.at.session_state["focus_event"] = event_id
        self.at.switch_page("app_pages/auction.py").run()
        self.assert_clean()
        self.at.button(key=f"t_go_results_{event_id}").click().run()
        self.assert_clean()
        self.assertEqual(self.at.selectbox(key="events_selection_AUCTION").value, event_id)
        self.assertTrue(any("중단 종료된 경매" in message.value for message in self.at.info))

    def test_admin_can_close_twenty_player_normal_preserving_confirmed_scores(self):
        self.core.create_account(self.token, "normal_owner", "normal-owner-password", role="organizer")
        owner_token = self.core.login("normal_owner", "normal-owner-password")
        members = self.members()
        member_ids = [member["id"] for member in members]
        initial_scores = {member_id: self.core.get_member(member_id)["score"] for member_id in member_ids}
        event_id = self.comp.create_normal(owner_token,
            [{"member_id": member["id"], "role": member["main_role"]} for member in members],
            title="일부 경기 후 중단할 20인 내전", format_name="TOURNAMENT")
        self.at.session_state["token"] = owner_token
        self.choose(event_id)
        first = self.comp.get_event(event_id)["games"][0]
        self.selected_winner(first)
        before = self.comp.get_event(event_id)
        core_game_id = before["games"][0]["core_game_id"]
        confirmed_game = self.core.get_game(core_game_id)
        confirmed_scores = {member_id: self.core.get_member(member_id)["score"] for member_id in member_ids}
        winning_ids = {player["member_id"] for player in before["players"] if player["team_id"] == first["team_a"]}
        losing_ids = {player["member_id"] for player in before["players"] if player["team_id"] == first["team_b"]}
        for member_id in member_ids:
            self.assertEqual(confirmed_scores[member_id] - initial_scores[member_id],
                10 if member_id in winning_ids else -10 if member_id in losing_ids else 0)
        remaining = next(game for game in before["games"] if game["status"] == "PENDING" and game["team_a"] and game["team_b"])
        departed_id = next(player["member_id"] for player in before["players"] if player["team_id"] == remaining["team_a"])
        self.core.kick_member(self.token, departed_id, "두 번째 경기 전 참가자 탈퇴")
        self.at.run()
        self.assert_clean()
        self.assertFalse(any(button.label == "기록을 보존하고 중단 종료" for button in self.at.button))
        self.assertTrue(any("이미 확정된 경기와 점수는 유지" in message.value for message in self.at.caption))
        self.at.selectbox(key=f"events_pending_{event_id}").select(remaining["id"]).run()
        self.selected_winner(remaining)
        self.assertTrue(self.at.error)

        self.at.session_state["token"] = self.token
        self.choose(event_id)
        self.submit("FormSubmitter:events_close_unfinished_form_")
        self.assertTrue(self.at.error)
        self.assertEqual(self.comp.get_event(event_id)["status"], "PLAYING")
        next(box for box in self.at.text_input if box.label == "중단 종료 사유").set_value("탈퇴로 남은 두 경기를 진행할 수 없음").run()
        self.submit("FormSubmitter:events_close_unfinished_form_")
        closed = self.comp.get_event(event_id)
        self.assertEqual(closed["status"], "CANCELLED")
        self.assertIsNone(closed["winner_team_id"])
        for field in ("games", "teams", "players"):
            self.assertEqual(closed[field], before[field], field)
        self.assertEqual(self.core.get_game(core_game_id), confirmed_game)
        self.assertEqual({member_id: self.core.get_member(member_id)["score"] for member_id in member_ids}, confirmed_scores)
        self.assertEqual(self.at.session_state["events_kind_tab"], "일반내전")

        self.at.session_state["token"] = None
        self.at.run()
        self.assert_clean()
        notice = next(message.value for message in self.at.info if "중단 종료된 일반내전" in message.value)
        self.assertIn("이미 확정된 경기와 점수는 유지", notice)
        self.assertNotIn("우승 보상", notice)
        table = next(table.value for table in self.at.dataframe if "첫 번째 팀" in table.value.columns)
        self.assertEqual(table["상태"].tolist().count("완료"), 1)
        self.assertEqual(table["상태"].tolist().count("결과 대기"), 2)
        self.assertTrue(any(button.proto.label == "일반내전 결과 CSV 다운로드" for button in self.at.get("download_button")))
        self.assertFalse(any(button.label == "기록을 보존하고 중단 종료" or (button.key or "").startswith("FormSubmitter:events_result_form_") for button in self.at.button))
        self.at.selectbox(key="events_history_detail_NORMAL").select(core_game_id).run()
        self.assert_clean()
        roster = next(table.value for table in self.at.dataframe if "경기 직전 점수" in table.value.columns)
        self.assertEqual(len(roster), 10)
        self.assertEqual(self.core.get_game(core_game_id)["status"], "CONFIRMED")

    def test_preparation_link_opens_selected_auction_and_empty_operating_tabs(self):
        service = TournamentService(self.core, self.comp)
        older = service.create(self.token, "준비할 경매", "2026-10-01T10:00:00+00:00", build_mode="AUCTION", team_count=4, format_name="TOURNAMENT")
        newer = service.create(self.token, "다른 경매", "2026-10-02T10:00:00+00:00", build_mode="AUCTION", team_count=4, format_name="TOURNAMENT")
        self.choose(older)
        self.at.session_state["auction_event"] = newer
        self.at.button(key=f"events_open_auction_{older}").click().run()
        self.assert_clean()
        self.assertEqual(next(box for box in self.at.selectbox if box.label == "경매 선택").value, older)

        self.at.selectbox(key="space").select("운영 공간").run()
        self.at.switch_page("app_pages/events.py").run()
        self.assert_clean()
        self.assertFalse(any(box.key == "events_selection_NORMAL" for box in self.at.selectbox))
        self.switch_kind("경매")
        self.assertFalse(any(box.key == "events_selection_AUCTION" for box in self.at.selectbox))
        self.assertTrue(any("저장된 경기 결과가 없습니다" in message.value for message in self.at.info))


if __name__ == "__main__":
    unittest.main()
