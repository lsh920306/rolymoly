"""Personal signup, approval status, and password recovery."""
from uuid import uuid4
from hashlib import sha256

import streamlit as st

from roly.browser_session import clear_login
from roly.ui import context, heading, perform, ROLE_NAMES
from roly.member_editor import current_tier_inputs

core, competition, token, actor = context()
heading("내 계정" if actor else "가입 신청", "개인 계정으로 로그인하고 운영진의 가입 승인을 받습니다.")

if actor:
    st.write(f"로그인 아이디 · {actor['username']}")
    member = core.get_own_member(token)
    if actor.get("member_status") == "PENDING":
        review_key = f"registration_edit_{sha256(str(core.db_path).encode()).hexdigest()[:12]}_{actor['id']}"
        st.session_state.setdefault(review_key, dict(member))
        reviewed = st.session_state[review_key]
        stale = reviewed["updated_at"] != member["updated_at"]
        form_version = sha256(reviewed["updated_at"].encode()).hexdigest()[:16]
        if stale:
            st.warning("가입 신청 정보가 변경되었습니다. 최신 정보를 불러온 뒤 다시 수정해주세요.")
            if st.button("최신 신청 정보 불러오기"):
                st.session_state[review_key] = dict(member)
                st.rerun()
        rejected = actor.get("registration_status") == "REJECTED"
        if rejected:
            st.warning("가입 신청을 보완해 주세요. 같은 계정으로 다시 신청할 수 있습니다.")
            st.text(actor.get("rejection_reason") or "운영진에게 문의해 주세요.")
        else:
            st.info("가입 승인 대기 중입니다. 운영진이 승인하면 이 계정으로 내전에 참여할 수 있습니다.")
        with st.form(f"registration_resubmit_{actor['id']}_{form_version}"):
            riot_id = st.text_input("Riot ID", value=reviewed["riot_id"], max_chars=53, disabled=stale, key=f"{review_key}_{form_version}_riot")
            main_role = st.selectbox("주 포지션", list(ROLE_NAMES), index=list(ROLE_NAMES).index(reviewed["main_role"]), format_func=ROLE_NAMES.get, disabled=stale, key=f"{review_key}_{form_version}_main")
            sub_role = st.selectbox("부 포지션", list(ROLE_NAMES), index=list(ROLE_NAMES).index(reviewed["sub_role"]), format_func=ROLE_NAMES.get, disabled=stale, key=f"{review_key}_{form_version}_sub")
            current_tier, current_lp = current_tier_inputs(reviewed, f"{review_key}_{form_version}", disabled=stale)
            notes = st.text_area("운영진에게 전할 말 (선택)", value=reviewed["application_notes"], max_chars=2000, disabled=stale, key=f"{review_key}_{form_version}_application_notes")
            resubmit = st.form_submit_button("보완하고 다시 신청" if rejected else "신청 정보 수정", type="primary", disabled=stale)
        if resubmit and not stale:
            def save_application():
                core.resubmit_registration(token, riot_id, main_role, sub_role, notes, current_tier=current_tier, current_tier_lp=current_lp, expected_updated_at=reviewed["updated_at"])
                st.session_state.pop(review_key, None)
            perform(save_application, "가입 신청 정보를 저장했습니다.")
        if st.button("승인 상태 확인"):
            st.rerun()
    elif member:
        st.success("가입 승인 완료")
        st.write(f"{member['riot_id']} · {ROLE_NAMES[member['main_role']]} / {ROLE_NAMES[member['sub_role']]}")
        lp_text = f" · {member['current_tier_lp']} LP" if member.get("current_tier_lp") is not None else ""
        st.write(f"클랜 티어 {member.get('clan_tier') or '미입력'} · 현재 티어 {member.get('current_tier') or '미입력'}{lp_text} · 전력점수 {member['score']:,} P")
        from roly.self_profile_ui import nickname_change_control
        nickname_change_control(core, token, actor, member["id"], key="account_nickname_change")
        st.caption("닉네임은 직접 변경할 수 있습니다. 주·부 포지션과 클랜 티어 변경은 운영진에게 요청해 주세요.")
        st.caption("내전 참가 신청은 카카오톡에서 받습니다. 개설자가 확정 명단을 등록하면 내 계정에 연결됩니다.")
        st.page_link("app_pages/normal.py", label="일반내전 개설", icon=":material/sports_esports:")
        st.page_link("app_pages/auction.py", label="경매 열기", icon=":material/gavel:")
    with st.expander("비밀번호 변경"):
        with st.form("change_password", clear_on_submit=True):
            current_password = st.text_input("현재 비밀번호", type="password")
            new_password = st.text_input("새 비밀번호 (10자 이상)", type="password")
            repeat = st.text_input("새 비밀번호 확인", type="password")
            if st.form_submit_button("비밀번호 변경"):
                def change():
                    if new_password != repeat:
                        raise ValueError("비밀번호 확인이 일치하지 않습니다.")
                    core.change_password(token, current_password, new_password)
                    clear_login()
                perform(change, "비밀번호를 변경했습니다. 새 비밀번호로 다시 로그인해 주세요.")
