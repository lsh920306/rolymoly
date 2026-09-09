"""Prepare and run an auction competition in one persistent workflow."""
from datetime import datetime

import streamlit as st

from roly.auction_ui import auction_settings_dialog, live_service, render_auction_setup, render_live_auction, sale_correction_control, auction_reset_control, auction_reset_dialog
from roly.auction_components import render_team
from roly.tournament import TournamentService, allowed_formats
from roly.registration_ui import creation_dialog, participants_dialog, confirmation_key, confirm_reviewed_participants
from roly.tournament_ui import (
    AUDIT_NAMES, KST, MATCH_FORMATS, PREPARATION_STATES,
    PREPARATION_STATUS, participant_management_dialog, bracket_dialog, captain_dialog, fixture_rows,
    local_datetime, participant_rows, preparation_team_cards, progress_steps, schedule_label,
)
from roly.ui import ROLE_NAMES, context, heading, perform, can_create, can_edit


core, competition, token, actor = context()
service = TournamentService(core, competition)
events = [event for event in service.list_events() if event["kind"] == "AUCTION"]
staff = can_create(actor)
event_map = {event["id"]: event for event in events}
event_ids = list(event_map)
requested_event = st.session_state.pop("focus_event", None)
if requested_event in event_ids:
    st.session_state.auction_event = requested_event
if event_ids and st.session_state.get("auction_event") not in event_ids:
    st.session_state.auction_event = event_ids[0]
selected_event = event_map.get(st.session_state.get("auction_event"))
live_page = bool(selected_event and selected_event["status"] in ("AUCTION", "AUCTION_RUNNING"))
creation_preset = st.session_state.pop("create_build_mode", None)
if creation_preset == "AUCTION":
    st.session_state.t_create_open = True

if not live_page:
    heading("경매", "참가자와 팀장을 확정하고 경매와 대진을 준비합니다.")
if staff and not live_page:
    if st.button("경매 만들기", type="primary", icon=":material/add:", key="t_open_create"):
        st.session_state.t_create_open = True
        st.session_state.pop("t_preparation_dialog", None)
elif not staff:
    st.session_state.pop("t_create_open", None)

if staff and st.session_state.get("t_create_open"):
    # Creation chooses the new event before its selection widget is registered.
    creation_dialog(service, token)

if not events:
    st.caption("등록된 경매가 없습니다. 경매를 만들면 참가 회원을 등록할 수 있습니다." if staff else "등록된 경매가 없습니다.")
    st.stop()

event_id = st.selectbox("경매 선택", event_ids,
    format_func=lambda selected_id: f"{event_map[selected_id]['title']} · #{selected_id}",
    key="auction_event", label_visibility="collapsed" if live_page else "visible")
event = service.get_event(event_id)
editable = can_edit(actor, event)
status = event["status"]
team_count = event["team_count"]
build_mode = event["build_mode"]
capacity = team_count * 5
players = event["players"]
player_map = {player["member_id"]: player for player in players}
team_map = {team["id"]: team for team in event["teams"]}
team_names = {team_id: team["name"] for team_id, team in team_map.items()}

detail_area = st.expander("경매 정보") if live_page else st.container(border=True, key="auction_details")
with detail_area:
    if not live_page:
        st.subheader(event["title"])
    with st.container(horizontal=True):
        st.badge(PREPARATION_STATUS.get(status, status), color="green" if status in ("COMPLETED", "FINISHED") else "gray")
        st.caption(f"{schedule_label(event.get('starts_at'))} · {team_count}팀/{capacity}명 · 주최자 {event.get('created_by_name') or event.get('host_name') or event['created_by']}")
    if event.get("description"):
        st.write(event["description"])
    progress_steps(status)
    if live_page and staff:
        if st.button("경매 만들기", icon=":material/add:", key="t_open_create"):
            st.session_state.t_create_open = True
            st.session_state.pop("t_preparation_dialog", None)
            st.rerun()

