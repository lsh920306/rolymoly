"""Native Streamlit shell and shared, presentation-only helpers."""
import os
import re
from pathlib import Path
import secrets
import sqlite3

import pandas as pd
import streamlit as st

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.storage_config import ConfigError, operating_database
from roly.browser_session import clear_login, sync_login
from roly.deployment_config import (load_deployment_config, require_operating_target,
                                    bind_deployed_session, clear_deployed_session)

ROOT = Path(__file__).resolve().parent.parent
ROLE_NAMES = {"TOP": "탑", "JG": "정글", "MID": "미드", "AD": "원딜", "SUP": "서포터"}
STATUS = {"DRAFT": "경매 준비", "RECRUITING": "참가자 모집", "CAPTAIN_SELECTION": "팀장 지정", "TEAM_BUILDING": "팀 구성", "AUCTION_READY": "경매 준비", "AUCTION": "경매 중", "BRACKET_SETUP": "대진 준비", "READY": "경기 준비", "PLAYING": "진행 중", "IN_PROGRESS": "진행 중", "COMPLETED": "종료", "CANCELLED": "취소"}
FORMATS = {"SINGLE": "단판", "LEAGUE": "풀리그", "TOURNAMENT": "토너먼트", "GROUP_STAGE": "조별리그 + 결승", "RANKING": "4팀 순위 결정전"}
KINDS = {"NORMAL": "일반내전", "AUCTION": "경매"}


@st.cache_resource
def services(path):
    core = Core(path)
    competition = Competition(core)
    from roly.live_auction import LiveAuction
    live = LiveAuction(core, competition)
    if live.has_active_sessions():
        live.ensure_worker()
    return core, competition


def context():
    core, competition = services(st.session_state.db_path)
    token = st.session_state.get("token")
    return core, competition, token, core.session(token) if token else None


@st.cache_resource
def lounge_service(path):
    from roly.lounge import Lounge
    return Lounge(services(path)[0])


def heading(title, description):
    st.title(title)
    st.caption(description)


def require_staff(actor, admin=False):
    if not actor or actor["role"] not in ("admin", "organizer") or (admin and actor["role"] != "admin"):
        message = "운영진 로그인이 필요합니다. 왼쪽 메뉴에서 로그인해 주세요." if not actor else "관리자 권한이 필요한 화면입니다." if admin else "진행자 또는 관리자 권한이 필요한 화면입니다."
        st.info(message, icon=":material/lock:")
        st.stop()


def can_create(actor):
    return bool(actor and (actor["role"] in ("admin", "organizer") or actor.get("member_status") == "APPROVED"))


def can_edit(actor, event):
    return bool(can_create(actor) and (actor["role"] == "admin" or actor["id"] == event["created_by"]))


def require_member(actor):
    if not can_create(actor):
        st.info("승인된 회원으로 로그인하면 내전을 개설할 수 있습니다.", icon=":material/lock:")
        st.page_link("app_pages/join.py", label="가입·승인 상태 확인", icon=":material/person:")
        st.stop()


def perform(action, success_message):
    try:
        action()
    except sqlite3.OperationalError:
        st.error("저장소 응답을 확인하지 못했습니다. 현재 처리 내역을 확인한 뒤 다시 시도해 주세요.", icon=":material/error:")
        return None
    except (ValueError, PermissionError, sqlite3.IntegrityError) as error:
        st.error(str(error), icon=":material/error:")
        return None
    st.session_state.flash = success_message
    st.rerun()


def award_label(member):
    parts = []
    for key, symbol in (("trophies", "🏆"), ("medals", "🏅"), ("stars", "⭐"), ("cats", "🐱")):
        count = member.get(key, 0)
        if count > 0:
            parts.append(f"{symbol} × {count}" if key == "trophies" and count > 10 else symbol * count)
    return " ".join(parts) or "-"


def member_table(members):
    columns = ["Riot ID", "우승 기호", "클랜 티어", "현재 티어", "현재 LP", "주 포지션", "부 포지션", "전력점수", "일반내전", "승률"]
    return pd.DataFrame([
        {"Riot ID": member["riot_id"], "주 포지션": ROLE_NAMES[member["main_role"]],
         "부 포지션": ROLE_NAMES[member["sub_role"]], "전력점수": member["score"],
         "클랜 티어": member.get("clan_tier") or "미입력", "현재 티어": member.get("current_tier") or "미입력",
         "현재 LP": member.get("current_tier_lp"),
         "일반내전": member["wins"] + member["losses"],
         "승률": round(member["wins"] / (member["wins"] + member["losses"]) * 100, 1) if member["wins"] + member["losses"] else None,
         "우승 기호": award_label(member)}
        for member in members
    ], columns=columns)