else:
    if not core.has_admin():
        st.info("운영 준비 중입니다. 최초 관리자 설정 후 가입 신청을 받을 수 있습니다.")
    else:
        st.session_state.setdefault("signup_request", uuid4().hex)
        with st.form("join_form"):
            username = st.text_input("사용할 로그인 아이디", max_chars=64)
            password = st.text_input("사용할 비밀번호 (10자 이상)", type="password")
            repeat = st.text_input("비밀번호 확인", type="password")
            riot_id = st.text_input("Riot ID", placeholder="닉네임#태그", max_chars=53)
            main_role = st.selectbox("주 포지션", list(ROLE_NAMES), format_func=ROLE_NAMES.get)
            sub_role = st.selectbox("부 포지션", list(ROLE_NAMES), index=1, format_func=ROLE_NAMES.get)
            current_tier, current_lp = current_tier_inputs({}, "signup")
            notes = st.text_area("운영진에게 전할 말 (선택)", max_chars=500)
            agreed = st.checkbox("경기 결과에 따른 점수와 우승 업적 기록, 운영진 승인 절차를 확인했습니다.")
            if st.form_submit_button("가입 신청하기", type="primary", width="stretch"):
                def submit():
                    if not agreed:
                        raise ValueError("운영 절차 확인을 체크해 주세요.")
                    if password != repeat:
                        raise ValueError("비밀번호 확인이 일치하지 않습니다.")
                    core.register_member(username, password, riot_id, main_role, sub_role, notes,
                                         request_key=st.session_state.signup_request, current_tier=current_tier, current_tier_lp=current_lp)
                    st.session_state.token = core.login(username, password)
                    st.session_state.pop("signup_request", None)
                perform(submit, "계정과 가입 신청을 함께 등록했습니다. 운영진의 승인을 기다려 주세요.")
        st.caption("가입 승인은 클랜 가입 절차입니다. 일반내전·경매 참가 신청은 카카오톡에서 별도로 받습니다.")
    with st.expander("비밀번호를 잊으셨나요?"):
        st.caption("카카오톡으로 운영진에게 본인 확인을 요청하고 일회용 재설정 코드를 받아 주세요.")
        with st.form("reset_password", clear_on_submit=True):
            reset_code = st.text_input("일회용 재설정 코드", type="password")
            new_password = st.text_input("새 비밀번호 (10자 이상)", type="password")
            repeat = st.text_input("새 비밀번호 확인", type="password")
            if st.form_submit_button("새 비밀번호 저장"):
                def reset():
                    if new_password != repeat:
                        raise ValueError("비밀번호 확인이 일치하지 않습니다.")
                    core.reset_password(reset_code, new_password)
                    clear_login()
                perform(reset, "새 비밀번호를 저장했습니다. 왼쪽 메뉴에서 로그인해 주세요.")
