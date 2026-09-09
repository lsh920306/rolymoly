"""Shared, versioned member editing for the member list and administration."""
from hashlib import sha256

import streamlit as st

from roly.core import ROLES
from roly.member_profile import CURRENT_TIERS
from roly.ui import ROLE_NAMES, perform


def current_tier_inputs(member, prefix, *, disabled=False):
    api_managed = member.get("current_tier_source") == "riot"
    disabled = disabled or api_managed
    tier = member.get("current_tier") or ""
    selected = st.selectbox(
        "현재 티어", CURRENT_TIERS, index=CURRENT_TIERS.index(tier),
        format_func=lambda value: value or "미입력", key=f"{prefix}_current_tier", disabled=disabled,
        help="현재 솔로랭크 티어입니다. 전력점수는 자동으로 변경되지 않습니다.",
    )
    lp = st.number_input(
        "현재 LP (선택)", min_value=0, max_value=10000,
        value=member.get("current_tier_lp"), step=1, placeholder="미입력",
        key=f"{prefix}_current_lp", disabled=disabled,
        help="티어가 미입력 또는 언랭크이면 LP도 비워주세요.",
    )
    if api_managed:
        st.caption("Riot API에서 가져온 현재 솔로랭크입니다. 회원 화면의 Riot 정보 갱신으로 업데이트합니다.")
    return selected, lp


def member_edit_form(core, token, actor, member_id, *, prefix):
    if not actor or actor["role"] != "admin":
        st.error("회원 정보 수정은 관리자만 할 수 있습니다.")
        return
    current = core.get_member(member_id)
    database = sha256(str(core.db_path).encode()).hexdigest()[:12]
    context_key = f"member_edit_context_{database}_{prefix}_{actor['id']}_{member_id}"
    st.session_state.setdefault(context_key, dict(current))
    reviewed = st.session_state[context_key]
    stale = reviewed["updated_at"] != current["updated_at"]
    if stale:
        st.warning("다른 작업에서 회원 정보가 변경되었습니다. 최신 정보를 불러온 뒤 다시 확인해주세요.")
        if st.button("최신 회원 정보 불러오기", key=f"{context_key}_reload"):
            reviewed = dict(current)
            st.session_state[context_key] = reviewed
            stale = False
    version = sha256(reviewed["updated_at"].encode()).hexdigest()[:16]
    key = f"{context_key}_{version}"
    with st.form(f"{key}_form"):
        riot_id = st.text_input("Riot ID", value=reviewed["riot_id"], max_chars=53, key=f"{key}_riot", disabled=stale)
        st.markdown("**관리자 설정**")
        st.caption("주·부 포지션과 클랜 티어는 직접 입력합니다. Riot 정보 갱신으로 변경되지 않습니다.")
        with st.container(horizontal=True):
            main_role = st.selectbox("주 포지션", ROLES, index=ROLES.index(reviewed["main_role"]), format_func=ROLE_NAMES.get, key=f"{key}_main", disabled=stale)
            sub_role = st.selectbox("부 포지션", ROLES, index=ROLES.index(reviewed["sub_role"]), format_func=ROLE_NAMES.get, key=f"{key}_sub", disabled=stale)
        clan_tier = st.text_input("클랜 티어", value=reviewed.get("clan_tier") or "", max_chars=32, key=f"{key}_clan", disabled=stale,
                                  help="운영진이 조정하는 내부 티어입니다. 전력점수와 구분해서 관리합니다.")
        st.markdown("**현재 솔로랭크**")
        current_tier, current_lp = current_tier_inputs(reviewed, key, disabled=stale)
        base_score = st.number_input("기본점수", 0, 10000, value=reviewed["base_score"], step=1, key=f"{key}_base", disabled=stale)
        st.caption("전력점수는 기본점수에 경기·수기 증감을 더한 값입니다. 확정된 내전 명단은 당시 정보를 유지합니다.")
        if reviewed["application_notes"]:
            st.caption("이전 신청 메시지")
            st.text(reviewed["application_notes"])
        notes = st.text_area("운영 메모", value=reviewed["notes"], max_chars=2000, key=f"{key}_notes", disabled=stale,
                             help="관리자에게만 표시됩니다. 신청자가 입력한 메시지와 별도로 보관합니다.")
        reason = st.text_input("정보 변경 사유", max_chars=1000, key=f"{key}_reason", disabled=stale)
        submit = st.form_submit_button("회원 정보 저장", type="primary", disabled=stale)
    if submit and not stale:
        def save():
            core.update_member(token, member_id, riot_id, main_role, sub_role, int(base_score), reason, notes,
                               clan_tier=clan_tier, current_tier=current_tier, current_tier_lp=current_lp,
                               expected_updated_at=reviewed["updated_at"])
            st.session_state.pop(context_key, None)
        perform(save, "회원 정보를 저장했습니다. 다음 내전부터 변경한 정보가 적용됩니다.")


@st.dialog("회원 정보 수정", width="medium")
def show_member_editor(core, token, actor, member_id):
    if "token" in st.session_state and token != st.session_state.get("token"):
        st.warning("로그인 계정이 변경되었습니다. 대화상자를 닫고 다시 열어 주세요.")
        return
    # Dialog reruns do not refresh the actor captured by the parent page.
    current_actor = core.session(token)
    if not current_actor:
        st.warning("로그인이 만료되었습니다. 다시 로그인해 주세요.")
        return
    member_edit_form(core, token, current_actor, member_id, prefix="members")
