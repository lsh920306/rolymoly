"""Presentation helpers for the tournament preparation page."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape

import streamlit as st

from roly.ui import ROLE_NAMES, perform
from roly.member_records import saved_tier


KST = timezone(timedelta(hours=9))
BUILD_MODES = {"BALANCE": "자동 밸런싱", "MANUAL": "수동 배정", "AUCTION": "경매"}
MATCH_FORMATS = {"SINGLE": "단판", "LEAGUE": "풀리그", "TOURNAMENT": "토너먼트", "RANKING": "순위 결정전", "GROUP_STAGE": "조별리그 + 결승"}
PREPARATION_STATUS = {
    "DRAFT": "경매 준비", "RECRUITING": "참가자 선택", "CAPTAIN_SELECTION": "팀장 지정",
    "TEAM_BUILDING": "팀 구성", "AUCTION_READY": "경매 준비", "AUCTION": "경매 진행",
    "AUCTION_RUNNING": "경매 진행", "AUCTION_COMPLETE": "경매 완료", "BRACKET_SETUP": "대진표 확인",
    "READY": "경기 준비", "PLAYING": "경기 진행", "IN_PROGRESS": "경기 진행",
    "COMPLETED": "종료", "FINISHED": "종료", "CANCELLED": "취소",
}
PREPARATION_STATES = {"DRAFT", "RECRUITING", "CAPTAIN_SELECTION", "TEAM_BUILDING", "AUCTION_READY", "BRACKET_SETUP"}
AUDIT_NAMES = {
    "CREATE": "경매 생성", "CREATE_DRAFT": "경매 생성", "DRAFT_CREATE": "경매 생성",
    "STAGE": "진행 단계 변경", "DETAILS": "경매 정보 변경",
    "UPDATE_DETAILS": "경매 정보 변경", "OPEN_RECRUITMENT": "참가자 선택 시작",
    "RECRUITMENT": "참가자 선택 시작", "PARTICIPANTS": "참가 명단 저장",
    "SET_PARTICIPANTS": "참가 명단 저장", "PARTICIPANTS_CONFIRM": "참가자 확정",
    "CONFIRM_PARTICIPANTS": "참가자 확정", "CAPTAINS": "팀장 지정", "SET_CAPTAINS": "팀장 지정",
    "PREPARATION_REOPENED": "참가 명단 다시 준비",
    "AUTO_BALANCE": "자동 팀 배정", "BALANCE": "자동 팀 배정", "ASSIGN": "선수 배정",
    "UNASSIGN": "선수 배정 해제", "ROLE": "포지션 변경", "AUCTION_READY": "경매 준비 완료",
    "BRACKET_BUILD": "대진표 생성", "BUILD_BRACKET": "대진표 생성",
    "BRACKET_CONFIRM": "대진표 확정", "CONFIRM_BRACKET": "대진표 확정",
    "ATTENDANCE_REQUEST": "출석 확인 요청", "ATTENDANCE_CONFIRM": "출석 확인",
    "PARTICIPANT_WARNING": "참가자 경고", "WARN": "참가자 경고",
    "PARTICIPANT_EXCLUDE": "참가 제외", "EXCLUDE": "참가 제외", "RESULT": "경기 결과 저장",
    "FINALIZE": "경매 종료", "CANCEL": "경매 취소",
}


def local_datetime(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (result.replace(tzinfo=KST) if result.tzinfo is None else result).astimezone(KST)
    except (TypeError, ValueError):
        return None


def schedule_label(value):
    result = local_datetime(value)
    return result.strftime("%Y.%m.%d %H:%M") if result else "일정 미지정"


def progress_steps(status):
    labels = ["참가자", "팀장", "팀 구성·경매", "대진표", "경기 진행", "종료"]
    current = {
        "DRAFT": 0, "RECRUITING": 0, "CAPTAIN_SELECTION": 1, "TEAM_BUILDING": 2,
        "AUCTION_READY": 2, "AUCTION": 2, "AUCTION_RUNNING": 2, "AUCTION_COMPLETE": 3,
        "BRACKET_SETUP": 3, "READY": 4, "PLAYING": 4, "IN_PROGRESS": 4,
        "COMPLETED": 5, "FINISHED": 5,
    }.get(status, 0)
    if status == "CANCELLED":
        st.caption("취소된 경매입니다. 참가 명단과 운영 기록을 조회할 수 있습니다.")
        return
    st.progress(current / 5, text=f"{current + 1}/6 · {PREPARATION_STATUS.get(status, status)}")
    st.markdown("  ›  ".join(f"**{index + 1}. {label}**" if index == current else f"{index + 1}. {label}" for index, label in enumerate(labels)))


def profile_snapshot_columns(player):
    """Only saved metadata: an unknown historical tier stays unknown."""
    return {"당시 클랜 티어": saved_tier(player.get("clan_tier_snapshot")),
            "당시 현재 티어": saved_tier(player.get("current_tier_snapshot"), player.get("current_tier_lp_snapshot"))}


def participant_rows(players, team_names=None):
    names = team_names or {}
    return [
        {"회원": player["riot_id"], "포지션": ROLE_NAMES.get(player["role"], player["role"]),
         "전력점수": player["score"], **profile_snapshot_columns(player), "팀": names.get(player.get("team_id"), "미배정")}
        for player in players
    ]


def fixture_rows(games):
    return [
        {"라운드": "3·4위전" if game.get("stage") == "THIRD_PLACE" else f"{game['group_key']}조 · {game['round']}R" if game.get("group_key") else f"{game['round']}R",
         "팀 A": game.get("team_a_name", "미정"),
         "팀 B": "부전승" if game["status"] == "BYE" else game.get("team_b_name", "미정"),
         "상태": {"PENDING": "대기", "COMPLETED": "완료", "BYE": "부전승"}.get(game["status"], game["status"])}
        for game in games
    ]


def preparation_team_cards(event, core=None):
    from roly.team_cards import render_preparation_teams
    render_preparation_teams(event, core)


def close_preparation_dialog():
    st.session_state.pop("t_preparation_dialog", None)
    for key in list(st.session_state):
        if key.startswith(("t_captain_draft_", "t_captain_query_")):
            st.session_state.pop(key, None)


def perform_in_dialog(action, message):
    def apply():
        action()
        close_preparation_dialog()
    perform(apply, message)


def _toggle_captain(draft_key, member_id):
    """Edit only the unsaved draft; the service authorizes the eventual save."""
    draft = st.session_state.get(draft_key)
    if not isinstance(draft, dict) or member_id not in draft["candidates"]:
        return
    selected = draft["selected"]
    if member_id in selected:
        selected.remove(member_id)
    elif len(selected) < draft["team_count"]:
        selected.append(member_id)


def _captain_card(player, profile, order):
    """Presentation from persisted public fields only; no HTTP or credentials."""
    nickname, _, tag = player["riot_id"].partition("#")
    icon = profile.get("profile_icon_url") or ""
    avatar = (f'<img src="{escape(icon, quote=True)}" alt="" loading="lazy">' if icon
              else f'<span>{escape(nickname[:1])}</span>')
    rank = saved_tier(profile.get("current_tier") or player.get("current_tier_snapshot"),
                      profile.get("lp") if profile else player.get("current_tier_lp_snapshot"))
    main = ROLE_NAMES.get(player.get("main_role_snapshot") or player["role"], "미정")
    sub = ROLE_NAMES.get(player.get("sub_role_snapshot"), "미정")
    state = "selected" if order else "available"
    label = f"{order}팀 선택" if order else "선택"
    return (f'<div class="captain-choice {state}"><div class="captain-choice-avatar">{avatar}</div>'
            f'<div class="captain-choice-info"><strong>{escape(nickname)}</strong>'
            f'<div class="captain-choice-meta"><span>#{escape(tag)}</span>'
            f'<span class="captain-choice-rank">{escape(rank)}</span></div>'
            f'<div class="captain-choice-roles"><span>주 {escape(main)}</span><span>부 {escape(sub)}</span></div></div>'
            f'<span class="captain-choice-badge">{label}</span></div>')


@st.dialog("팀장 지정하기", width="medium", on_dismiss=close_preparation_dialog)
def captain_dialog(service, token, event_id):
    from roly.ui import can_edit
    actor = service.core.session(token)
    if ("token" in st.session_state and token != st.session_state.get("token")) or not actor:
        close_preparation_dialog()
        st.warning("로그인 상태가 변경되었습니다. 창을 닫고 다시 열어 주세요.")
        return
    event = service.get_event(event_id)
    if not can_edit(actor, event):
        close_preparation_dialog()
        st.warning("이 경매의 주최자 또는 관리자만 팀장을 지정할 수 있습니다.")
        return
    if event["status"] not in ("CAPTAIN_SELECTION", "TEAM_BUILDING"):
        close_preparation_dialog()
        st.warning("준비 상태가 변경되었습니다. 현재 단계에서는 팀장을 지정할 수 없습니다.")
        return
    player_map = {player["member_id"]: player for player in event["players"]
                  if player.get("participation_status", "SELECTED") == "SELECTED"}
    context = sha256(f"{service.core.db_path}:{actor['id']}:{token}".encode()).hexdigest()
    draft_key = f"t_captain_draft_{event_id}"
    draft = st.session_state.get(draft_key)
    if not isinstance(draft, dict) or draft.get("context") != context:
        draft = {"context": context, "candidates": tuple(player_map), "team_count": event["team_count"],
                 "selected": [team["captain_id"] for team in event["teams"] if team["captain_id"] in player_map]}
        st.session_state[draft_key] = draft
    draft["candidates"], draft["team_count"] = tuple(player_map), event["team_count"]
    draft["selected"] = list(dict.fromkeys(member for member in draft["selected"] if member in player_map))[:event["team_count"]]
    selected = draft["selected"]
    from roly.riot_profile import member_profiles
    with st.container(key="t_captain_picker", gap="small"):
        with st.container(horizontal=True, wrap=False, vertical_alignment="center", gap="small", key="t_captain_header"):
            st.html(f'<div class="captain-count">선택한 팀장 <strong>{len(selected)}/{event["team_count"]}</strong></div>')
            query = st.text_input("팀장 참가자 검색", placeholder="닉네임, 태그 검색", label_visibility="collapsed",
                                  icon=":material/search:", key=f"t_captain_query_{event_id}", width=210).strip().casefold()
        st.caption("선택한 순서대로 팀을 구성합니다.")
        with st.container(height=340, border=False, gap="xsmall", key="t_captain_list"):
            visible = [player for player in player_map.values() if query in player["riot_id"].casefold()]
            profiles = member_profiles(service.core, [player["member_id"] for player in visible])
            if not visible:
                st.caption("검색 결과가 없습니다. 선택한 팀장은 유지됩니다.")
            for player in visible:
                member_id = player["member_id"]
                order = selected.index(member_id) + 1 if member_id in selected else 0
                with st.container(key=f"t_captain_card_{event_id}_{member_id}", gap=None):
                    st.html(_captain_card(player, profiles.get(member_id) or {}, order))
                    st.button(f"{player['riot_id']} {'선택 해제' if order else '팀장 선택'}",
                              key=f"t_captain_toggle_{event_id}_{member_id}", width="stretch",
                              disabled=not order and len(selected) >= event["team_count"],
                              on_click=_toggle_captain, args=(draft_key, member_id))
        with st.container(horizontal=True, wrap=False, gap="xsmall", key="t_captain_actions"):
            if st.button("취소", key=f"t_captains_cancel_{event_id}", width="stretch"):
                close_preparation_dialog()
                st.rerun()
            if st.button("선택 완료", type="primary", disabled=len(selected) != event["team_count"],
                         key=f"t_captains_confirm_{event_id}", width="stretch"):
                perform_in_dialog(lambda: service.set_captains(token, event_id, selected), "팀장을 지정했습니다.")


@st.dialog("참가자 관리", width="medium", on_dismiss=close_preparation_dialog)
def participant_management_dialog(service, token, event_id):
    event = service.get_event(event_id)
    player_map = {player["member_id"]: player for player in event["players"]}
    if not player_map:
        st.caption("참가 명단을 먼저 저장해 주세요.")
        return
    st.dataframe(participant_rows(event["players"]), hide_index=True)
    member_id = st.selectbox(
        "관리할 참가자", list(player_map),
        format_func=lambda member_id: player_map[member_id]["riot_id"],
        key=f"t_management_member_{event_id}",
    )
    if event["status"] in PREPARATION_STATES:
        st.caption("경고는 운영 기록으로 남습니다. 참가 제외 시 준비 단계로 돌아가 명단을 다시 확정합니다.")
        with st.form(f"t_participant_action_{event_id}_{member_id}"):
            action = st.selectbox("참가자 조치", ["경고 기록", "참가 제외"])
            reason = st.text_input("조치 사유", max_chars=1000)
            apply_action = st.form_submit_button("참가자 조치 적용")
        if apply_action:
            if action == "경고 기록":
                perform_in_dialog(lambda: service.warn_participant(token, event_id, member_id, reason), "경고 사유를 기록했습니다.")
            elif action == "참가 제외":
                perform_in_dialog(lambda: service.exclude_participant(token, event_id, member_id, reason), "경매 참가 명단에서 제외했습니다.")


@st.dialog("대진표 확정", width="medium", on_dismiss=close_preparation_dialog)
def bracket_dialog(service, token, event_id):
    event = service.get_event(event_id)
    st.write(f"{event['title']} · {MATCH_FORMATS.get(event['format'], event['format'])}")
    st.dataframe(fixture_rows(event["games"]), hide_index=True)
    st.caption("참가 팀과 대진을 확인한 뒤 확정하면 경기 결과를 입력할 수 있습니다.")
    if st.button("대진표 확정하고 경기 준비", type="primary", key=f"t_bracket_confirm_{event_id}"):
        perform_in_dialog(lambda: service.confirm_bracket(token, event_id), "대진표를 확정했습니다.")
