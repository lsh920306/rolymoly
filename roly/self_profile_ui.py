"""A personal account can rename its existing member without re-registering."""
from hashlib import sha256
import sqlite3

import streamlit as st


def can_change_nickname(actor, member_id):
    return bool(actor and actor.get("member_id") == member_id
                and actor.get("member_status") == "APPROVED"
                and actor.get("registration_status") == "APPROVED")


def nickname_change_control(core, token, actor, member_id, *, key):
    if can_change_nickname(actor, member_id):
        if st.button("닉네임 변경", key=key, icon=":material/edit:"):
            nickname_dialog(core, token, member_id)


@st.dialog("닉네임 변경", width="small")
def nickname_dialog(core, token, member_id):
    # A dialog fragment retains its original arguments after the outer page's
    # session or selected database has changed.
    if (st.session_state.get("token") != token
            or st.session_state.get("db_path") != core.db_path):
        st.warning("로그인 계정이나 이용 공간이 변경되었습니다. 닫고 다시 열어 주세요.")
        return
    try:
        actor = core.session(token)
        if not can_change_nickname(actor, member_id):
            st.warning("승인된 본인 계정으로 다시 로그인해 주세요.")
            return
        member = core.get_own_member(token)
    except (ValueError, PermissionError):
        st.warning("승인된 본인 계정으로 다시 로그인해 주세요.")
        return
    except sqlite3.Error:
        st.error("회원 정보를 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.")
        return
    if not member or member["id"] != member_id or member["status"] != "APPROVED":
        st.warning("승인된 본인 계정만 닉네임을 변경할 수 있습니다.")
        return

    database = sha256(str(core.db_path).encode()).hexdigest()[:12]
    context_key = f"member_edit_context_self_{database}_{actor['id']}_{member_id}"
    current = {"riot_id": member["riot_id"], "updated_at": member["updated_at"]}
    st.session_state.setdefault(context_key, current)
    reviewed = st.session_state[context_key]
    stale = reviewed["updated_at"] != member["updated_at"]
    if stale:
        st.warning("회원 정보가 변경되었습니다. 최신 정보를 불러온 뒤 다시 확인해 주세요.")
        if st.button("최신 닉네임 불러오기", key=f"{context_key}_reload"):
            reviewed = current
            st.session_state[context_key] = reviewed
            stale = False
    version = sha256(reviewed["updated_at"].encode()).hexdigest()[:16]
    st.caption("로그인 아이디, 주·부 포지션, 클랜 티어와 점수는 유지됩니다.")
    with st.form(f"{context_key}_{version}_form"):
        riot_id = st.text_input("새 Riot ID", value=reviewed["riot_id"], placeholder="닉네임#태그",
                                max_chars=53, key=f"{context_key}_{version}_riot", disabled=stale)
        submitted = st.form_submit_button("닉네임 저장", type="primary", disabled=stale)
    st.caption("게임에서 변경한 닉네임과 태그를 입력하세요. Riot 정보는 저장 후 갱신하기로 다시 조회합니다.")
    if submitted and not stale:
        try:
            core.update_own_riot_id(token, riot_id, expected_updated_at=reviewed["updated_at"])
        except (ValueError, PermissionError) as error:
            st.error(str(error))
            return
        except sqlite3.Error:
            st.error("저장 응답을 확인하지 못했습니다. 현재 닉네임을 확인한 뒤 다시 시도해 주세요.")
            return
        st.session_state.pop(context_key, None)
        st.session_state.flash = "닉네임을 저장했습니다. 기존 계정으로 계속 이용할 수 있습니다."
        st.rerun()