def team_cards(event):
    teams = event["teams"]
    for start in range(0, len(teams), 4):
        for column, team in zip(st.columns(min(4, len(teams) - start)), teams[start:start + 4]):
            with column.container(border=True):
                st.markdown(f"**{team['name']}**")
                if event["kind"] == "AUCTION":
                    st.caption(f"포인트 {team['remaining']:,} P · {len(team['players'])}/5명")
                st.metric("팀 전력", f"{team['total_score']:,.0f}", label_visibility="collapsed")
                for player in sorted(team["players"], key=lambda p: ROLES.index(p["role"])):
                    label = player["riot_id"].split("#")[0]
                    suffix = " · 팀장" if player["member_id"] == team["captain_id"] else ""
                    st.caption(f"{ROLE_NAMES[player['role']]}  |  {label}{suffix}  ·  {player['score']:g}")


def event_picker(events, label="경기 선택", key="event_picker"):
    if not events:
        st.info("아직 개설된 경기가 없습니다.")
        st.stop()
    by_id = {e["id"]: e for e in events}
    desired = st.session_state.pop("focus_event", None)
    ids = list(by_id)
    saved_key = f"_event_picker_selection_{key}"
    incoming = st.session_state.get(key)
    # Native selectboxes serialize their formatted labels. A client returning an
    # earlier title can therefore arrive as a string, rather than the event ID.
    # The immutable suffix recovers that ID; every path stays in this event list.
    if isinstance(incoming, str):
        match = re.fullmatch(r".* · #([0-9]{1,19})", incoming, flags=re.DOTALL)
        incoming = int(match.group(1)) if match else None
    candidates = (desired, incoming, st.session_state.get(saved_key))
    selected = next((value for value in candidates if isinstance(value, int) and not isinstance(value, bool) and value in by_id), ids[0])
    st.session_state[saved_key] = selected
    # Also send the resolved value back to the client when its title changed.
    # Public Session State assignment makes Streamlit emit its normal set_value.
    st.session_state[key] = selected
    return st.selectbox(label, ids,
                        format_func=lambda i: f"{by_id[i]['title']} · #{i}", key=key, persist_state="session")