if editable and status in ("DRAFT", "RECRUITING"):
    with st.expander("경매 정보 수정"):
        scheduled = local_datetime(event.get("starts_at")) or datetime.now(KST)
        formats = list(allowed_formats(team_count, build_mode))
        with st.form(f"t_edit_form_{event_id}"):
            edited_title = st.text_input("수정할 경매 이름", value=event["title"], max_chars=100)
            edited_date = st.date_input("수정할 날짜", value=scheduled.date())
            edited_time = st.time_input("수정할 시작 시각", value=scheduled.time().replace(tzinfo=None))
            edited_description = st.text_area("수정할 설명", value=event.get("description", ""), max_chars=2000)
            edited_format = st.selectbox("수정할 경기 방식", formats, index=formats.index(event["format"]) if event["format"] in formats else 0, format_func=MATCH_FORMATS.get)
            edit_submit = st.form_submit_button("경매 정보 저장")
        if edit_submit:
            perform(lambda: service.update_details(token, event_id, edited_title, datetime.combine(edited_date, edited_time, tzinfo=KST).isoformat(), edited_description, edited_format), "경매 정보를 저장했습니다.")

if not editable and not live_page:
    st.caption("조회 중입니다. 운영 작업은 주최자 또는 관리자가 처리합니다.")

if not live_page and status in PREPARATION_STATES and players:
    from roly.riot_ui import member_refresh_picker
    member_refresh_picker(core, token, players, key=f"auction_{event_id}")

if status == "DRAFT":
    if editable and st.button("참가자 선택 시작", type="primary", key=f"t_open_{event_id}"):
        perform(lambda: service.open_recruitment(token, event_id), "참가자 선택을 시작했습니다.")
elif status == "RECRUITING" and editable:
    reviewed_key = confirmation_key(core, actor, event_id)
    reviewed_token = st.session_state.setdefault(reviewed_key, event["roster_token"])
    roster_stale = reviewed_token != event["roster_token"]
    if st.session_state.get(f"{reviewed_key}_error"):
        st.error(st.session_state.pop(f"{reviewed_key}_error"))
    if roster_stale:
        st.warning("다른 화면에서 참가 명단이나 준비 상태가 바뀌었습니다. 최신 명단을 확인한 뒤 확정해 주세요.")
        if st.button("확정할 최신 명단 불러오기", key=f"t_confirm_reload_{event_id}"):
            st.session_state[reviewed_key] = event["roster_token"]
            st.rerun()
    with st.container(horizontal=True):
        if st.button("참가자 등록·수정", key=f"t_edit_roster_{event_id}", icon=":material/person_add:"):
            st.session_state.t_preparation_dialog = ("participants", event_id)
        st.button("참가자 확정", type="primary", disabled=roster_stale or len(players) != capacity,
                  key=f"t_participants_confirm_{event_id}", on_click=confirm_reviewed_participants,
                  args=(service, token, event_id, reviewed_token, reviewed_key))
        st.caption(f"저장된 참가자 {len(players)}/{capacity}명 · 확정 시 전력점수와 포지션을 보존합니다.")
elif status == "CAPTAIN_SELECTION" and editable:
    if st.button("팀장 지정하기", type="primary", key=f"t_open_captains_{event_id}"):
        st.session_state.t_preparation_dialog = ("captains", event_id)
elif status == "TEAM_BUILDING" and editable:
    with st.container(horizontal=True):
        if st.button("팀장 다시 지정", key=f"t_reset_captains_{event_id}"):
            st.session_state.t_preparation_dialog = ("captains", event_id)
        if st.button("경매 설정하기", type="primary", key=f"t_prepare_auction_{event_id}", icon=":material/tune:"):
            def prepare_settings():
                service.prepare_auction(token, event_id)
                st.session_state.pop("t_auction_settings_review", None)
                st.session_state.t_preparation_dialog = ("settings", event_id)
            perform(prepare_settings, "입찰 시간과 팀별 시작 포인트를 설정해 주세요.")
