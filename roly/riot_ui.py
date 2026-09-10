"""Riot refresh controls; HTTP only runs in the independent queue worker."""
from hashlib import sha256
import sqlite3
from time import monotonic

import streamlit as st

from .riot_api import load_riot_config, RiotConfig
from .storage_config import ConfigError


IDLE_CHECK_SECONDS = 30
PENDING_STATES = frozenset({"QUEUED", "RUNNING", "RETRY", "PENDING"})
POLL_CACHE_LIMIT = 16


def _refresh_requested(cache_key):
    # A button callback precedes rendering, so revoked sessions also get a
    # freshly disabled button instead of reusing the idle display permission.
    st.session_state.get("_riot_poll_cache", {}).pop(cache_key, None)


def _rate_core(path):
    from .service_resources import service_bundle
    return service_bundle(path)["core"]


def _service(path, key_hash, rate_path, _core, _config):
    from .riot_sync import RiotSync
    from .service_resources import backend_revision, riot_resource
    rate_core = _core if rate_path == path else _rate_core(rate_path)
    key = (backend_revision(), key_hash, rate_path, id(_core), id(rate_core))
    return riot_resource(path, key, rate_path, lambda: RiotSync(_core, config=_config, rate_core=rate_core))


def _clear_service():
    from .service_resources import clear_riot
    clear_riot()


_service.clear = _clear_service


def _clear_rate_core(path=None):
    from .service_resources import clear_services
    clear_services(path)


_rate_core.clear = _clear_rate_core


def riot_service(core):
    # Only the app-created demo can use the configured key. Arbitrary SQLite
    # development/QA stores never read it. Every allowed demo shares the same
    # operational rate budget, while jobs and fetched profiles stay in its DB.
    rate_path = core.db_path
    if not core.is_postgres:
        from .storage_config import SUPABASE_TARGET
        if (st.session_state.get("space") != "체험 공간"
                or st.session_state.get("demo_riot_database") != core.db_path
                or st.session_state.get("demo_riot_rate_target") != SUPABASE_TARGET):
            return None
        rate_path = SUPABASE_TARGET
    from .riot_sync import stop_workers_except_key, stop_demo_workers
    try:
        config = load_riot_config()
    except ConfigError:
        stop_workers_except_key(RiotConfig())
        raise
    stop_workers_except_key(config)
    if not config.allow_demo:
        stop_demo_workers()
        if not core.is_postgres:
            return None
    if not config.enabled:
        return None
    return _service(core.db_path, sha256(config.api_key.encode()).hexdigest(), rate_path, core, config)


@st.fragment(run_every=3)
def refresh_control(core, token, member_ids, *, key, label="Riot 정보 갱신",
                    show_details=False, rerun_on_update=True):
    """Queue writes require a fresh backend session; polling is DB-only."""
    if not member_ids:
        return
    ids = tuple(dict.fromkeys(member_ids))
    try:
        sync = riot_service(core)
    except ConfigError as error:
        st.warning(str(error))
        _cached_details(core, ids, show_details)
        return
    except sqlite3.Error:
        st.caption("Riot 갱신 서버에 연결하지 못했습니다. 저장된 프로필은 계속 볼 수 있습니다.")
        _cached_details(core, ids, show_details)
        return
    if sync is None:
        _cached_details(core, ids, show_details)
        return
    try:
        context = sha256(repr((str(core.db_path), token, ids)).encode()).hexdigest()
        cache_key = f"riot_poll_{key}"
        cache = st.session_state.setdefault("_riot_poll_cache", {})
        cached = cache.get(cache_key)
        if not isinstance(cached, dict) or cached.get("context") != context:
            cached = None
        checked_at = monotonic()
        due = cached is None or cached["pending"] or checked_at - cached["checked_at"] >= IDLE_CHECK_SECONDS
        actor = core.session(token) if due else None
        allowed = (bool(actor and (actor["role"] in ("admin", "organizer") or actor.get("member_status") == "APPROVED"))
                   if due else cached["allowed"])
        refresh = st.button(label, key=f"riot_refresh_{key}", disabled=not allowed,
                            on_click=_refresh_requested, args=(cache_key,),
                            icon=":material/sync:", help="버튼을 누를 때만 조회합니다. 최근 5분 이내 요청은 재사용하며 전력점수와 클랜 티어는 유지됩니다.")
        if refresh and not due:
            # Cached permission controls presentation only. Every click is
            # checked against the current session before a job can be queued.
            actor = core.session(token)
            allowed = bool(actor and (actor["role"] in ("admin", "organizer") or actor.get("member_status") == "APPROVED"))
            if not allowed:
                st.caption("로그인 상태를 확인한 뒤 다시 갱신해 주세요.")
        if refresh and allowed:
            queued = 0
            for start in range(0, len(ids), 40):
                queued += sync.enqueue(token, ids[start:start + 40], force=True)
            from .riot_sync import start_worker
            start_worker(core, sync.config, sync=sync)
            st.caption(f"{queued}명 갱신 예약 · 5분 이내 요청은 재사용합니다.")
        if due or refresh:
            profiles = {}
            for start in range(0, len(ids), 40):
                profiles.update(sync.get_profiles(ids[start:start + 40]))
            cache.pop(cache_key, None)
            cache[cache_key] = {"context": context, "checked_at": checked_at, "profiles": profiles,
                "allowed": allowed, "pending": any(value.get("status") in PENDING_STATES for value in profiles.values())}
            while len(cache) > POLL_CACHE_LIMIT:
                cache.pop(next(iter(cache)))
        else:
            profiles = cached["profiles"]
        ready = sum(bool(value.get("fetched_at") and value.get("current_tier")) for value in profiles.values())
        pending = sum(value.get("status") in PENDING_STATES for value in profiles.values())
        errors = {value.get("error") for value in profiles.values()} - {None, ""}
        if len(ids) == 1:
            st.caption("Riot 정보 갱신 대기 중입니다." if pending else "솔로·자유랭크 · 숙련도 상위 5개" if ready else "아직 Riot 조회 전입니다.")
        else:
            st.caption(f"Riot 조회 {ready}/{len(ids)}명 · 솔로·자유랭크 · 숙련도 상위 5개" + (f" · 갱신 대기 {pending}명" if pending else ""))
        timestamps = [value.get("updated_at") for value in profiles.values() if value.get("updated_at")]
        if timestamps:
            from .member_records import korean_time
            st.caption(f"최근 조회 완료 {korean_time(max(timestamps))} · 버튼을 눌렀을 때만 갱신")
        for error in sorted(errors):
            st.caption(error)
        if show_details and len(ids) == 1:
            from .riot_profile import public_profile
            profile = profiles.get(ids[0], {})
            show_profile(public_profile(profile) if profile.get("fetched_at") else None,
                         show_updated=False)
        # Refresh the member table/preparation roster once a new cache arrives.
        signature = tuple((mid, value.get("updated_at")) for mid, value in sorted(profiles.items()))
        previous = st.session_state.get(f"riot_display_{key}") if cached is not None else None
        st.session_state[f"riot_display_{key}"] = signature
        if rerun_on_update and previous is not None and signature != previous:
            st.rerun()
    except (ValueError, PermissionError) as error:
        st.warning(str(error))
    except sqlite3.Error:
        st.caption("Riot 갱신 상태를 불러오지 못했습니다. 저장된 회원 정보는 계속 사용할 수 있습니다.")