def run():
    st.set_page_config(page_title="롤리몰리", page_icon=":material/sports_esports:", layout="wide", initial_sidebar_state="expanded")
    st.html(ROOT / "static" / "app.css")
    st.logo(str(ROOT / "static" / "rolymoly-logo.png"), size="large")
    data_root = Path(os.environ.get("ROLYMOLY_DATA_DIR", ROOT / ".data"))
    try:
        deployment = load_deployment_config()
    except ConfigError as error:
        clear_deployed_session(st.session_state)
        st.error(str(error))
        st.stop()
    if deployment.is_test:
        st.badge("검수용", icon=":material/science:", color="orange")
    operation_error = None
    try:
        operation_target = operating_database(data_root)
        require_operating_target(deployment, operation_target)
    except ConfigError as error:
        operation_target, operation_error = None, str(error)
    if not deployment.allow_demo:
        if operation_error:
            clear_deployed_session(st.session_state)
            st.error(operation_error)
            st.stop()
        bind_deployed_session(st.session_state, operation_target)
    if "space" not in st.session_state:
        st.session_state.space = "운영 공간" if operation_error or operation_target.startswith("supabase://") else "체험 공간"
    if operation_error and st.session_state.space != "체험 공간":
        with st.sidebar:
            st.selectbox("Workspace", ["체험 공간", "운영 공간"], key="space", persist_state="session")
        st.error(operation_error)
        st.caption("운영 공간은 Supabase 설정이 필요합니다. 예시를 확인하려면 사이드바에서 체험 공간을 선택하세요.")
        st.stop()
    if deployment.allow_demo and "demo_id" not in st.session_state:
        st.session_state.demo_id = secrets.token_hex(16)
    mode = st.session_state.space
    from roly.demo import DEMO_DATA_VERSION
    db_path = str(data_root / f"demo-v{DEMO_DATA_VERSION}-{st.session_state.demo_id}.sqlite3") if mode == "체험 공간" else operation_target
    if st.session_state.get("db_path") != db_path:
        # A form started in one database must never be submitted to another.
        preserved = {"space", "demo_id", "demo_token", "demo_accounts", "db_path", "token", "profile_database", "profile_origin"}
        for key in list(st.session_state):
            if key not in preserved:
                del st.session_state[key]
        st.session_state.db_path = db_path
        st.session_state.token = st.session_state.get("demo_token") if mode == "체험 공간" else None
        st.session_state.pop("focus_event", None)
    try:
        core, competition = services(db_path)
    except sqlite3.OperationalError:
        st.error("저장소에 연결하지 못했습니다. 잠시 후 다시 접속해 주세요.")
        st.stop()
    if deployment.allow_demo and mode == "체험 공간" and not core.has_admin():
        from roly.demo import seed
        for key in ("demo_token", "demo_accounts", "token"):
            st.session_state.pop(key, None)
        with st.spinner("나만의 체험 공간을 준비하고 있습니다…"):
            try:
                credentials = []
                st.session_state.token = seed(core, credentials=credentials)
                st.session_state.demo_token = st.session_state.token
                st.session_state.demo_accounts = credentials
            except Exception:
                st.error("체험 데이터를 준비하지 못했습니다. 새 체험 공간으로 다시 시작해 주세요.")
    if deployment.allow_demo and mode == "체험 공간":
        st.session_state.demo_riot_database = db_path
        st.session_state.demo_riot_rate_target = operation_target
    if mode == "운영 공간":
        sync_login(core)
        # Resume persisted Riot jobs after a server restart; no API request is
        # made by this render thread or by the auction fragment.
        from roly.riot_ui import riot_service
        try:
            riot_service(core)
        except (ConfigError, sqlite3.Error):
            pass  # The member refresh control provides the actionable message.
    actor = core.session(st.session_state.get("token")) if st.session_state.get("token") else None
    if st.session_state.get("token") and not actor:
        clear_login()
        st.rerun()
    with st.sidebar:
        contact = lounge_service(db_path).profile()["contact_url"]
        if contact:
            st.link_button("클랜 연락 링크", contact, icon=":material/link:", width="stretch")
        if deployment.allow_demo:
            st.selectbox("Workspace", ["체험 공간", "운영 공간"], key="space", persist_state="session")
        if deployment.allow_demo and mode == "체험 공간":
            st.caption("예시 데이터")
            demo_accounts = {account["username"]: account for account in st.session_state.get("demo_accounts", [])}
            if demo_accounts:
                def switch_demo_account():
                    # Native callbacks run before the next full script. Recheck
                    # current server configuration, not the old rendered mode.
                    try:
                        permitted = load_deployment_config().allow_demo
                    except ConfigError:
                        permitted = False
                    if not permitted:
                        clear_deployed_session(st.session_state)
                        return
                    if st.session_state.get("space") != "체험 공간" or st.session_state.get("db_path") != core.db_path:
                        return
                    account = demo_accounts[st.session_state.demo_account_choice]
                    try:
                        next_token = core.login(account["username"], account["password"])
                    except (ValueError, PermissionError) as error:
                        st.session_state.demo_login_error = str(error)
                        if actor and actor["username"] in demo_accounts:
                            st.session_state.demo_account_choice = actor["username"]
                        return
                    previous_token = st.session_state.get("token")
                    if previous_token and previous_token != st.session_state.get("demo_token"):
                        core.logout(previous_token)
                    st.session_state.token = next_token
                    if account["username"] == "demo":
                        st.session_state.demo_token = next_token
                    for key in list(st.session_state):
                        if key.startswith(("live_amount_", "live_request_", "live_pending_", "live_command_", "live_transport_", "t_confirm_roster_")) or key in ("home_dialog", "t_preparation_dialog", "t_auction_settings_review", "normal_creation_draft", "t_creation_draft", "_clan_profile_editor", "live_reset_event", "live_reset_review"):
                            st.session_state.pop(key, None)
                    st.session_state.pop("demo_login_error", None)
                    st.session_state.flash = f"{account['label']} 계정으로 전환했습니다."
                if st.session_state.get("demo_account_choice") not in demo_accounts:
                    st.session_state.demo_account_choice = actor["username"] if actor and actor["username"] in demo_accounts else "demo"
                st.selectbox("체험 계정", list(demo_accounts), key="demo_account_choice",
                    format_func=lambda username: demo_accounts[username]["label"], persist_state="session", on_change=switch_demo_account)
                st.caption("이 체험 공간 안에서 계정을 바꿔 입찰합니다. 다른 브라우저의 체험 데이터와는 공유되지 않습니다.")
                if not actor:
                    st.button("체험 계정으로 로그인", on_click=switch_demo_account)
                if st.session_state.get("demo_login_error"):
                    st.error(st.session_state.demo_login_error)
            else:
                st.caption("계정 전환 테스트는 체험 설정에서 새 체험 공간을 만든 뒤 이용할 수 있습니다.")
        if actor:
            role = {"admin": "관리자", "organizer": "진행자", "member": "회원"}.get(actor["role"], "회원")
            st.caption(f"{actor['display_name']} · {role}")
            if mode == "운영 공간" and st.button("로그아웃", type="tertiary"):
                core.logout(st.session_state.token)
                clear_login()
                st.rerun()
        elif mode == "체험 공간":
            st.caption("체험 설정에서 새 공간을 만들어 주세요.")
        elif core.has_admin():
            with st.expander("계정 로그인", expanded=True):
                with st.form("login", clear_on_submit=True, border=False):
                    username = st.text_input("로그인 아이디", key="login_username")
                    password = st.text_input("비밀번호", type="password", key="login_password")
                    if st.form_submit_button("로그인", type="primary", width="stretch"):
                        def login():
                            st.session_state.token = core.login(username, password)
                        perform(login, "로그인했습니다.")
        elif core.is_postgres:
            st.info("운영 준비 중입니다. 최초 관리자 계정 설정 후 로그인할 수 있습니다.")
        else:
            st.caption("운영을 시작하려면 관리자 계정을 만들어 주세요.")
            with st.form("setup", clear_on_submit=True, border=False):
                username = st.text_input("관리자 아이디")
                name = st.text_input("표시 이름", value="운영진")
                password = st.text_input("비밀번호 (10자 이상)", type="password")
                repeat = st.text_input("비밀번호 확인", type="password")
                if st.form_submit_button("운영 시작하기", type="primary", width="stretch"):
                    def setup():
                        if password != repeat:
                            raise ValueError("비밀번호 확인이 일치하지 않습니다.")
                        core.setup_admin(username, password, name)
                        st.session_state.token = core.login(username, password)
                    perform(setup, "관리자 계정을 만들었습니다. 회원 가입을 받을 수 있습니다.")
        if deployment.allow_demo and mode == "체험 공간":
            with st.popover("체험 설정"):
                st.caption("새 공간을 만들면 예시 데이터로 다시 시작합니다.")
                if st.button("새 체험 공간 만들기", icon=":material/refresh:"):
                    st.session_state.demo_id = secrets.token_hex(16)
                    st.session_state.pop("demo_token", None)
                    st.session_state.pop("demo_accounts", None)
                    st.rerun()
    # Register shared widgets before navigation so they belong to the entrypoint,
    # not to the selected page. Streamlit still displays navigation above them.
    page = st.navigation({
        "": [
            st.Page("app_pages/home.py", title="라운지", icon=":material/home:", default=True),
            st.Page("app_pages/members.py", title="회원", icon=":material/group:"),
            st.Page("app_pages/profile.py", title="회원 프로필", url_path="profile", visibility="hidden"),
            st.Page("app_pages/normal.py", title="일반내전", icon=":material/sports_esports:"),
            st.Page("app_pages/auction.py", title="경매", icon=":material/gavel:"),
            st.Page("app_pages/events.py", title="경기 기록", icon=":material/emoji_events:"),
            st.Page("app_pages/join.py", title="내 계정" if actor else "회원가입", icon=":material/person_add:"),
        ],
        "관리": [st.Page("app_pages/admin.py", title="운영 관리", icon=":material/tune:",
                        visibility="visible" if actor and actor["role"] == "admin" else "hidden")],
    }, position="sidebar", expanded=True)
    if page.url_path != "auction":
        st.session_state.pop("live_overview_event", None)
        st.session_state.pop("sale_dialog_event", None)
        st.session_state.pop("sale_preview", None)
        st.session_state.pop("live_reset_event", None)
        st.session_state.pop("live_reset_review", None)
    # A queued page fragment must keep the same ancestor path when the one-time
    # login/action message is consumed on a subsequent full rerun.
    flash_area = st.empty()
    if message := st.session_state.pop("flash", None):
        flash_area.success(message)
    with st.container(gap="small", key=f"page_{page.url_path or 'home'}"):
        if actor and actor.get("member_status") == "PENDING" and page.url_path != "join":
            st.info("가입 승인 후 이용할 수 있습니다. 내 계정에서 신청 상태를 확인해 주세요.")
            st.page_link("app_pages/join.py", label="회원가입 상태 확인", icon=":material/person:")
            st.stop()
        page.run()
