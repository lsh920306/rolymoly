"""The shared, server-timed auction view; no bid or balance rules live here."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from time import monotonic
from uuid import uuid4

import streamlit as st

from roly.live_auction import BID_EXTENSION_SECONDS, BID_INCREMENTS, BID_SECONDS_OPTIONS, LiveAuction
from roly.auction_components import render_stage, render_team, render_overview, render_queue, render_remaining, render_sound
from roly.auction_sync import render_auction_sync
from roly.ui import ROLE_NAMES, can_edit, perform, services


KST = timezone(timedelta(hours=9))
LOT_LABELS = {"QUEUED": "대기", "PENDING": "대기", "OPEN": "입찰 중", "SOLD": "낙찰", "UNSOLD": "유찰", "CANCELLED": "낙찰 취소"}
SESSION_LABELS = {"READY": "시작 대기", "RUNNING": "입찰 중", "PAUSED": "일시정지", "WAITING": "다음 선수 대기", "COMPLETED": "경매 완료", "CANCELLED": "경매 취소"}


@st.cache_resource
def live_service(db_path):
    core, competition = services(db_path)
    live = LiveAuction(core, competition)
    if live.has_active_sessions():
        live.ensure_worker()
    return live


def stamp(value):
    if not value:
        return ""
    parsed = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST).strftime("%H:%M:%S")


def _increment(key, amount, highest_bid):
    st.session_state[key] = max(int(st.session_state.get(key, 0)), int(highest_bid or 0)) + amount


def _reset_amount(key, highest_bid):
    st.session_state[key] = int(highest_bid or 0)


def _view_adjacent_team(key, team_ids, direction):
    current = st.session_state.get(key)
    index = team_ids.index(current) if current in team_ids else 0
    st.session_state[key] = team_ids[(index + direction) % len(team_ids)]


def _live_action(event_id, action, message, *, lot_id=None):
    """Widget callback: let Streamlit rerun this fragment, keeping sound mounted."""
    try:
        action()
    except sqlite3.OperationalError:
        st.session_state[f"live_notice_{event_id}"] = {"success": False, "message": "입찰 처리 결과를 확인하지 못했습니다. 현재 최고가를 확인한 뒤 다시 시도해 주세요.", "lot_id": lot_id, "token": st.session_state.get("token")}
    except (ValueError, PermissionError, sqlite3.IntegrityError) as error:
        st.session_state[f"live_notice_{event_id}"] = {"success": False, "message": str(error), "lot_id": lot_id, "token": st.session_state.get("token")}
    except sqlite3.Error:
        st.session_state[f"live_notice_{event_id}"] = {"success": False, "message": "입찰 처리 결과를 확인하지 못했습니다. 저장소 상태를 확인하고 있습니다.", "lot_id": lot_id, "token": st.session_state.get("token")}
    else:
        st.session_state[f"live_notice_{event_id}"] = {"success": True, "message": message, "lot_id": lot_id, "token": st.session_state.get("token")}


def _submit_bid(live, token, event_id, lot_id, amount_key, request_key, displayed_amount):
    def submit():
        if st.session_state.get(f"live_pending_{event_id}"):
            if not _resolve_pending_bid(live, token, event_id):
                raise sqlite3.OperationalError("Unresolved bid")
        submitted_amount = int(st.session_state[amount_key])
        if submitted_amount != displayed_amount:
            raise ValueError("입찰 금액이 화면의 금액과 달라 접수하지 않았습니다. 현재 금액을 확인한 뒤 다시 입찰해 주세요.")
        pending_key = f"live_pending_{event_id}"
        st.session_state[pending_key] = {"token": token, "db_path": live.core.db_path,
            "event_id": event_id, "lot_id": lot_id, "amount": submitted_amount,
            "request_id": st.session_state[request_key], "request_key": request_key}
        try:
            live.place_bid(token, event_id, lot_id, submitted_amount, st.session_state[request_key])
        except (ValueError, PermissionError, sqlite3.IntegrityError):
            st.session_state.pop(pending_key, None)
            raise
        st.session_state.pop(pending_key, None)
        st.session_state[request_key] = uuid4().hex
    with st.spinner("입찰 접수를 확인하고 있습니다.", show_time=True):
        _live_action(event_id, submit, "입찰을 접수했습니다.", lot_id=lot_id)


def _resolve_pending_bid(live, token, event_id):
    """Confirm the old immutable request before accepting another bid intent."""
    key = f"live_pending_{event_id}"
    pending = st.session_state.get(key)
    if not pending:
        return True
    if pending["token"] != token or pending["db_path"] != live.core.db_path:
        st.session_state.pop(key, None)
        return True
    try:
        receipt = live.resolve_bid(token, event_id, pending["lot_id"], pending["amount"], pending["request_id"])
    except sqlite3.OperationalError:
        st.session_state[f"live_notice_{event_id}"] = {"success": False,
            "message": "이전 입찰의 접수 여부를 확인하고 있습니다. 연결이 회복되면 자동으로 확인합니다.",
            "lot_id": None, "token": token}
        return False
    except (ValueError, PermissionError, sqlite3.IntegrityError) as error:
        st.session_state.pop(key, None)
        st.session_state[f"live_notice_{event_id}"] = {"success": False,
            "message": str(error), "lot_id": None, "token": token}
        return False
    except sqlite3.Error:
        st.session_state[f"live_notice_{event_id}"] = {"success": False,
            "message": "이전 입찰의 접수 여부를 확인하지 못했습니다. 저장소가 정상화되면 자동으로 다시 확인합니다.",
            "lot_id": None, "token": token}
        return False
    st.session_state.pop(key, None)
    if st.session_state.get(pending["request_key"]) == pending["request_id"]:
        st.session_state[pending["request_key"]] = uuid4().hex
    st.session_state[f"live_notice_{event_id}"] = {"success": bool(receipt),
        "message": f"이전 {pending['amount']:,} P 입찰의 접수를 확인했습니다." if receipt else
            "이전 입찰은 접수되지 않았습니다. 현재 금액과 남은 시간을 확인한 뒤 다시 입찰해 주세요.",
        "lot_id": None, "token": token}
    return True


def close_team_overview():
    st.session_state.pop("live_overview_event", None)


def close_auction_reset():
    st.session_state.pop("live_reset_event", None)
    st.session_state.pop("live_reset_review", None)


def auction_reset_control(event_id):
    if st.button("경매 초기화", key=f"live_reset_open_{event_id}", type="primary"):
        close_team_overview()
        close_sale_correction()
        close_auction_reset()
        close_auction_settings()
        st.session_state.live_reset_event = event_id


@st.dialog("경매 초기화", width="medium", on_dismiss=close_auction_reset)
def auction_reset_dialog(event_id, token):
    if token != st.session_state.get("token"):
        close_auction_reset()
        st.warning("로그인 상태가 변경되었습니다. 창을 닫고 다시 열어 주세요.")
        return
    try:
        live = live_service(st.session_state.db_path)
        actor = live.core.session(token)
        event = live.competition.get_event(event_id)
    except sqlite3.Error:
        st.error("초기화 정보를 불러오지 못했습니다. 연결이 회복된 뒤 다시 열어 주세요.")
        return
    if not can_edit(actor, event):
        close_auction_reset()
        st.warning("이 경매의 주최자 또는 관리자만 초기화할 수 있습니다.")
        return
    context = (live.core.db_path, token, event_id)
    review = st.session_state.get("live_reset_review")
    if not review or review["context"] != context:
        try:
            preview = live.preview_reset(token, event_id)
        except (ValueError, PermissionError, sqlite3.Error) as error:
            st.error("초기화 정보를 불러오지 못했습니다. 잠시 후 다시 열어 주세요." if isinstance(error, sqlite3.Error) else str(error))
            return
        review = {"context": context, "preview": preview, "request_id": uuid4().hex}
        st.session_state.live_reset_review = review
    preview = review["preview"]
    st.markdown(f"**{preview['title']}**")
    st.warning(f"낙찰 {preview['sold_count']}건의 배정을 되돌리고 {preview['refund_total']:,} P를 돌려줍니다.")
    st.caption("참가자·팀장·시작 포인트·입찰 시간은 유지합니다. 선수 순서를 새로 섞고 시작 대기로 돌아갑니다. 이전 입찰 기록은 보존됩니다.")
    st.dataframe([{"팀": row["team_name"], "현재 포인트": row["before"], "초기화 후 포인트": row["after"]}
                  for row in preview["team_balances"]], hide_index=True, height="auto")
    with st.form(f"live_reset_form_{event_id}", border=False):
        reason = st.text_input("초기화 사유", value="경매 다시 시작", max_chars=1000)
        st.caption("진행 중 입찰이나 배정이 바뀌면 초기화가 중단됩니다. 최신 상태를 확인한 뒤 다시 실행해 주세요.")
        cancel_column, confirm_column = st.columns(2)
        cancel = cancel_column.form_submit_button("취소", width="stretch")
        confirm = confirm_column.form_submit_button("경매 초기화 확정", type="primary", width="stretch")
    if cancel:
        close_auction_reset()
        st.rerun()
    if confirm:
        def apply_reset():
            live.reset(token, event_id, reason=reason, request_id=review["request_id"],
                       expected_fingerprint=preview["fingerprint"])
            close_auction_reset()
            st.session_state.pop(f"live_pending_{event_id}", None)
        perform(apply_reset, "경매를 초기화했습니다. 설정을 확인하고 경매를 시작해 주세요.")
    if st.button("최신 초기화 내용 확인", key=f"live_reset_reload_{event_id}", type="tertiary"):
        st.session_state.pop("live_reset_review", None)
        st.rerun()


def close_sale_correction():
    st.session_state.pop("sale_dialog_event", None)
    st.session_state.pop("sale_preview", None)


@st.dialog("낙찰 정정", width="large", on_dismiss=close_sale_correction)
def sale_correction_dialog(event_id):
    live = live_service(st.session_state.db_path)
    token = st.session_state.get("token")
    state = live.get_state(event_id)
    sold = {lot["id"]: lot for lot in state["lots"] if lot["status"] == "SOLD"}
    if not sold:
        st.info("정정할 낙찰이 없습니다.")
        return
    teams = {team["id"]: team for team in state["teams"]}
    st.caption("낙찰 취소는 포인트를 환불하고 재경매 대상으로 돌립니다. 변경 사유와 원래 입찰 기록은 보존됩니다.")
    lot_id = st.selectbox("정정할 낙찰 선수", list(sold), format_func=lambda key: sold[key]["riot_id"], key=f"sale_lot_{event_id}")
    lot = sold[lot_id]
    options = [None] + list(teams)
    selected_team = st.selectbox("정정 후 팀", options,
        index=options.index(lot["highest_team_id"]), key=f"sale_team_{event_id}_{lot_id}",
        format_func=lambda key: teams[key]["name"] if key is not None else "낙찰 취소 · 재경매")
    amount = st.number_input("정정 후 낙찰 포인트", min_value=0, value=int(lot["highest_bid"] or 0), step=5,
        disabled=selected_team is None, key=f"sale_amount_{event_id}_{lot_id}")
    reason = st.text_input("낙찰 정정 사유", max_chars=1000, key=f"sale_reason_{event_id}")
    proposal = (event_id, lot_id, selected_team, int(amount) if selected_team is not None else 0, reason.strip())
    saved = st.session_state.get("sale_preview")
    if saved and saved["proposal"] != proposal:
        st.session_state.pop("sale_preview", None)
        saved = None
    if st.button("환불·팀 변경 미리보기", key=f"sale_preview_button_{event_id}"):
        try:
            if not reason.strip():
                raise ValueError("낙찰 정정 사유를 입력해 주세요.")
            preview = live.preview_sale_correction(token, event_id, lot_id, team_id=selected_team, amount=proposal[3])
            saved = {"proposal": proposal, "preview": preview, "request_id": uuid4().hex}
            st.session_state.sale_preview = saved
        except (ValueError, PermissionError, sqlite3.Error) as error:
            st.error("저장소 응답을 확인하지 못했습니다. 다시 미리보기해 주세요." if isinstance(error, sqlite3.OperationalError) else str(error))
    if saved:
        preview = saved["preview"]
        st.write(f"{preview['before']['team_name']} · {preview['before']['amount']:,} P → "
                 + ("낙찰 취소" if preview["requeue"] else f"{preview['after']['team_name']} · {preview['after']['amount']:,} P"))
        st.dataframe([{"팀": row["team_name"], "현재 포인트": row["before"], "정정 후 포인트": row["after"],
                       "현재 인원": row["players_before"], "정정 후 인원": row["players_after"]}
                      for row in preview["team_balances"]], hide_index=True)
        if st.button("정정 확정", type="primary", key=f"sale_apply_{event_id}"):
            def apply():
                live.correct_sale(token, event_id, lot_id, team_id=selected_team, amount=proposal[3],
                    reason=proposal[4], request_id=saved["request_id"], expected_fingerprint=preview["fingerprint"])
                close_sale_correction()
            perform(apply, "낙찰을 정정했습니다. 명단과 포인트를 확인한 뒤 경매를 재개해 주세요.")


def sale_correction_control(event, state, token, actor):
    allowed = bool(actor and actor["role"] == "admin" and state and not event.get("games") and not event.get("has_games") and (
        state["status"] == "PAUSED" and event["status"] == "AUCTION"
        or state["status"] == "COMPLETED" and event["status"] == "BRACKET_SETUP"))
    if not allowed:
        if st.session_state.get("sale_dialog_event") == event["id"]:
            close_sale_correction()
        return
    if any(lot["status"] == "SOLD" for lot in state["lots"]):
        if st.button("낙찰 정정", key=f"sale_open_{event['id']}"):
            close_team_overview()
            close_auction_reset()
            st.session_state.sale_dialog_event = event["id"]
        if st.session_state.get("sale_dialog_event") == event["id"]:
            sale_correction_dialog(event["id"])


@st.dialog("전체 경매 현황", width="large", on_dismiss=close_team_overview)
def team_overview(event_id):
    refresh_team_overview(event_id)


@st.fragment(run_every=1)
def refresh_team_overview(event_id):
    try:
        actor, state = live_service(st.session_state.db_path).get_view(st.session_state.get("token"), event_id)
    except sqlite3.Error:
        st.caption("팀 현황을 잠시 불러올 수 없습니다. 연결이 회복되면 다시 표시합니다.")
        return
    if not state:
        st.caption("경매 정보를 확인할 수 없습니다.")
        return
    render_overview(state, key=f"team_overview_{event_id}", member_id=actor.get("member_id") if actor else None)


@st.fragment(run_every=1)
def watch_auction_start(event_id):
    """Keep waiting clients in sync without rerunning the settings form."""
    render_auction_sync(key=f"auction_wait_sync_{event_id}")
    live = live_service(st.session_state.db_path)
    if live.get_event_status(event_id) != "AUCTION_READY":
        st.rerun(scope="app")


def close_auction_settings():
    st.session_state.pop("t_preparation_dialog", None)
    st.session_state.pop("t_auction_settings_review", None)


def _settings_version(event, current):
    return (event["roster_token"], current.get("updated_at") if current else None)


@st.dialog("경매 설정", width="medium", on_dismiss=close_auction_settings)
def auction_settings_dialog(event_id, token):
    """A short setup form, with permissions and stage checked on each rerun."""
    live = live_service(st.session_state.db_path)
    actor = live.core.session(token)
    if ("token" in st.session_state and token != st.session_state.get("token")) or not actor:
        close_auction_settings()
        st.warning("로그인 상태가 변경되었습니다. 창을 닫고 다시 열어 주세요.")
        return
    event = live.competition.get_event(event_id)
    current = live.get_state(event_id)
    if not can_edit(actor, event):
        close_auction_settings()
        st.warning("이 경매의 주최자 또는 관리자만 설정할 수 있습니다.")
        return
    if event["status"] != "AUCTION_READY" or (current and current["status"] != "READY"):
        close_auction_settings()
        st.warning("준비 상태가 변경되었습니다. 시작 전 경매에서만 설정할 수 있습니다.")
        return
    context = (live.core.db_path, token, event_id)
    version = _settings_version(event, current)
    review = st.session_state.get("t_auction_settings_review")
    teams = current["teams"] if current else event["teams"]
    if not review or review["context"] != context:
        review = {"context": context, "version": version}
        st.session_state.t_auction_settings_review = review
        st.session_state[f"live_seconds_{event_id}"] = int(current["bid_seconds"]) if current else 10
        for team in teams:
            st.session_state[f"live_budget_{event_id}_{team['id']}"] = int(team["budget"])
    if review["version"] != version:
        st.warning("다른 화면에서 팀 또는 설정을 변경했습니다. 최신 설정을 불러와 주세요.")
        if st.button("최신 경매 설정 불러오기", key=f"live_settings_reload_{event_id}"):
            st.session_state.pop("t_auction_settings_review", None)
            st.rerun()
        return
    players = {player["member_id"]: player for player in event["players"]}
    budgets = {}
    with st.form(f"live_settings_{event_id}", border=False):
        seconds = st.selectbox("선수별 입찰 시간 (초)", list(BID_SECONDS_OPTIONS),
                              format_func=lambda value: f"{value}초", key=f"live_seconds_{event_id}")
        st.caption(f"유효 입찰마다 남은 시간 +{BID_EXTENSION_SECONDS}초 · 선택한 시간이 상한 · 다음 선수 준비 3초")
        with st.container(height=360 if len(teams) > 4 else "content", border=False, gap="small"):
            for team in teams:
                captain = players[team["captain_id"]]
                with st.container(border=True, gap="xxsmall"):
                    detail, points = st.columns([3, 1.25], vertical_alignment="center", gap="small")
                    with detail:
                        st.markdown(f"**{team['name']}**")
                        st.caption(f"팀장 {captain['riot_id']} · {ROLE_NAMES[captain['role']]}")
                    with points:
                        budgets[team["id"]] = st.number_input(
                            f"{team['name']} 시작 포인트", min_value=0, step=10,
                            key=f"live_budget_{event_id}_{team['id']}",
                            help="팀장 전력으로 계산한 기본값입니다. 시작 전에 변경할 수 있습니다.")
        st.caption("선수는 무작위 순서로 진행하며, 설정을 다시 저장해도 순서는 유지됩니다.")
        cancel_column, save_column = st.columns(2)
        cancel = cancel_column.form_submit_button("취소", width="stretch")
        save = save_column.form_submit_button("경매 설정 저장", type="primary", width="stretch")
    if cancel:
        close_auction_settings()
        st.rerun()
    if save:
        def save_settings():
            live.configure(token, event_id, bid_seconds=int(seconds), team_budgets=budgets, expected_settings=review["version"])
            close_auction_settings()
        perform(save_settings, "경매 설정을 저장했습니다.")


def render_auction_setup(event, token, actor):
    """Show saved setup and actions; editing stays inside one explicit dialog."""
    watch_auction_start(event["id"])
    live = live_service(st.session_state.db_path)
    current = live.get_state(event["id"])
    if not can_edit(actor, event):
        st.caption("진행자가 경매를 준비하고 있습니다.")
        return
    if current:
        st.markdown(f"**입찰 {int(current['bid_seconds'])}초** · {len(current['teams'])}팀 준비 완료")
        st.caption(" · ".join(f"{team['name']} {team['budget']:,} P" for team in current["teams"]))
    else:
        st.caption("팀장 지정 완료 · 입찰 시간과 시작 포인트를 설정해 주세요.")
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button("경매 설정하기", key=f"live_open_settings_{event['id']}",
                     type="secondary" if current else "primary", icon=":material/tune:"):
            close_team_overview()
            close_sale_correction()
            close_auction_reset()
            st.session_state.pop("t_auction_settings_review", None)
            st.session_state.t_preparation_dialog = ("settings", event["id"])
        if st.button("경매 시작", key=f"live_start_{event['id']}", type="primary", disabled=current is None):
            perform(lambda: live.start(token, event["id"]), "경매를 시작했습니다.")
        if current:
            auction_reset_control(event["id"])


@st.fragment(run_every=0.5)
def render_live_auction(event_id):
    """Refresh the active auction twice a second; decisions use the DB clock."""
    render_auction_sync(key=f"auction_live_sync_{event_id}")
    token = st.session_state.get("token")
    live = live_service(st.session_state.db_path)
    _resolve_pending_bid(live, token, event_id)
    try:
        actor, state = live.get_view(token, event_id)
        view_received_at = monotonic()
    except sqlite3.Error:
        st.error("경매 상태를 불러오지 못했습니다. 연결이 회복되면 다시 확인합니다.")
        return
    if not state:
        st.caption("아직 경매 설정이 저장되지 않았습니다.")
        return
    event = state["event"]
    if st.session_state.get("live_overview_event") not in (None, event_id):
        close_team_overview()
    if st.session_state.get("live_reset_event") not in (None, event_id):
        close_auction_reset()
    editable = can_edit(actor, event)
    status = state["status"]
    status_key = f"live_observed_status_{event_id}"
    previous_status = st.session_state.get(status_key)
    st.session_state[status_key] = status
    if previous_status is not None and previous_status != status and status in ("COMPLETED", "READY"):
        st.rerun(scope="app")
    if status in ("RUNNING", "WAITING", "PAUSED"):
        live.ensure_worker()
    worker_notice = st.empty()
    if state.get("worker_error"):
        worker_notice.error("경매 마감 처리를 다시 시도하고 있습니다. 진행자는 일시정지한 뒤 상태를 확인해 주세요.")
    notice_key = f"live_notice_{event_id}"
    notice = st.session_state.get(notice_key)
    current_lot_id = (state.get("current_lot") or {}).get("id")
    if notice and (not isinstance(notice, dict) or notice["token"] != token or notice["lot_id"] not in (None, current_lot_id)):
        st.session_state.pop(notice_key, None)
        notice = None
    # Keep the fragment's following columns at a stable delta path. Inserting
    # an alert ahead of them while a timed rerun arrives can invalidate the
    # browser's block tree (notably when several bids are rejected together).
    notice_area = st.empty()
    if notice:
        if notice["success"]:
            st.toast(notice["message"])
            st.session_state.pop(notice_key, None)
        else:
            notice_area.error(notice["message"])
    teams = {team["id"]: team for team in state["teams"]}
    lot = state.get("current_lot")
    display_state = state
    if status == "READY" and lot is None:
        lot = next((item for item in state.get("lots", []) if item["status"] == "QUEUED"), None)
        display_state = dict(state, current_lot=lot)
    unsold = [player for player in event["players"] if player["team_id"] is None and player["state"] == "UNSOLD"]
    round_finished = ((status == "WAITING" and state.get("next_at") is None) or (
        status == "PAUSED" and state.get("paused_phase") == "WAITING" and state.get("pause_remaining") is None))
    retry_ready = bool(unsold and round_finished and not state.get("queued_count") and (not lot or lot["status"] != "OPEN"))
    retry_help = ("낙찰 선수를 제외한 유찰 선수 전원을 섞어 3초 후 다시 진행합니다." if retry_ready else
        "이번 순서에 남은 선수를 모두 진행한 뒤 유찰 선수만 다시 시작할 수 있습니다." if unsold else
        "유찰 선수가 생기고 이번 순서를 모두 마친 뒤 사용할 수 있습니다.")
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center", key=f"auction_live_toolbar_{event_id}"):
        st.subheader(event["title"])
        with st.container(horizontal=True, vertical_alignment="center", width="content", gap="small"):
            render_sound(state, key=f"live_sound_{event_id}")
            if editable:
                auction_reset_control(event_id)
    render_queue(state, key=f"live_queue_{event_id}")
    left, right = st.columns([1, 2.25], gap="medium")
    center = right.container(key=f"auction_current_panel_{event_id}", border=True, gap="small")
    with center, st.container(key=f"live_controls_{event_id}", border=False, gap="xsmall"):
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            st.subheader("현재 경매")
            st.caption(f"주최자 {event.get('host_name') or '미등록'}")
            st.badge(SESSION_LABELS.get(status, status), color="orange" if status == "PAUSED" else "green" if status == "RUNNING" else "gray")
        if editable and status != "READY":
            with st.container(horizontal=True, vertical_alignment="center"):
                if status in ("RUNNING", "WAITING"):
                    st.button("일시 정지", type="primary", icon=":material/pause:", key="live_pause", on_click=_live_action,
                        help="주최자·관리자가 입찰 시간과 다음 선수 대기를 멈춥니다.",
                        args=(event_id, lambda: live.pause(token, event_id), "경매를 일시정지했습니다."))
                elif status == "PAUSED":
                    st.button("경매 재개", type="primary", icon=":material/play_arrow:", key="live_resume", on_click=_live_action,
                        help="주최자·관리자가 일시 정지 전 남은 시간부터 다시 진행합니다.",
                        args=(event_id, lambda: live.resume(token, event_id), "남은 시간부터 경매를 재개했습니다."))
                st.button("유찰 재시작", icon=":material/replay:", key="live_retry", disabled=not retry_ready, help=retry_help, on_click=_live_action,
                    args=(event_id, lambda: live.retry_unsold(token, event_id), "유찰 선수만 무작위로 다시 진행합니다. 3초 후 시작합니다."))
            if retry_ready:
                st.caption(f"유찰 {len(unsold)}명 · 재시작하면 유찰 선수만 무작위로 3초 후 진행합니다.")
            elif status == "PAUSED":
                st.caption("일시 정지 중 · 재개하면 남은 시간부터 진행합니다.")
            elif unsold:
                st.caption(f"유찰 {len(unsold)}명 · 현재 순서를 마친 뒤 재시작할 수 있습니다.")
        elif status == "PAUSED":
            st.caption("진행자가 일시 정지했습니다. 주최자 또는 관리자가 재개하면 입찰할 수 있습니다.")

    own_team = next((team for team in teams.values() if actor and actor.get("member_id") == team["captain_id"]), None)
    view_team = next((team for team in teams.values() if actor and any(player["member_id"] == actor.get("member_id") for player in team["players"])), None)
    with left, st.container(border=True, key=f"auction_team_panel_{event_id}", gap="small"):
        team_key = f"live_team_view_{event_id}_{actor['id'] if actor else 'guest'}"
        if st.session_state.get(team_key) not in teams:
            st.session_state[team_key] = view_team["id"] if view_team else next(iter(teams))
        selected_team = st.session_state[team_key]
        with st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="distribute"):
            st.markdown(f"**팀 현황**  `{list(teams).index(selected_team) + 1}/{len(teams)}`")
            with st.container(horizontal=True, width="content", gap="xxsmall", key=f"live_carousel_tools_{event_id}"):
                if st.button("전체 팀 보기", icon=":material/grid_view:", help="전체 팀 보기", width=34,
                             key=f"live_team_overview_{event_id}"):
                    close_sale_correction()
                    close_auction_reset()
                    st.session_state.live_overview_event = event_id
                st.button("이전 팀", icon=":material/chevron_left:", help="이전 팀", width=34,
                          key=f"live_previous_team_{event_id}", on_click=_view_adjacent_team,
                          args=(team_key, list(teams), -1))
                st.button("다음 팀", icon=":material/chevron_right:", help="다음 팀", width=34,
                          key=f"live_next_team_{event_id}", on_click=_view_adjacent_team,
                          args=(team_key, list(teams), 1))
        render_team(teams[selected_team], key=f"live_team_card_{event_id}", member_id=actor.get("member_id") if actor else None)
        if st.session_state.get("live_overview_event") == event_id:
            team_overview(event_id)
    with center, st.container(gap="small"):
        if status == "COMPLETED":
            st.success("경매가 완료되었습니다. 명단과 포지션을 확인하고 대진표를 만드세요.")
        elif lot:
            render_stage(display_state, key=f"live_stage_{event_id}", elapsed_seconds=monotonic() - view_received_at)
            remaining = max(0, float(lot.get("remaining_seconds") or 0))
            if lot["status"] == "UNSOLD":
                closed_event = next((item for item in state.get("events", [])
                    if item["type"] == "UNSOLD" and item.get("lot_id") == lot["id"]), None)
                failure_reason = json.loads(closed_event["detail"]).get("reason") if closed_event else None
                if failure_reason or lot.get("highest_team_id") is not None:
                    st.warning("낙찰 조건을 충족하지 못해 유찰되었습니다.")
                    st.caption(failure_reason or "진행자가 해당 선수의 낙찰 실패 기록을 확인해 주세요.")
                    st.caption("참가 상태를 확인한 뒤 재경매를 진행해 주세요.")
                else:
                    st.info("입찰이 없어 유찰되었습니다. 마지막 선수까지 진행한 뒤 재경매할 수 있습니다.")
            if own_team:
                amount_key = f"live_amount_{lot['id']}"
                request_key = f"live_request_{lot['id']}"
                st.session_state.setdefault(amount_key, int(lot.get("highest_bid") or 0))
                st.session_state.setdefault(request_key, uuid4().hex)
                can_bid = (event["status"] == "AUCTION" and status == "RUNNING" and lot["status"] == "OPEN"
                    and remaining > 0 and len(own_team["players"]) < 5 and not st.session_state.get(f"live_pending_{event_id}"))
                balance_column, amount_column = st.columns(2, gap="xsmall")
                with balance_column, st.container(border=True, key=f"live_balance_panel_{event_id}", gap="xxsmall"):
                    st.caption(f"내 포인트 · {own_team['name']}")
                    st.markdown(f"**{own_team['remaining']:,} P**")
                with amount_column, st.container(border=True, key=f"live_amount_panel_{event_id}", gap="xxsmall"):
                    amount = st.number_input("입찰할 포인트", min_value=0, step=5, key=amount_key, disabled=not can_bid)
                with st.container(horizontal=True, gap="xxsmall", key=f"live_increments_{event_id}"):
                    for increment in BID_INCREMENTS:
                        st.button(f"+{increment}", key=f"live_add_{lot['id']}_{increment}", on_click=_increment,
                                  args=(amount_key, increment, lot.get("highest_bid")), disabled=not can_bid, width="stretch")
                    st.button("금액 초기화", key=f"live_reset_{lot['id']}", on_click=_reset_amount, width="stretch",
                              args=(amount_key, lot.get("highest_bid")), disabled=not can_bid,
                              help="입찰 예정 금액을 현재 최고가로 돌립니다. 접수된 입찰은 변경하지 않습니다.")
                st.button(f"{amount:,} P 입찰하기", type="primary", width="stretch", key=f"live_bid_{lot['id']}", disabled=not can_bid,
                    on_click=_submit_bid, args=(live, token, event_id, lot["id"], amount_key, request_key, int(amount)))
                if len(own_team["players"]) >= 5:
                    st.caption("팀원 5명이 확정되어 입찰을 마쳤습니다.")
                elif not can_bid:
                    st.caption("일시 정지 중에는 입찰할 수 없습니다." if status == "PAUSED" else
                               "입찰 접수를 확인하고 있습니다." if st.session_state.get(f"live_pending_{event_id}") else
                               "다음 선수 입찰을 기다려 주세요.")
            elif actor:
                is_participant = any(player["member_id"] == actor.get("member_id") for player in event["players"])
                membership = f"참가 중 · {view_team['name']}" if view_team else "참가 중 · 팀 배정 대기" if is_participant else "관전 중"
                st.caption(f"{membership} · 이 경매의 팀장만 입찰할 수 있습니다.")
            else:
                st.caption("본인 계정으로 로그인해 주세요. 이 경매의 팀장으로 지정되면 입찰할 수 있습니다.")
        elif state.get("unsold_count", 0):
            st.caption("유찰 선수가 남아 있습니다. 진행자가 재경매를 시작할 수 있습니다.")
        else:
            st.caption("다음 경매 선수를 준비하고 있습니다.")
        render_remaining(state, key=f"live_remaining_{event_id}")
        lots = state.get("lots", [])
        with st.expander("경매 참가자 · 진행 현황"):
            st.dataframe([
                {"순서": item.get("sequence", index) + 1, "선수": item["riot_id"],
                 "포지션": ROLE_NAMES.get(item["role"], item["role"]),
                 "낙찰 포인트": f"{int(item['highest_bid']):,} P" if item["status"] == "SOLD" else "—",
                 "상태": LOT_LABELS.get(item["status"], item["status"])}
                for index, item in enumerate(lots)
            ], hide_index=True, height=280)
        sale_correction_control(event, state, token, actor)
    with right, st.container(key=f"auction_history_panel_{event_id}", border=True, gap="small"):
        st.markdown("**입찰 기록**")
        cancelled_ids = {item["id"] for item in state.get("lots", []) if item["status"] == "CANCELLED"}
        bids = [item for item in state.get("bids", []) if item["lot_id"] not in cancelled_ids]
        archived_bids = [item for item in state.get("bids", []) if item["lot_id"] in cancelled_ids]
        empty_bids = st.empty()
        if not bids:
            empty_bids.caption("접수된 입찰이 없습니다.")
        with st.container(height=220, border=False):
            for bid in bids[:30]:
                name = bid.get("team_name") or teams.get(bid["team_id"], {}).get("name", "")
                with st.container(key=f"auction_bid_card_{event_id}_{bid['id']}", border=True, gap="xxsmall"):
                    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center", gap="xsmall"):
                        st.markdown(f"**{bid['amount']:,} P**")
                        st.caption(stamp(bid.get("created_at")))
                    st.text(name)
                    st.caption(f"팀장 {bid.get('riot_id', '')}")
        if archived_bids:
            with st.expander("초기화·취소 전 입찰 기록"):
                st.dataframe([{"시각": stamp(bid.get("created_at")), "팀": bid.get("team_name") or teams.get(bid["team_id"], {}).get("name", ""),
                               "팀장": bid.get("riot_id", ""), "입찰 포인트": bid["amount"]}
                              for bid in archived_bids], hide_index=True, height=220)
        with st.expander("진행 기록"):
            event_labels = {"CONFIGURE": "경매 설정", "START": "경매 시작", "LOT_OPEN": "선수 경매 시작", "BID": "입찰 접수", "SOLD": "낙찰", "UNSOLD": "유찰", "PAUSE": "일시정지", "RESUME": "경매 재개", "RETRY_UNSOLD": "재경매", "COMPLETED": "경매 완료", "CANCELLED": "경매 취소", "SALE_CORRECTION": "낙찰 정정", "RESET": "경매 초기화"}
            for item in state.get("events", [])[:20]:
                detail = json.loads(item["detail"])
                player = next((row["riot_id"] for row in lots if row["member_id"] == detail.get("member_id")), "")
                st.caption(f"{stamp(item['created_at'])} · {event_labels.get(item['type'], item['type'])} {player}")
    if st.session_state.get("live_reset_event") == event_id:
        auction_reset_dialog(event_id, token)
