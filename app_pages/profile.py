"""One compact profile page reached from both members and the lounge."""
import sqlite3
import streamlit as st

from roly.core import integer
from roly.member_profile_page import render_profile
from roly.ui import context

core, competition, token, actor = context()
origin = st.session_state.get("profile_origin", "members")
with st.container(horizontal=True):
    st.page_link("app_pages/home.py" if origin == "home" else "app_pages/members.py",
                 label="라운지로" if origin == "home" else "회원 목록으로", icon=":material/arrow_back:")
if st.session_state.get("profile_database", core.db_path) != core.db_path:
    st.info("이용 공간이 변경되었습니다. 회원 목록에서 다시 선택해주세요.")
    st.stop()
try:
    member_id = integer(st.query_params.get("member", ""), "회원 번호")
    if member_id <= 0:
        raise ValueError("회원 목록에서 프로필을 선택해주세요.")
    render_profile(core, token, actor, member_id)
except (ValueError, PermissionError) as error:
    st.info(str(error))
except sqlite3.Error:
    st.error("프로필 정보를 불러오지 못했습니다. 잠시 후 다시 확인해주세요.")
