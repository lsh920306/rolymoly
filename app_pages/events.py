"""Competition progress, frozen rosters, and individual game records."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

import streamlit as st

from roly.result_revision import ResultRevisionService
from roly.tournament_ui import profile_snapshot_columns
from roly.ui import (
    FORMATS, KINDS, ROLE_NAMES, STATUS,
    can_edit, context, event_picker, heading, perform, team_cards,
)


def korean_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value or "")


def round_label(game):
    if game["stage"] == "THIRD_PLACE":
        return "3·4위 결정전"
    if game["stage"] == "FINAL":
        return "결승"
    if game["stage"] == "TIEBREAK":
        return f"추가 경기 {game['round'] % 1000}라운드"
    prefix = f"{game['group_key']}조 · " if game["group_key"] else ""
    return f"{prefix}{game['round']}라운드"


def game_label(game):
    return f"{round_label(game)} · {game['team_a_name']} vs {game['team_b_name']}"


def fixture_rows(games):
    return [
        {"라운드": round_label(game), "첫 번째 팀": game["team_a_name"],
         "두 번째 팀": "부전승" if game["status"] == "BYE" else game["team_b_name"],
         "상태": {"PENDING": "결과 대기", "COMPLETED": "완료", "BYE": "부전승"}.get(game["status"], game["status"]),
         "승리팀": game["winner_name"]}
        for game in games
    ]


def clear_revision_preview():
    st.session_state.pop("events_revision_preview", None)


def save_reviewed_swap(competition, token, event_id, review, body, context_key):
    try:
        result = competition.swap_players(token, event_id, **body,
            expected_roster_token=review["token"], request_id=review["request_id"])
    except sqlite3.OperationalError:
        review["retry_body"] = dict(body)
        raise
    st.session_state.pop(context_key, None)
    return result


def audit_detail(row):
    """Summarize known receipts while preserving older plain-text records."""
    try:
        detail = json.loads(row["detail"])
        if not isinstance(detail, dict):
            return row["detail"]
        action = row["action"]
        if action == "SWAP" and isinstance(detail.get("summary"), str):
            return detail["summary"]
        if action not in ("RESET", "SALE_CORRECTION", "NORMAL_ROSTER_REPLACE", "PREPARATION_REOPENED", "CLOSE_UNFINISHED"):
            return row["detail"]
        reason = detail["reason"]
        if not isinstance(reason, str):
            return row["detail"]

        def number(value):
            if type(value) is not int:
                raise ValueError("Unknown audit number")
            return f"{value:,}"

        def records(value):
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("Unknown audit records")
            return value

        def names(players):
            labels = [str(player.get("riot_id") or f"선수 #{player['member_id']}") for player in players]
            return ", ".join(labels[:5]) + (f" 외 {len(labels) - 5}명" if len(labels) > 5 else "")

        if action == "RESET":
            result = detail["result"]
            if not isinstance(result["member_ids"], list) or any(type(mid) is not int for mid in result["member_ids"]):
                return row["detail"]
            summary = (f"낙찰 {number(detail['sold_count'])}명 해제 · {number(result['refund_total'])} P 환불 · "
                       f"선수 {len(result['member_ids'])}명 경매 준비")
        elif action == "SALE_CORRECTION":
            before, after = detail["before"], detail["after"]
            previous_team = before.get("team_name") or f"팀 #{before['team_id']}"
            previous = f"{previous_team} {number(before['amount'])} P"
            if after["team_id"] is None:
                following = "낙찰 취소·재경매 대기"
            else:
                following = f"{after.get('team_name') or '팀 #' + str(after['team_id'])} {number(after['amount'])} P"
            summary = f"선수 #{number(detail['result']['member_id'])} · {previous} → {following}"
        elif action == "NORMAL_ROSTER_REPLACE":
            before, after = records(detail["before"]["players"]), records(detail["after"])
            if not isinstance(detail["balanced"], bool):
                return row["detail"]
            old = {player["member_id"]: player for player in before}
            new = {player["member_id"]: player for player in after}
            added = [player for mid, player in new.items() if mid not in old]
            removed = [player for mid, player in old.items() if mid not in new]
            changed_roles = sum(old[mid]["role"] != player["role"] for mid, player in new.items() if mid in old)
            parts = [f"참가 명단 {len(before)}명 → {len(after)}명",
                     "전력 기준 팀 재편성" if detail["balanced"] else "선택 순서로 팀 재편성"]
            if added:
                parts.append(f"합류: {names(added)}")
            if removed:
                parts.append(f"제외: {names(removed)}")
            if changed_roles:
                parts.append(f"포지션 변경 {changed_roles}명")
            summary = " · ".join(parts)
        elif action == "PREPARATION_REOPENED":
            summary = f"참가 명단 {len(records(detail['players']))}명 보존 · 팀장·팀·경매 설정 다시 준비"
        else:
            confirmed = sum(game["status"] == "CONFIRMED" for game in records(detail["core_games"]))
            pending = sum(game["status"] == "PENDING" for game in records(detail["fixtures"]))
            summary = f"확정 {confirmed}경기 보존 · 남은 {pending}경기 중단 · 점수·낙찰 내역 유지 · 우승 보상 없음"
        return f"{summary} · 사유: {reason}"
    except (ValueError, TypeError, KeyError, AttributeError):
        # Unknown historical formats remain readable without altering the audit.
        pass
    return row["detail"]


def result_context(event, game_id, actor, mode="result"):
    """Keep the review version until this actor explicitly loads new context."""
    base_key = f"events_{mode}_base_{event['id']}_{game_id}_{actor['id']}"
    st.session_state.setdefault(base_key, event["roster_token"])
    expected = st.session_state[base_key]
    stale = expected != event["roster_token"]
    if stale:
        st.warning("다른 화면에서 명단·대진·결과가 변경되었습니다. 최신 경기를 불러와 확인한 뒤 저장해 주세요.")
        label = "최신 경기 불러오기" if mode == "result" else "최신 정정 대상 불러오기"
        if st.button(label, key=f"events_{mode}_reload_{event['id']}_{game_id}"):
            st.session_state[base_key] = event["roster_token"]
            st.session_state.pop(f"events_winner_{game_id}", None)
            st.rerun()
    return base_key, expected, stale


@st.dialog("경기 결과 정정 영향 확인", width="large", on_dismiss=clear_revision_preview)
def revision_dialog(preview):
    source = preview["source_game"]
    st.write(f"**{preview['title']} · {game_label(source)}**")
    st.write(f"승리팀: {source['winner_name']} → **{preview['winner_name']}**")
    st.caption(f"정정 사유: {preview['reason']}")
    impacts = preview["affected_games"]
    if impacts:
        st.dataframe([{"경기": game_label(game), "현재 결과": game["winner_name"] or "미진행",
            "처리": ("실경기 무효 · " if game["status"] == "COMPLETED" else "") + ("추가 대진 제외" if game["action"] == "REMOVE_TIEBREAK" else "다시 진행"),
            "변경 후 대진": f"{game['after_team_a_name']} vs {game['after_team_b_name']}"}
            for game in impacts], hide_index=True)
    else:
        st.caption("다시 진행하거나 제외할 후속 경기가 없습니다.")
    st.write(f"완료 경기 {preview['void_count']}개 무효 · 후속 대진 {preview['replay_count']}개 재계산 · 동률 추가 대진 {preview['removed_tiebreak_count']}개 제외")
    st.caption("무효 경기의 원본 결과·선수·정정 사유는 전체 경기 이력에 남습니다. 경매 경기의 전력점수 증감은 0점이며 우승 보상은 경매 종료 때 확정됩니다.")
    if preview["removed_tiebreak_count"]:
        st.caption("정정 후에도 1위가 동률이면 새로 계산된 순위를 확인하고 추가 경기를 만들어 주세요.")
    confirmed = st.checkbox("무효 처리할 경기와 변경될 대진을 확인했습니다.", key=f"events_revision_confirm_{preview['preview_token']}")
    with st.container(horizontal=True):
        if st.button("정정 확정", type="primary", disabled=not confirmed, key="events_revision_apply"):
            def apply_revision():
                current_core, current_competition, current_token, _ = context()
                ResultRevisionService(current_core, current_competition).apply(current_token, preview["preview_token"], confirmed=confirmed)
                clear_revision_preview()
            perform(apply_revision, "경기 결과를 정정하고 후속 경기와 대진을 갱신했습니다.")
        if st.button("돌아가기", key="events_revision_cancel"):
            clear_revision_preview()
            st.rerun()


core, competition, token, actor = context()
heading("경기 기록", "일반내전과 경매의 대진·명단·경기 결과를 확인합니다.")

all_events = competition.list_events()
focused_event = next((item for item in all_events if item["id"] == st.session_state.get("focus_event")), None)
if focused_event is not None:
    st.session_state.events_kind_tab = KINDS[focused_event["kind"]]
elif st.session_state.get("events_kind_tab") not in ("일반내전", "경매"):
    st.session_state.events_kind_tab = st.session_state.get("events_active_kind", "일반내전")

normal_tab, auction_tab = st.tabs(["일반내전", "경매"], key="events_kind_tab", on_change="rerun")
kind_label = st.session_state.events_kind_tab
# Keep the selected kind across navigation and action-triggered full reruns.
st.session_state.events_active_kind = kind_label
kind = "NORMAL" if kind_label == "일반내전" else "AUCTION"
selected_tab = normal_tab if kind == "NORMAL" else auction_tab
with selected_tab:
    events = [item for item in all_events if item["kind"] == kind]
    event = None
    if events:
        event_id = event_picker(events, label=f"{kind_label} 선택", key=f"events_selection_{kind}")
        event = competition.get_event(event_id)
        editable = can_edit(actor, event)
        team_names = {team["id"]: team["name"] for team in event["teams"]}
        active = event["status"] in ("READY", "PLAYING")
        completed = [game for game in event["games"] if game["status"] == "COMPLETED"]
        actual_games = [game for game in event["games"] if game["status"] != "BYE"]
        closed_unfinished = event["status"] == "CANCELLED" and any(row["action"] == "CLOSE_UNFINISHED" for row in event["audit"])
        with st.container(horizontal=True):
            st.badge(KINDS.get(event["kind"], event["kind"]), color="gray")
            st.badge(FORMATS.get(event["format"], event["format"]), color="gray")
            st.badge("중단 종료" if closed_unfinished else STATUS.get(event["status"], "진행 중"), color="green" if event["status"] == "COMPLETED" else "orange")
            st.caption(f"{len(event['teams'])}팀 · {len(event['players'])}명 · 개설 {korean_time(event['created_at'])}")
    else:
        st.caption(f"아직 {kind_label} 기록이 없습니다. {kind_label} 메뉴에서 개설할 수 있습니다.")

    bracket_tab, team_tab, history_tab = st.tabs([f"{kind_label} 결과", f"{kind_label} 명단", "전체 경기 이력"])

    with bracket_tab:
        if event is not None:
            if event["status"] in ("DRAFT", "RECRUITING", "CAPTAIN_SELECTION", "TEAM_BUILDING", "AUCTION_READY", "AUCTION", "BRACKET_SETUP"):
                st.info("경매 메뉴에서 참가자·팀 구성과 대진을 확정하면 경기 결과를 입력할 수 있습니다.")
                if st.button("경매 준비·진행 열기", icon=":material/gavel:", key=f"events_open_auction_{event_id}"):
                    st.session_state.focus_event = event_id
                    st.switch_page("app_pages/auction.py")
            elif event["status"] == "CANCELLED":
                if closed_unfinished:
                    preserved_notice = "이미 확정된 경기와 점수는 유지하며 남은 경기를 중단합니다." if kind == "NORMAL" else "기존 경기·낙찰·명단은 보존되며 우승 보상은 지급하지 않습니다."
                    st.info(f"중단 종료된 {kind_label}입니다. {preserved_notice}")
                    st.subheader("중단 당시 경기 결과")
                    st.dataframe(fixture_rows(event["games"]), hide_index=True)
                    st.download_button(f"{kind_label} 결과 CSV 다운로드", core.csv_bytes(fixture_rows(event["games"])),
                        file_name=f"rolymoly_event_{event_id}_results.csv", mime="text/csv", icon=":material/download:")
                else:
                    st.info(f"취소된 {kind_label}입니다. 명단과 변경 기록은 보존됩니다.")
            else:
                if event["status"] == "COMPLETED":
                    st.success(f"우승 · {team_names.get(event['winner_team_id'], '우승팀')}", icon=":material/emoji_events:")
                if actual_games:
                    st.progress(len(completed) / len(actual_games), text=f"실경기 {len(completed)} / {len(actual_games)} 완료")
                if event["kind"] == "NORMAL":
                    policy = event["policy_snapshot"]["score_policy"]
                    if policy["mode"] == "fixed":
                        st.caption(f"이 일반내전은 완료된 각 경기마다 승리 +{policy['k']}점 · 패배 −{policy['k']}점을 반영합니다.")
                    else:
                        st.caption(f"이 일반내전은 경기 직전 {policy['threshold']}점 이상 ±{policy['high_k']}점, 미만 ±{policy['k']}점을 반영합니다.")
                else:
                    reward = {4: "고양이 1개", 6: "별 1개", 8: "메달 1개"}.get(len(event["teams"]), "규모에 따른 우승 기호")
                    st.caption(f"경매의 개별 경기는 전력점수에 반영하지 않습니다. {kind_label} 종료 시 우승팀 각 선수에게 {reward}를 지급합니다.")

                if event["format"] == "TOURNAMENT" and event["games"]:
                    rounds = sorted({game["round"] for game in event["games"]})
                    for column, round_number in zip(st.columns(len(rounds)), rounds):
                        with column:
                            st.subheader("결승" if round_number == max(rounds) else f"{round_number}라운드")
                            for game in event["games"]:
                                if game["round"] != round_number:
                                    continue
                                with st.container(border=True):
                                    st.write(game["team_a_name"])
                                    st.caption("vs")
                                    st.write("부전승" if game["status"] == "BYE" else game["team_b_name"])
                                    if game["winner_team_id"] is not None:
                                        st.badge(f"{'진출' if game['status'] == 'BYE' else '승리'} · {game['winner_name']}", color="green")
                                    else:
                                        st.caption("결과 대기" if game["team_a"] and game["team_b"] else "앞선 경기 대기")
                else:
                    st.dataframe(fixture_rows(event["games"]), hide_index=True)

                if event.get("final_rankings"):
                    st.subheader("순위 결정전 결과")
                    st.dataframe([{"순위": row["rank"], "팀": row["team_name"]} for row in event["final_rankings"]], hide_index=True)
                    if event["status"] != "COMPLETED":
                        st.caption("아직 진행하지 않은 순위 결정 경기의 결과는 표시하지 않습니다.")

                if event["standings"]:
                    st.subheader("순위")
                    st.dataframe([
                        {"조": row["group"] or "전체", "순위": row["rank"], "팀": row["team_name"],
                         "경기": row["played"], "승": row["wins"], "패": row["losses"],
                         "승점": row["points"], "동률팀 상대 승점": row["h2h"],
                         "추가 경기 승": row["tiebreak_wins"]}
                        for row in event["standings"]
                    ], hide_index=True)
                    st.caption("승점 → 동률팀 간 승점 → 추가 경기 순서로 결정합니다. 진행 중 순위는 잠정 순위입니다.")
                    for group in (("A", "B") if event["format"] == "GROUP_STAGE" else ("",)):
                        leaders = [row for row in event["standings"] if row["group"] == group and row["rank"] == 1]
                        group_games = [game for game in event["games"] if game["group_key"] == group and game["stage"] in ("MAIN", "TIEBREAK")]
                        if len(leaders) > 1 and group_games and all(game["status"] == "COMPLETED" for game in group_games):
                            st.info(f"{group + '조' if group else '리그'} 1위가 동률입니다: {', '.join(row['team_name'] for row in leaders)}")
                            if editable and active and st.button(f"{group + '조 ' if group else ''}동률 추가 경기 만들기", key=f"events_tiebreak_{event_id}_{group}"):
                                perform(lambda group=group: competition.create_tiebreakers(token, event_id, group), "동률 팀들의 추가 경기 대진을 만들었습니다.")

                if active:
                    ready_games = [game for game in event["games"] if game["status"] == "PENDING" and game["team_a"] and game["team_b"]]
                    if editable and ready_games:
                        st.subheader(f"{kind_label} 결과 입력")
                        ready_map = {game["id"]: game for game in ready_games}
                        selected_game_id = st.selectbox("결과를 입력할 경기", list(ready_map), format_func=lambda game_id: game_label(ready_map[game_id]), key=f"events_pending_{event_id}")
                        selected_game = ready_map[selected_game_id]
                        result_base_key, result_token, stale_result = result_context(event, selected_game_id, actor)
                        with st.form(f"events_result_form_{event_id}_{selected_game_id}"):
                            winner = st.selectbox("승리 팀", [selected_game["team_a"], selected_game["team_b"]], index=None,
                                format_func=team_names.get, placeholder="실제 경기의 승리팀을 선택하세요", key=f"events_winner_{selected_game_id}")
                            submit_result = st.form_submit_button("경기 결과 저장", type="primary", icon=":material/check:", disabled=stale_result)
                        if submit_result and not stale_result:
                            if winner is None:
                                st.error("승리팀을 선택해 주세요.")
                            else:
                                def save_result():
                                    competition.record_result(token, event_id, selected_game_id, winner, expected_roster_token=result_token)
                                    st.session_state.pop(result_base_key, None)
                                perform(save_result, "경기 결과를 저장했습니다.")
                    elif not editable:
                        st.caption(f"결과 입력은 이 {kind_label}의 진행자 또는 관리자가 할 수 있습니다.")

                    if editable and completed:
                        all_finished = all(game["status"] != "PENDING" for game in event["games"])
                        st.divider()
                        st.subheader(f"{kind_label} 종료")
                        st.caption("대진 결과로 우승팀을 결정합니다. 종료하면 결과와 우승 보상이 함께 확정됩니다." if kind == "AUCTION" else "대진 결과로 우승팀을 확정합니다. 전력점수는 각 경기 결과 저장 시 반영됩니다.")
                        if st.button(f"우승 확정하고 {kind_label} 종료", type="primary", icon=":material/emoji_events:", disabled=not all_finished, key=f"events_finish_{event_id}"):
                            perform(lambda: competition.finalize_event(token, event_id), "경매 결과와 우승 보상을 확정했습니다." if kind == "AUCTION" else "일반내전 결과와 우승팀을 확정했습니다.")
                        if not all_finished:
                            st.caption("남은 경기와 동률 추가 경기를 먼저 완료해 주세요.")

                if actor and actor["role"] == "admin" and completed:
                    with st.expander("경기 결과 정정"):
                        if event["status"] == "COMPLETED":
                            st.caption(f"종료된 {kind_label}의 결과는 확정되어 있습니다. 정정이 필요한 경우 운영진의 분쟁 처리가 필요합니다.")
                        elif active:
                            st.caption("정정할 승리팀과 사유를 입력한 뒤 후속 경기의 영향을 확인합니다." if event["kind"] == "AUCTION" else "정정 사유가 기록됩니다. 이미 완료된 후속 경기가 있으면 자동 정정이 차단됩니다.")
                            correction_map = {game["id"]: game for game in completed}
                            correction_id = st.selectbox("정정할 경기", list(correction_map), format_func=lambda game_id: game_label(correction_map[game_id]), key=f"events_correction_game_{event_id}")
                            correction_game = correction_map[correction_id]
                            correction_base_key, correction_token, stale_correction = result_context(event, correction_id, actor, "correction")
                            with st.form(f"events_correction_form_{event_id}_{correction_id}"):
                                corrected_winner = st.selectbox("정정 후 승리 팀", [correction_game["team_a"], correction_game["team_b"]],
                                    index=None, format_func=team_names.get, placeholder="정정할 승리팀을 선택하세요")
                                correction_reason = st.text_input("결과 정정 사유", max_chars=1000)
                                submit_correction = st.form_submit_button("정정 영향 미리보기" if event["kind"] == "AUCTION" else "결과 정정 저장", disabled=stale_correction)
                            if submit_correction and not stale_correction:
                                if corrected_winner is None or not correction_reason.strip():
                                    st.error("정정할 승리팀과 사유를 모두 입력해 주세요.")
                                elif corrected_winner == correction_game["winner_team_id"]:
                                    st.info("현재 기록된 승리팀과 같습니다.")
                                elif event["kind"] == "AUCTION":
                                    try:
                                        st.session_state.events_revision_preview = ResultRevisionService(core, competition).preview(token, event_id, correction_id, corrected_winner, correction_reason)
                                        st.rerun()
                                    except (ValueError, PermissionError) as error:
                                        st.error(str(error))
                                else:
                                    def save_correction():
                                        competition.record_result(token, event_id, correction_id, corrected_winner,
                                            reason=correction_reason, expected_roster_token=correction_token)
                                        st.session_state.pop(correction_base_key, None)
                                    perform(save_correction, "경기 결과와 후속 대진을 정정했습니다.")

                if event["games"]:
                    st.download_button(f"{kind_label} 결과 CSV 다운로드", core.csv_bytes(fixture_rows(event["games"])),
                        file_name=f"rolymoly_event_{event_id}_results.csv", mime="text/csv", icon=":material/download:")

            if editable and event["status"] not in ("COMPLETED", "CANCELLED"):
                with st.expander(f"{kind_label} 취소"):
                    if completed:
                        if event["status"] == "PLAYING":
                            st.caption("이미 확정된 경기와 점수는 유지하며 남은 경기를 중단합니다." if kind == "NORMAL" else "중단 종료하면 우승팀을 확정하거나 우승 보상을 지급하지 않습니다. 기존 경기 결과·낙찰·참가 명단은 보존합니다.")
                            if actor and actor["role"] == "admin":
                                with st.form(f"events_close_unfinished_form_{event_id}"):
                                    close_reason = st.text_input("중단 종료 사유", max_chars=1000)
                                    close_submit = st.form_submit_button("기록을 보존하고 중단 종료")
                                if close_submit:
                                    preserved_result = "이미 확정된 경기와 점수는 유지했습니다." if kind == "NORMAL" else "우승 보상은 지급하지 않았습니다."
                                    perform(lambda: competition.close_unfinished(token, event_id, close_reason), f"기존 기록을 보존하고 {kind_label} 중단 종료를 완료했습니다. {preserved_result}")
                            else:
                                st.caption("중단 종료가 필요하면 관리자에게 요청해 주세요.")
                        else:
                            st.caption("이미 실제 경기 결과가 있어 일괄 취소할 수 없습니다. 기록을 보존한 상태에서 운영진의 분쟁 처리가 필요합니다.")
                    else:
                        st.caption(f"경기 결과가 없는 {kind_label}만 취소할 수 있습니다. {kind_label} 명단과 취소 사유는 보존됩니다.")
                        with st.form(f"events_cancel_form_{event_id}"):
                            cancel_reason = st.text_input(f"{kind_label} 취소 사유", max_chars=1000)
                            cancel_submit = st.form_submit_button(f"{kind_label} 취소")
                        if cancel_submit:
                            perform(lambda: competition.cancel_event(token, event_id, cancel_reason), f"{kind_label} 취소를 완료했습니다.")

    with team_tab:
        if event is not None:
            team_cards(event)
            st.caption("팀 전력은 참가·편성 확정 당시 점수입니다. 이후 개인 점수가 달라져도 당시 기록은 유지됩니다.")
            if event["pool"]:
                st.subheader("미배정 선수")
                st.dataframe([{"선수": player["riot_id"], "포지션": ROLE_NAMES[player["role"]], "전력": player["score"], **profile_snapshot_columns(player),
                    "상태": "유찰" if player["state"] == "UNSOLD" else "대기"} for player in event["pool"]], hide_index=True)
            if editable and event["kind"] == "NORMAL" and event["status"] == "READY":
                with st.expander("카카오톡 확정 명단 수정·재편성"):
                    from roly.roster_ui import roster_editor
                    st.caption("첫 경기 결과를 저장하기 전까지만 가능합니다. 정원을 유지하고 변경하면 팀과 대진을 함께 다시 만듭니다.")
                    base_key = f"events_roster_base_{event_id}_{actor['id']}"
                    st.session_state.setdefault(base_key, {"token": event["roster_token"], "players": event["players"]})
                    base = st.session_state[base_key]
                    stale = base["token"] != event["roster_token"]
                    if stale:
                        st.warning("다른 화면에서 명단·팀·대진이 변경되었습니다. 최신 명단을 불러와 다시 확인해 주세요.")
                        if st.button("최신 명단 불러오기", key=f"events_roster_reload_{event_id}"):
                            st.session_state.pop(base_key, None)
                            st.rerun()
                    replacement = roster_editor(core.list_members(), key=f"events_roster_{event_id}_{actor['id']}",
                        team_count=len(event["teams"]), saved=base["players"],
                        matching_members=core.list_members(include_pending=True), revision=base["token"])
                    replacement_reason = st.text_input("명단 변경 사유", key=f"events_roster_reason_{event_id}", max_chars=1000)
                    if st.button("명단 확정하고 팀·대진 다시 편성", key=f"events_roster_save_{event_id}", disabled=stale) and not stale:
                        def replace_roster():
                            competition.replace_normal_roster(token, event_id, replacement, replacement_reason,
                                expected_roster_token=base["token"])
                            st.session_state.pop(base_key, None)
                        perform(replace_roster, "변경된 명단으로 팀과 대진을 다시 편성했습니다.")
                with st.expander("같은 포지션 선수 교환"):
                    st.caption("첫 경기 결과를 저장하기 전, 다른 팀의 같은 포지션 선수끼리 교환할 수 있습니다.")
                    database_key = sha256(str(core.db_path).encode()).hexdigest()[:12]
                    swap_key = f"events_swap_base_{database_key}_{event_id}_{actor['id']}"
                    st.session_state.setdefault(swap_key, {"token": event["roster_token"],
                        "players": event["players"], "team_names": team_names, "request_id": str(uuid4())})
                    swap_review = st.session_state[swap_key]
                    swap_stale = swap_review["token"] != event["roster_token"]
                    retry_body = swap_review.get("retry_body")
                    if retry_body:
                        st.warning("교환 응답을 받지 못했습니다. 처음 제출한 교환의 처리 결과를 다시 확인해 주세요.")
                        if st.button("교환 처리 결과 다시 확인", key=f"events_swap_retry_{event_id}"):
                            perform(lambda: save_reviewed_swap(competition, token, event_id, swap_review,
                                retry_body, swap_key), "선수 교환 처리 결과를 확인했습니다.")
                    elif swap_stale:
                        st.warning("명단이나 진행 상태가 변경되었습니다. 최신 교환 명단을 확인한 뒤 다시 선택해 주세요.")
                    if swap_stale or retry_body:
                        if st.button("최신 교환 명단 불러오기", key=f"events_swap_reload_{event_id}"):
                            st.session_state.pop(swap_key, None)
                            st.rerun()
                    swap_blocked = swap_stale or bool(retry_body)
                    player_map = {player["member_id"]: player for player in swap_review["players"]}
                    swap_team_names = swap_review["team_names"]
                    first_id = st.selectbox("교환할 선수", list(player_map), format_func=lambda member_id: f"{player_map[member_id]['riot_id']} · {swap_team_names[player_map[member_id]['team_id']]}", key=f"events_swap_first_{event_id}", disabled=swap_blocked)
                    first_player = player_map[first_id]
                    candidates = [player["member_id"] for player in swap_review["players"] if player["role"] == first_player["role"] and player["team_id"] != first_player["team_id"]]
                    second_id = st.selectbox("맞교환할 선수", candidates, format_func=lambda member_id: f"{player_map[member_id]['riot_id']} · {swap_team_names[player_map[member_id]['team_id']]}", key=f"events_swap_second_{event_id}_{first_id}", disabled=swap_blocked)
                    swap_reason = st.text_input("교환 사유", value="팀 균형 조정", max_chars=1000, key=f"events_swap_reason_{event_id}", disabled=swap_blocked)
                    if st.button("선수 교환", key=f"events_swap_{event_id}", disabled=swap_blocked) and not swap_blocked:
                        if not swap_reason.strip():
                            st.error("교환 사유를 입력해 주세요.")
                        else:
                            perform(lambda: save_reviewed_swap(competition, token, event_id, swap_review,
                                {"first_member_id": first_id, "second_member_id": second_id, "reason": swap_reason}, swap_key),
                                "같은 포지션 선수를 교환했습니다.")
            roster_rows = [{"팀": team_names.get(player["team_id"], "미배정"), "Riot ID": player["riot_id"],
                "포지션": ROLE_NAMES[player["role"]], **profile_snapshot_columns(player), "편성 당시 전력": player["score"], "낙찰가": player["price"]}
                for player in event["players"]]
            st.download_button(f"{kind_label} 명단 CSV 다운로드", core.csv_bytes(roster_rows), file_name=f"rolymoly_event_{event_id}_roster.csv", mime="text/csv", icon=":material/download:")
            with st.expander(f"{kind_label} 변경 기록"):
                actions = {"CREATE": f"{kind_label} 개설", "UNSOLD": "유찰", "BID": "낙찰", "MOVE": "재배정", "ROLE": "포지션 변경",
                    "SWAP": "선수 교환", "AUCTION_FINALIZE": "경매 확정", "TIEBREAK": "추가 경기", "RESULT": "결과 저장", "RESULT_CASCADE": "후속 경기 포함 결과 정정", "FINALIZE": f"{kind_label} 종료", "CANCEL": f"{kind_label} 취소", "CLOSE_UNFINISHED": "기록 보존·중단 종료", "PREPARATION_REOPENED": "참가 명단 다시 준비",
                    "RESET": "경매 초기화", "SALE_CORRECTION": "낙찰 정정", "NORMAL_ROSTER_REPLACE": "일반내전 명단 재편성",
                    "CREATE_DRAFT": f"{kind_label} 준비 개설", "DETAILS": "내전 정보 수정", "STAGE": "진행 단계 변경",
                    "PARTICIPANTS": "참가 명단 변경", "AUTO_BALANCE": "전력 기준 팀 편성", "ASSIGN": "선수 배정",
                    "UNASSIGN": "선수 배정 해제", "PARTICIPANT_WARNING": "참가자 경고"}
                st.dataframe([{"시각": korean_time(row["created_at"]), "작업": actions.get(row["action"], row["action"]), "내용": audit_detail(row)}
                    for row in event["audit"]], hide_index=True)

    with history_tab:
        st.subheader("전체 경기 이력")
        st.caption(f"모든 {kind_label}의 확정·무효 경기 기록입니다.")
        history_games = core.list_games(kind=kind)
        if not history_games:
            st.info("저장된 경기 결과가 없습니다.")
        else:
            event_map = {str(item["id"]): item for item in events}
            history_team_names = competition.game_labels()
            history_rows = [{"경기 번호": game["id"], "경기 시각": korean_time(game["played_at"]),
                "종류": KINDS[game["kind"]], f"{kind_label}": event_map.get(str(game["tournament_id"]), {}).get("title", "개별 경기"),
                "승리팀": history_team_names.get(game["id"], {}).get("winner_name") or f"{game['winner']}팀", "상태": "확정" if game["status"] == "CONFIRMED" else "무효",
                "진행자": game["actor_name"]} for game in history_games]
            st.dataframe(history_rows, hide_index=True)
            st.download_button(f"{kind_label} 경기 이력 CSV 다운로드", core.csv_bytes(history_rows), file_name=f"rolymoly_{kind.lower()}_game_history.csv", mime="text/csv", icon=":material/download:")
            history_map = {game["id"]: game for game in history_games}
            history_id = st.selectbox("상세 기록을 볼 경기", list(history_map), index=None,
                format_func=lambda game_id: f"{korean_time(history_map[game_id]['played_at'])} · {history_map[game_id]['notes'] or '개별 경기'}",
                placeholder="경기를 선택하면 선수별 점수와 정정 내역이 표시됩니다.", key=f"events_history_detail_{kind}", persist_state="session")
            if history_id is not None:
                detail = core.get_game(history_id)
                detail_names = history_team_names.get(history_id, {})
                team_labels = {"A": detail_names.get("team_a_name") or "A팀", "B": detail_names.get("team_b_name") or "B팀"}
                st.subheader("선수별 경기 기록")
                score_rows = [{"팀": team_labels[player["team"]], "선수": player["riot_id"], "포지션": ROLE_NAMES[player["role"]], **profile_snapshot_columns(player),
                    "경기 직전 점수": player["score_before"], "최종 반영 증감": player["delta"]} for player in detail["players"]]
                st.dataframe(score_rows, hide_index=True)
                st.caption("경기 직전 점수는 당시 기록이며, 최종 반영 증감에는 결과 정정이 반영됩니다.")
                st.download_button("선수별 경기 기록 CSV 다운로드", core.csv_bytes(score_rows), file_name=f"rolymoly_game_{history_id}_players.csv", mime="text/csv", icon=":material/download:")
                with st.expander("결과 확정·정정 이력", expanded=True):
                    st.dataframe([{"변경 차수": row["revision"], "승리팀": team_labels.get(row["winner"], ""), "상태": "확정" if row["status"] == "CONFIRMED" else "무효",
                        "사유": row["reason"], "시각": korean_time(row["created_at"])} for row in detail["revisions"]], hide_index=True)
                if detail["tournament_id"] is not None:
                    st.caption(f"이 경기의 결과 정정은 해당 {kind_label} 선택 후 ‘{kind_label} 결과’에서 진행합니다.")

    if preview := st.session_state.get("events_revision_preview"):
        if actor and actor["role"] == "admin" and event is not None and preview["event_id"] == event["id"]:
            revision_dialog(preview)
        else:
            clear_revision_preview()
