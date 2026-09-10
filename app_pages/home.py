"""Clan profile, schedules and member records, using saved data."""
from datetime import datetime

import streamlit as st

from roly.ui import context, lounge_service, ROOT, FORMATS, KINDS, STATUS
from roly.lounge_ui import KST, WEEKDAYS, local_time, time_label, profile_dialog, creation_action
from roly.member_profile_page import open_member_profile
from roly.member_cards import render_member_cards


core, competition, token, actor = context()
lounge = lounge_service(core.db_path)
profile = lounge.profile()
today = datetime.now(KST)
is_admin = bool(actor and actor["role"] == "admin")
members = core.list_members(include_pending=is_admin)
approved = [member for member in members if member["status"] == "APPROVED"]
events = competition.list_events()
active = [event for event in events if event["status"] not in ("COMPLETED", "CANCELLED")]
normal = [event for event in active if event["kind"] == "NORMAL"]
active_auction_count = sum(event["kind"] == "AUCTION" for event in active)
confirmed_games = [game for game in core.list_games() if game["status"] == "CONFIRMED"]
confirmed_games.sort(key=lambda game: local_time(game["played_at"]) or datetime.min.replace(tzinfo=KST), reverse=True)
month_games = sum(
    bool((played := local_time(game["played_at"])) and (played.year, played.month) == (today.year, today.month))
    for game in confirmed_games
)


def event_row(event):
    with st.container(border=True, key=f"lounge_event_{event['id']}"):
        name_column, action_column = st.columns([4, 1.6], vertical_alignment="center")
        with name_column.container(gap="xsmall"):
            st.text(event["title"])
            st.caption(f"{KINDS[event['kind']]} · {event['participant_count']}명 · {event['team_count']}팀 · {FORMATS.get(event['format'], event['format'])}")
            stamp = event.get("starts_at")
            st.caption(f"시작 {time_label(stamp)}" if stamp else f"개설 {time_label(event['created_at'])}")
        with action_column.container(gap="xsmall"):
            st.caption(STATUS.get(event["status"], event["status"]), text_alignment="center")
            if st.button("경매 열기" if event["kind"] == "AUCTION" else "내전 열기", key=f"home_event_{event['id']}", width="stretch"):
                st.session_state.focus_event = event["id"]
                st.switch_page("app_pages/auction.py" if event["kind"] == "AUCTION" else "app_pages/events.py")


def empty_schedule(message, key):
    with st.container(height=210, border=True, vertical_alignment="center", key=key):
        st.caption(message, text_alignment="center")


