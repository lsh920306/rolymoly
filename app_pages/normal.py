import streamlit as st
import sqlite3
from roly.ui import context, heading, require_member, perform, ROLE_NAMES, team_cards, FORMATS
from roly.roster_ui import roster_editor
from roly.registration_ui import creation_draft, rotate_creation

core, competition, token, actor = context()
heading("일반 내전 만들기", "포지션별 참가자를 선택하고 전력점수를 기준으로 팀을 편성합니다.")
require_member(actor)
st.caption("카카오톡에서 신청받은 명단을 확인하고 참가자를 등록해 주세요.")
matching_members = core.list_members(include_pending=True)
members = [member for member in matching_members if member["status"] == "APPROVED"]
from roly.riot_ui import member_refresh_picker
member_refresh_picker(core, token, members, key="normal_members")
if len(members) < 10:
    st.info(f"일반내전은 승인된 회원 10명부터 시작할 수 있습니다. 현재 {len(members)}명입니다.")
    if actor["role"] == "admin":
        st.page_link("app_pages/admin.py", label="가입 승인 관리", icon=":material/person_add:")
    st.stop()
draft_key, draft = creation_draft(core, actor, "NORMAL")
request_key = draft["request_key"]
count = st.segmented_control("참가 인원", [10, 20], default=10, format_func=lambda n: f"{n}명 · {n // 5}팀", key=f"normal_count_{request_key}")
count = count or 10
with st.container(width=900):
    st.subheader("내전 정보")
    name_column, format_column = st.columns([2, 1])
    title = name_column.text_input("내전 이름", value=f"오늘의 {count}인 내전", max_chars=80,
        placeholder="예: 월요일 저녁 내전", key=f"normal_title_{count}_{request_key}")
    format_name = format_column.selectbox("진행 방식", ["SINGLE"] if count == 10 else ["LEAGUE", "TOURNAMENT"],
        format_func=FORMATS.get, key=f"normal_format_{count}_{request_key}")
    st.divider()
    st.subheader("참가자 등록")
    assignments = roster_editor(members, key=f"normal_roster_{count}_{request_key}", team_count=count // 5,
                                matching_members=matching_members)
    balanced = st.checkbox("전력점수로 팀 균형 맞추기", value=True,
        help="해제하면 각 포지션의 선택 순서대로 팀을 구성합니다.", key=f"normal_balance_{count}_{request_key}")
    if st.button("일반 내전 만들기", type="primary", icon=":material/groups:", key=f"normal_create_submit_{request_key}"):
        def create():
            body = draft.get("retry_body") or {"assignments": assignments, "title": title,
                "balanced": balanced, "format_name": format_name}
            with st.spinner("가능한 포지션 조합을 비교하고 있습니다…"):
                try:
                    event_id = competition.create_normal(token, **body, request_key=request_key)
                except sqlite3.OperationalError:
                    draft["retry_body"] = body
                    raise
            st.session_state.focus_event = event_id
            rotate_creation(draft_key)
        perform(create, "팀 편성을 마쳤습니다. 아래에서 팀을 확인해 주세요.")
    if draft.get("retry_body"):
        st.warning("저장 응답을 받지 못해 첫 제출 명단으로 재시도합니다. 내용을 바꾸려면 기록에서 생성 여부를 먼저 확인한 뒤 새 작성을 시작해 주세요.")
    st.caption("응답이 끊겼다면 입력을 그대로 두고 다시 생성해 주세요. 같은 요청은 한 번만 저장됩니다.")
    if st.button("입력을 비우고 새 내전 작성", key=f"normal_create_reset_{request_key}"):
        rotate_creation(draft_key)
        st.rerun()
event_id = st.session_state.get("focus_event")
if event_id:
    event = competition.get_event(event_id)
    if event["kind"] == "NORMAL":
        st.subheader(event["title"])
        scores = [t["total_score"] for t in event["teams"]]
        st.caption(f"가장 높은 팀과 낮은 팀의 전력 차이 {max(scores) - min(scores):g} P · {FORMATS[event['format']]}")
        team_cards(event)
        st.page_link("app_pages/events.py", label="대진표 확인하고 결과 입력", icon=":material/arrow_forward:", icon_position="right")
