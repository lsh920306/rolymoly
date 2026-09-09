"""Shared profile destination for explicit row actions and lounge cards."""
import html

import pandas as pd
import streamlit as st

from .core import integer
from .member_records import korean_time, member_records, saved_tier
from .riot_profile import member_profiles
from .ui import ROLE_NAMES, STATUS, award_label


def open_member_profile(member_id, *, origin="members"):
    member_id = integer(member_id, "회원 번호")
    if member_id <= 0:
        raise ValueError("회원 번호를 확인해주세요.")
    st.session_state.profile_origin = origin if origin in ("members", "home") else "members"
    st.session_state.profile_database = st.session_state.db_path
    st.switch_page("app_pages/profile.py", query_params={"member": str(member_id)})


def profile_header(member, profile):
    name, _, tag = member["riot_id"].partition("#")
    escape = html.escape
    icon = profile.get("profile_icon_url") if profile else ""
    avatar = f'<img src="{escape(icon, quote=True)}" alt="소환사 프로필">' if icon else f'<span>{escape(name[:1])}</span>'
    tier = saved_tier(profile["current_tier"], profile.get("lp")) if profile else (saved_tier(member["current_tier"], member["current_tier_lp"]) if member["current_tier"] else "미입력")
    rank = "Riot 조회 전 · 수기 입력" if not profile else "한국 서버"
    level = f"레벨 {profile['summoner_level']}" if profile and "summoner_level" in profile else ""
    record = ""
    if profile and "rank_wins" in profile and "rank_losses" in profile:
        wins, losses = profile["rank_wins"], profile["rank_losses"]
        total = wins + losses
        record = f"{total}경기 · {wins}승 {losses}패" + (f" · {wins / total:.0%}" if total else "")
    flex_tier = saved_tier(profile["flex_current_tier"], profile.get("flex_lp")) if profile and profile.get("flex_current_tier") else "미입력"
    flex_record = ""
    if profile and "flex_rank_wins" in profile and "flex_rank_losses" in profile:
        wins, losses = profile["flex_rank_wins"], profile["flex_rank_losses"]
        total = wins + losses
        flex_record = f"{total}경기 · {wins}승 {losses}패" + (f" · {wins / total:.0%}" if total else "")
    return f'''<div class="roly-member-profile">
      <div class="profile-identity"><div class="profile-avatar">{avatar}</div><div>
        <h1>{escape(name)} <span>#{escape(tag)}</span></h1><div class="profile-meta">{level}</div>
        <div class="profile-meta">{escape(ROLE_NAMES[member['main_role']])} / {escape(ROLE_NAMES[member['sub_role']])}</div>
      </div></div>
      <div class="profile-ranks">
        <div class="profile-rank"><div class="profile-kicker">솔로랭크</div><strong>{escape(tier)}</strong><div class="profile-meta">{record}</div><div class="profile-meta">{rank}</div></div>
        <div class="profile-rank"><div class="profile-kicker">자유랭크</div><strong>{escape(flex_tier)}</strong><div class="profile-meta">{flex_record}</div></div>
      </div>
    </div>'''


def render_profile(core, token, actor, member_id):
    member = core.get_member(member_id)
    if member["status"] != "APPROVED":
        raise ValueError("현재 활동 중인 회원의 프로필만 조회할 수 있습니다.")
    profile = member_profiles(core, [member_id]).get(member_id)
    st.html(profile_header(member, profile))
    with st.container(horizontal=True, vertical_alignment="center"):
        from .riot_ui import refresh_control
        refresh_control(core, token, [member_id], key=f"profile_{member_id}", label="갱신하기")
        from .self_profile_ui import nickname_change_control
        nickname_change_control(core, token, actor, member_id, key=f"profile_nickname_{member_id}")
        if actor and actor["role"] == "admin":
            if st.button("회원 정보 수정", key=f"profile_edit_{member_id}", icon=":material/edit:"):
                from .member_editor import show_member_editor
                show_member_editor(core, token, actor, member_id)
    st.caption(f"클랜 티어 {member['clan_tier'] or '미입력'} · 전력점수 {member['score']:,} P · 일반내전 {member['wins']}승 {member['losses']}패 · 우승 기호 {award_label(member)}")
    with st.container(border=True, key="profile_champions"):
        st.subheader("주력 챔피언")
        st.caption("챔피언 숙련도 상위 5개 · 경기별 승률·KDA와 다른 지표입니다.")
        if profile and profile["champions"]:
            rows = [{"챔피언 이미지": champion["icon_url"] or None, "챔피언": champion["name"],
                     "숙련도 점수": champion["points"], "숙련도 레벨": champion["level"]} for champion in profile["champions"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", row_height=62,
                         column_config={"챔피언 이미지": st.column_config.ImageColumn("", width="small"),
                                        "숙련도 점수": st.column_config.NumberColumn(format="localized"),
                                        "숙련도 레벨": st.column_config.NumberColumn(format="%d")})
        else:
            st.caption("아직 저장된 Riot 정보가 없습니다." if not profile else "아직 챔피언 숙련도 기록이 없습니다.")
    with st.expander("일반내전·경매 기록"):
        data = member_records(core, member_id)
        st.caption(f"명단 등록 {len(data['tournaments'])}개 · 확정 경기 {len(data['games'])}판")
        if data["games"]:
            st.dataframe([{"일시": korean_time(game["played_at"]), "구분": "일반내전" if game["kind"] == "NORMAL" else "경매",
                           "내전": game["title"] or "개별 경기", "결과": "승" if game["team"] == game["winner"] else "패",
                           "점수 증감": game["delta"]} for game in data["games"][:50]], hide_index=True)
        else:
            st.caption("아직 확정된 경기 기록이 없습니다.")
        if data["tournaments"]:
            st.markdown("**팀·낙찰 이력**")
            st.dataframe([
                {"구분": "일반내전" if item["kind"] == "NORMAL" else "경매",
                 "내전·경매": item["title"], "상태": STATUS.get(item["status"], item["status"]),
                 "팀": item["team_name"] or "미배정",
                 "역할": "팀장" if item["captain_id"] == member_id else "선수",
                 "당시 Riot ID": item["riot_id"],
                 "당시 클랜 티어": saved_tier(item["clan_tier_snapshot"]),
                 "당시 현재 티어": saved_tier(item["current_tier_snapshot"], item["current_tier_lp_snapshot"]),
                 "포지션": ROLE_NAMES[item["role"]], "당시 전력": item["score"],
                 "낙찰 포인트": f"{item['price']:,} P" if item["kind"] == "AUCTION" and item["captain_id"] != member_id and item["team_name"] else "—"}
                for item in data["tournaments"]
            ], hide_index=True, width="stretch")
            st.caption("당시 이름·티어·전력과 팀 배정은 저장된 명단을 표시합니다. 일반내전·팀장·미배정 선수에게 낙찰 포인트를 표시하지 않습니다.")