with st.container(horizontal=True, horizontal_alignment="center"):
    with st.container(width=1150, gap="small"):
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            with st.container(width="content", gap="xsmall"):
                st.title("라운지")
                st.caption(f"{today.year}년 {today.month}월 {today.day}일 {WEEKDAYS[today.weekday()]} · 한국 시간")

        with st.container(border=True, gap="small", key="lounge_profile"):
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                st.subheader(profile["name"], width="content")
                st.page_link("app_pages/join.py", label="회원가입", icon=":material/person_add:")
            poster, details = st.columns([1, 2.1], gap="medium")
            with poster:
                image_path = ROOT / "static" / "rolymoly-poster.png"
                if image_path.is_file():
                    # A bundled poster has a stable public asset URL; it does not
                    # need a session-owned, resized /media file.
                    st.html('<img src="app/static/rolymoly-poster.png" alt="롤리몰리 클랜 소개 포스터" '
                        'style="display:block;width:min(280px,100%);height:auto;aspect-ratio:1;object-fit:contain;border-radius:12px;">')
            with details:
                with st.container(horizontal=True, gap="medium"):
                    with st.container(width="content", gap="xxsmall"):
                        st.caption("클랜원")
                        st.text(f"{len(approved)} / {profile['capacity']}명" if profile["capacity"] else f"{len(approved)}명")
                    with st.container(width="content", gap="xxsmall"):
                        st.caption("개설일")
                        st.text(profile["founded_on"] or "미등록")
                    with st.container(width="content", gap="xxsmall"):
                        st.caption("주 게임")
                        st.text("리그 오브 레전드")
                st.caption(f"일반내전 진행 {len(normal)}개 · 경매 진행 {active_auction_count}개")
                st.markdown("**클랜 소개**")
                if profile["description"]:
                    st.text(profile["description"])
                else:
                    st.caption("아직 등록된 클랜 소개가 없습니다.")
                if is_admin and st.button("소개 수정", type="tertiary", icon=":material/edit:"):
                    st.session_state.home_dialog = ("profile", None)

        main, side = st.columns([2.1, 1], gap="medium")
        with main:
            for kind, label in (("NORMAL", "일반 내전 만들기"), ("AUCTION", "경매 내전 만들기")):
                with st.container(border=True, gap="small", key=f"lounge_{kind.lower()}"):
                    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                        st.subheader(label, width="content")
                        with st.container(width=190):
                            creation_action(actor, kind)
                    st.caption("10명 · 20명 · 포지션별 팀 편성" if kind == "NORMAL" else "4팀 · 6팀 · 8팀 · 팀장 지정 후 포인트 경매")

            with st.container(border=True, gap="small", key="lounge_events"):
                st.subheader("내전 목록")
                event_tabs = st.tabs(["일반 내전", "경매 내전"])
                for tab, kind, label in zip(event_tabs, ("NORMAL", "AUCTION"), ("일반 내전", "경매 내전")):
                    with tab:
                        visible = sorted(
                            (event for event in active if event["kind"] == kind),
                            key=lambda event: event.get("starts_at") or event["created_at"],
                        )
                        st.caption(f"진행 중 {len(visible)}개")
                        if not visible:
                            empty_schedule(f"진행 중인 {label}이 없습니다.", f"lounge_empty_{kind.lower()}")
                        for event in visible:
                            event_row(event)
                        if st.button(f"{label} 보러가기", type="tertiary", key=f"home_{kind.lower()}_browse",
                                     icon=":material/arrow_forward:", icon_position="right"):
                            st.session_state.pop("focus_event", None)
                            if visible:
                                st.session_state.focus_event = visible[0]["id"]
                            if kind == "AUCTION":
                                st.switch_page("app_pages/auction.py")
                            elif visible:
                                st.session_state.events_kind_tab = KINDS[kind]
                                st.session_state.events_active_kind = KINDS[kind]
                                st.switch_page("app_pages/events.py")
                            else:
                                st.switch_page("app_pages/normal.py")

            with st.container(border=True, gap="small", key="lounge_recent"):
                st.subheader("최근 경기")
                if not confirmed_games:
                    st.caption("확정된 경기 기록이 없습니다.")
                game_names = competition.game_labels()
                for game in confirmed_games[:5]:
                    labels = game_names.get(game["id"], {})
                    team_a, team_b = labels.get("team_a_name") or "A팀", labels.get("team_b_name") or "B팀"
                    winner = team_a if game["winner"] == "A" else team_b
                    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                        st.markdown(f"{team_a} vs {team_b}", width="content")
                        st.markdown("1 : 0" if game["winner"] == "A" else "0 : 1", width="content")
                    st.caption(f"단판 · {winner} 승")
                    st.caption(f"{time_label(game['played_at'])} · {labels.get('title') or '개별 경기'} · {KINDS[game['kind']]}")
                    st.divider()
                st.page_link("app_pages/events.py", label="전체 기록", icon=":material/arrow_forward:", icon_position="right")
                st.caption(f"활동 회원 {len(approved)}명  ·  이번 달 확정 경기 {month_games}건  ·  진행 중 경매 {active_auction_count}개")

        with side:
            with st.container(border=True, gap="xsmall", key="lounge_record_link"):
                st.subheader("내전 전력 & 기록")
                st.page_link("app_pages/members.py", label="전체 회원 기록", icon=":material/arrow_forward:", icon_position="right")
            with st.container(border=True, gap="xsmall", key="lounge_members"):
                st.subheader(f"클랜원 · {len(approved)}명")
                query = st.text_input("닉네임으로 검색", key="home_member_search", placeholder="닉네임 또는 Riot ID", label_visibility="collapsed")
                found = [member for member in approved if query.strip().casefold() in member["riot_id"].casefold()]
                found.sort(key=lambda member: member["riot_id"].casefold())
                selected_member_id = render_member_cards(found, key="home_member_cards")
                if selected_member_id is not None:
                    try:
                        open_member_profile(selected_member_id, origin="home")
                    except ValueError as error:
                        st.warning(str(error))

            if is_admin:
                with st.container(border=True, gap="xsmall", key="lounge_pending"):
                    pending = [member for member in members if member["status"] == "PENDING" and member.get("registration_status") != "REJECTED"]
                    pending.sort(key=lambda member: member["created_at"], reverse=True)
                    st.markdown(f"**최근 가입 신청 · {len(pending)}명 대기**")
                    if not pending:
                        st.caption("승인 대기 중인 가입 신청이 없습니다.")
                    for member in pending[:5]:
                        st.text(member["riot_id"])
                        st.caption(f"신청 {time_label(member['created_at'])}")
                    st.page_link("app_pages/admin.py", label="가입 관리", icon=":material/arrow_forward:", icon_position="right")

dialog = st.session_state.get("home_dialog")
if dialog:
    if dialog[0] == "profile" and is_admin:
        profile_dialog(lounge, token)
    else:
        st.session_state.pop("home_dialog", None)