def _cached_details(core, ids, enabled):
    if enabled and len(ids) == 1:
        from .riot_profile import member_profiles
        try:
            show_profile(member_profiles(core, ids).get(ids[0]))
        except (ValueError, sqlite3.Error):
            st.caption("저장된 Riot 정보를 잠시 불러올 수 없습니다.")


def member_refresh_picker(core, token, members, *, key):
    """A preparation-page lookup selects one member before offering refresh."""
    if not members:
        return
    try:
        if riot_service(core) is None:
            return
    except ConfigError as error:
        st.caption(str(error))
        return
    except sqlite3.Error:
        st.caption("Riot 갱신 서버에 연결하지 못했습니다. 저장된 회원 정보는 계속 사용할 수 있습니다.")
        return
    choices = {member.get("member_id", member.get("id")): member["riot_id"] for member in members}
    picker = st.expander("Riot 정보 확인", key=f"riot_picker_panel_{key}", on_change="rerun")
    if picker.open:
        with picker:
            selected_id = st.selectbox("조회할 회원", list(choices), index=None,
                format_func=choices.get, placeholder="닉네임 또는 Riot 태그로 검색",
                key=f"riot_member_picker_{key}", persist_state="session")
            if selected_id is not None:
                refresh_control(core, token, [selected_id], key=f"{key}_{selected_id}",
                                label="이 회원 갱신", show_details=True)
            else:
                st.caption("확인할 회원 한 명을 선택해 주세요. 선택만으로 API를 호출하지 않습니다.")


def show_profile(profile, *, show_updated=True):
    if not profile:
        st.caption("Riot API 조회 전입니다.")
        return
    from .member_records import korean_time
    lp = f" · {profile['lp']} LP" if profile.get("lp") is not None else ""
    st.markdown(f"**현재 솔로랭크 · {profile['current_tier']}{lp}**")
    flex_lp = f" · {profile['flex_lp']} LP" if profile.get("flex_lp") is not None else ""
    st.markdown(f"**현재 자유랭크 · {profile.get('flex_current_tier') or '미입력'}{flex_lp}**")
    st.caption("주력 챔피언 · 숙련도 상위 5개")
    with st.container(horizontal=True):
        for champion in profile["champions"]:
            with st.container(width=130, border=True):
                if champion["icon_url"]:
                    st.image(champion["icon_url"], width=48)
                st.text(champion["name"])
                st.caption(f"숙련도 {champion['points']:,} · 레벨 {champion['level']}")
    if not profile["champions"]:
        st.caption("아직 챔피언 숙련도 기록이 없습니다.")
    if show_updated and profile.get("updated_at"):
        try:
            st.caption(f"Riot 갱신 {korean_time(profile['updated_at'])}")
        except ValueError:
            pass