elif status == "AUCTION_READY":
    render_auction_setup(event, token, actor)
    if editable:
        with st.expander("참가 명단 다시 준비"):
            st.caption("명단은 보존하고 팀장·예산·경매 설정을 다시 준비합니다. 시작한 경매에서는 사용할 수 없습니다.")
            with st.form(f"t_reopen_form_{event_id}"):
                reopen_reason = st.text_input("다시 준비하는 사유", max_chars=1000)
                reopen_submit = st.form_submit_button("참가 명단 다시 준비")
            if reopen_submit:
                perform(lambda: service.reopen_preparation(token, event_id, reopen_reason), "명단을 보존하고 참가자 준비 단계로 돌아갔습니다.")
elif status in ("AUCTION", "AUCTION_RUNNING"):
    current_live = live_service(st.session_state.db_path).get_state(event_id)
    if current_live:
        render_live_auction(event_id)
        st.stop()
    else:
        st.caption("실시간 경매 설정이 없는 기존 경매입니다. 저장된 명단을 조회할 수 있습니다.")
        if editable and not event.get("workflow_version"):
            if st.button("새 경매 준비로 전환", type="primary", key=f"t_upgrade_{event_id}"):
                perform(lambda: service.upgrade_auction(token, event_id), "저장된 명단과 예산으로 실시간 경매를 준비합니다.")
elif status == "BRACKET_SETUP":
    st.subheader("경매 완료")
    st.caption("최종 팀과 낙찰 포인트입니다. 포지션 확인 후 대진을 준비하세요.")
elif status in ("READY", "PLAYING", "IN_PROGRESS", "COMPLETED", "FINISHED", "CANCELLED"):
    if st.button("경매 결과 보기", type="primary", key=f"t_go_results_{event_id}"):
        st.session_state.focus_event = event_id
        st.switch_page("app_pages/events.py")

if status == "BRACKET_SETUP":
    final_state = live_service(st.session_state.db_path).get_state(event_id)
    if editable and final_state and not event["games"]:
        auction_reset_control(event_id)
    sale_correction_control(event, final_state, token, actor)
    final_teams = final_state["teams"] if final_state and final_state["status"] == "COMPLETED" else event["teams"]
    for offset in range(0, len(final_teams), 2):
        for column, team in zip(st.columns(2, gap="medium"), final_teams[offset:offset + 2]):
            with column:
                render_team(team, key=f"auction_final_team_{event_id}_{team['id']}")
    teams_column = st.container(key="auction_finished_teams")
    participants_column = None
else:
    teams_column, participants_column = st.columns([1.25, 1])
    teams_column = teams_column.container(border=True, key="auction_preparation_teams")
    participants_column = participants_column.container(border=True, key="auction_participants")
with teams_column:
    if status != "BRACKET_SETUP":
        st.subheader("참가 팀")
        preparation_team_cards(event, core)
    if editable and status == "BRACKET_SETUP":
        with st.expander("팀별 포지션 확인·수정", expanded=not event["games"]):
            st.caption("각 팀의 탑·정글·미드·원딜·서포터가 한 명씩 배정되도록 확인하세요.")
            for team in event["teams"]:
                with st.form(f"t_roles_form_{event_id}_{team['id']}"):
                    st.markdown(f"**{team['name']}**")
                    chosen_roles = {}
                    for player in team["players"]:
                        chosen_roles[player["member_id"]] = st.selectbox(player["riot_id"], list(ROLE_NAMES), index=list(ROLE_NAMES).index(player["role"]), format_func=ROLE_NAMES.get, key=f"t_role_{event_id}_{player['member_id']}")
                    roles_submit = st.form_submit_button("팀 포지션 저장")
                if roles_submit:
                    def save_team_roles():
                        service.set_team_roles(token, event_id, team["id"], chosen_roles)
                    perform(save_team_roles, "팀 포지션을 저장했습니다.")
        formats = list(allowed_formats(team_count, build_mode))
        with st.form(f"t_bracket_form_{event_id}"):
            bracket_format = st.selectbox("대진표 경기 방식", formats, index=formats.index(event["format"]) if event["format"] in formats else 0, format_func=MATCH_FORMATS.get)
            build_submit = st.form_submit_button("대진표 생성", disabled=bool(event["pool"]) or len(event["teams"]) != team_count)
        if build_submit:
            perform(lambda: service.build_bracket(token, event_id, bracket_format), "대진표를 생성했습니다. 참가 팀을 확인해 주세요.")
    if event["games"]:
        st.subheader("대진표")
        st.dataframe(fixture_rows(event["games"]), hide_index=True)
    if editable and status == "BRACKET_SETUP" and event["games"]:
        if st.button("대진표 최종 확인", type="primary", key=f"t_open_bracket_{event_id}"):
            st.session_state.t_preparation_dialog = ("bracket", event_id)

