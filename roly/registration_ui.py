"""Compact native dialogs for auction creation and participant registration."""
from datetime import datetime, time
from hashlib import sha256
import sqlite3
from uuid import uuid4

import streamlit as st

from roly.roster_ui import roster_editor
from roly.tournament import allowed_formats, allowed_team_counts
from roly.tournament_ui import KST, MATCH_FORMATS, close_preparation_dialog, perform_in_dialog
from roly.ui import can_create, can_edit, perform


def creation_draft(core, actor, kind):
    name = "normal_creation_draft" if kind == "NORMAL" else "t_creation_draft"
    context = (sha256(str(core.db_path).encode()).hexdigest(), actor["id"])
    draft = st.session_state.get(name)
    if not draft or draft.get("context") != context:
        draft = {"context": context, "request_key": str(uuid4())}
        st.session_state[name] = draft
    return name, draft


def rotate_creation(name):
    previous = st.session_state[name]
    request_key = previous["request_key"]
    for key in list(st.session_state):
        if request_key in key:
            del st.session_state[key]
    st.session_state[name] = {"context": previous["context"], "request_key": str(uuid4())}


def confirmation_key(core, actor, event_id):
    database = sha256(str(core.db_path).encode()).hexdigest()[:16]
    return f"t_confirm_roster_{database}_{actor['id']}_{event_id}"


def confirm_reviewed_participants(service, token, event_id, expected, base_key):
    # Callback arguments belong to the previously rendered screen. Do not read
    # a freshly loaded roster token while handling an older browser request.
    try:
        service.confirm_participants(token, event_id, expected_roster_token=expected)
    except sqlite3.OperationalError:
        st.session_state[f"{base_key}_error"] = "저장소 응답을 확인하지 못했습니다. 현재 진행 상태를 확인한 뒤 다시 시도해 주세요."
    except (ValueError, PermissionError, sqlite3.IntegrityError) as error:
        st.session_state[f"{base_key}_error"] = str(error)
    else:
        st.session_state.pop(base_key, None)
        st.session_state.pop(f"{base_key}_error", None)
        st.session_state.flash = "참가자를 확정했습니다. 팀장을 지정해 주세요."


def close_creation():
    st.session_state.t_create_open = False


@st.dialog("경매 내전 만들기", width="medium", on_dismiss=close_creation)
def creation_dialog(service, token):
    actor = service.core.session(token)
    if "token" in st.session_state and token != st.session_state.get("token"):
        st.warning("로그인 계정이 변경되었습니다. 대화상자를 닫고 다시 열어 주세요.")
        return
    if not can_create(actor):
        st.warning("로그인이 만료되었습니다. 다시 로그인해 주세요." if not actor else "승인된 회원만 경매를 만들 수 있습니다.")
        return
    draft_key, draft = creation_draft(service.core, actor, "AUCTION")
    request_key = draft["request_key"]
    team_count = st.segmented_control("참가 인원", list(allowed_team_counts("AUCTION")),
        default=4, format_func=lambda count: f"{count * 5}명 · {count}팀", key=f"t_create_count_AUCTION_{request_key}") or 4
    with st.form(f"t_create_form_{request_key}"):
        title = st.text_input("경매 이름", max_chars=100, placeholder="예: 9월 둘째 주 주말 경매", key=f"t_create_title_{request_key}")
        date_column, time_column = st.columns(2)
        start_date = date_column.date_input("진행 날짜", value=datetime.now(KST).date(), key=f"t_create_date_{request_key}")
        start_time = time_column.time_input("시작 시각", value=time(20, 0), key=f"t_create_time_{request_key}")
        match_format = st.selectbox("경기 방식", list(allowed_formats(team_count, "AUCTION")),
            format_func=MATCH_FORMATS.get, key=f"t_create_format_AUCTION_{team_count}_{request_key}")
        description = st.text_area("참가 안내 (선택)", max_chars=2000, height=90,
            placeholder="참가자에게 필요한 안내를 적어주세요", key=f"t_create_description_{request_key}")
        st.caption(f"팀당 5명 · 총 {team_count * 5}명 · 한국 시간 기준")
        submit = st.form_submit_button("경매 내전 만들기", type="primary", width="stretch")
    if submit:
        def create():
            body = draft.get("retry_body") or {
                "title": title, "starts_at": datetime.combine(start_date, start_time, tzinfo=KST).isoformat(),
                "description": description, "build_mode": "AUCTION", "team_count": team_count, "format_name": match_format}
            try:
                event_id = service.create(token, **body, request_key=request_key)
            except sqlite3.OperationalError:
                draft["retry_body"] = body
                raise
            st.session_state.focus_event = event_id
            st.session_state.auction_event = event_id
            rotate_creation(draft_key)
            close_creation()
        perform(create, "경매를 만들었습니다. 참가 회원을 등록해 주세요.")
    if draft.get("retry_body"):
        st.warning("저장 응답을 받지 못해 첫 제출 내용으로 재시도합니다. 내용을 바꾸려면 경매 목록에서 생성 여부를 먼저 확인한 뒤 새 작성을 시작해 주세요.")
    st.caption("응답이 끊겼다면 입력을 그대로 두고 다시 생성해 주세요. 같은 요청은 한 번만 저장됩니다.")
    if st.button("입력을 비우고 새 경매 작성", key=f"t_create_reset_{request_key}"):
        rotate_creation(draft_key)
        st.rerun()


@st.dialog("경매 참가자 등록", width="large", on_dismiss=close_preparation_dialog)
def participants_dialog(service, token, event_id):
    actor = service.core.session(token)
    if "token" in st.session_state and token != st.session_state.get("token"):
        st.warning("로그인 계정이 변경되었습니다. 대화상자를 닫고 다시 열어 주세요.")
        return
    if not can_create(actor):
        st.warning("로그인이 만료되었습니다. 다시 로그인해 주세요." if not actor else "승인된 회원만 참가 명단을 관리할 수 있습니다.")
        return
    event = service.get_event(event_id)
    if not can_edit(actor, event):
        st.warning("이 경매의 주최자 또는 관리자만 참가 명단을 관리할 수 있습니다.")
        return
    base_key = f"t_roster_base_{event_id}_{actor['id'] if actor else 'guest'}"
    if base_key not in st.session_state:
        st.session_state[base_key] = {"token": event["roster_token"], "players": event["players"]}
    base = st.session_state[base_key]
    stale = base["token"] != event["roster_token"]
    st.write(event["title"])
    st.caption("카카오톡에서 받은 참가 명단을 등록하세요. 팀별 최종 포지션은 낙찰 후 확정합니다.")
    if stale:
        st.warning("다른 화면에서 명단이나 진행 상태를 변경했습니다. 최신 명단을 불러온 뒤 다시 확인해 주세요.")
        if st.button("최신 명단 불러오기", key=f"t_roster_reload_{event_id}"):
            base = {"token": event["roster_token"], "players": event["players"]}
            st.session_state[base_key] = base
            stale = False
    assignments = roster_editor(service.core.list_members(), key=f"t_roster_{event_id}",
        team_count=event["team_count"], saved=base["players"], strict_roles=False,
        matching_members=service.core.list_members(include_pending=True), revision=(actor['id'] if actor else None, base["token"]))
    if st.button("참가 명단 저장", type="primary", disabled=stale, key=f"t_save_roster_{event_id}") and not stale:
        def save():
            service.set_participants(token, event_id, assignments, expected_roster_token=base["token"])
            st.session_state.pop(base_key, None)
            st.session_state.pop(confirmation_key(service.core, actor, event_id), None)
        perform_in_dialog(save, "참가 명단을 저장했습니다.")