if participants_column is not None:
    with participants_column:
        st.subheader(f"참가자 · {len(players)}/{capacity}명")
        with st.container(horizontal=True):
            for role, role_name in ROLE_NAMES.items():
                count = sum(player["role"] == role for player in players)
                st.caption(f"{role_name} {count}명")
        if editable and players and status in PREPARATION_STATES:
            if st.button("참가자 관리", key=f"t_manage_participants_{event_id}"):
                st.session_state.t_preparation_dialog = ("participant_management", event_id)
        query = st.text_input("참가자 검색", placeholder="닉네임 또는 Riot 태그", key=f"t_search_{event_id}")
        role_filter = st.pills("참가 포지션", ["전체"] + list(ROLE_NAMES), default="전체", format_func=lambda role: ROLE_NAMES.get(role, role), key=f"t_filter_role_{event_id}")
        roster_view = st.segmented_control("참가 명단 보기", ["참가자", "미배정", "제외"], default="참가자", key=f"t_roster_view_{event_id}")
        visible = event.get("excluded_players", []) if roster_view == "제외" else event["pool"] if roster_view == "미배정" else players
        visible = [player for player in visible if query.casefold() in player["riot_id"].casefold() and (role_filter in (None, "전체") or player["role"] == role_filter)]
        if visible:
            st.dataframe(participant_rows(visible, team_names), hide_index=True, height="auto")
        else:
            st.caption("조건에 맞는 참가자가 없습니다.")

with st.expander("경매 운영 기록"):
    if event["audit"]:
        st.dataframe([
            {"시각": schedule_label(row["created_at"]), "작업": AUDIT_NAMES.get(row["action"], row["action"]),
             "내용": row["detail"]} for row in event["audit"]
        ], hide_index=True)
    else:
        st.caption("운영 기록이 없습니다.")

dialog = st.session_state.get("t_preparation_dialog")
if dialog and editable and dialog[1] == event_id and not st.session_state.get("t_create_open"):
    if dialog[0] == "participants" and status == "RECRUITING":
        participants_dialog(service, token, event_id)
    elif dialog[0] == "captains" and status in ("CAPTAIN_SELECTION", "TEAM_BUILDING"):
        captain_dialog(service, token, event_id)
    elif dialog[0] == "settings" and status == "AUCTION_READY":
        auction_settings_dialog(event_id, token)
    elif dialog[0] == "participant_management" and status in PREPARATION_STATES:
        participant_management_dialog(service, token, event_id)
    elif dialog[0] == "bracket" and status == "BRACKET_SETUP":
        bracket_dialog(service, token, event_id)
    else:
        st.session_state.pop("t_preparation_dialog", None)
elif dialog:
    st.session_state.pop("t_preparation_dialog", None)

if st.session_state.get("live_reset_event") == event_id and status in ("AUCTION_READY", "BRACKET_SETUP"):
    auction_reset_dialog(event_id, token)
