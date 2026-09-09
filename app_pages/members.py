import streamlit as st
from roly.ui import context, heading, member_table, ROLE_NAMES, award_label
from roly.member_records import saved_tier
from roly.riot_profile import member_profiles

core, competition, token, actor = context()
heading("회원", "회원별 클랜·현재 티어, 포지션, 전력점수, 일반내전 전적과 우승 업적을 조회합니다.")
st.caption("주·부 포지션과 클랜 티어는 관리자가 직접 설정합니다. Riot 갱신은 현재 티어·전적·숙련도만 갱신하며 관리자 설정과 전력점수는 유지됩니다.")
with st.expander("점수·우승 업적 기준"):
    st.caption("전력점수 = 운영진이 확인한 기본점수 + 일반내전 증감 + 사유가 남는 운영진 보정. 승률과 판수는 확정된 일반내전만 포함합니다.")
    st.caption("경매 우승 업적은 전력점수와 별개입니다. 우승팀 5명에게 4팀 경매는 고양이 1개, 6팀은 별 1개, 8팀은 메달 1개를 각각 지급합니다.")
    st.caption("고양이 5개 = 별 1개 · 별 5개 = 메달 1개 · 메달 5개 = 트로피 1개. 경매 결과는 경기 기록의 경매 탭에서 확인할 수 있습니다.")
members = core.list_members()
profiles = member_profiles(core, [member["id"] for member in members])
for member in members:
    member["riot_profile"] = profiles.get(member["id"])
search = st.text_input("클랜원 검색", placeholder="닉네임 또는 Riot 태그로 검색", icon=":material/search:")
position = st.pills("주 포지션", ["전체"] + list(ROLE_NAMES), default="전체", format_func=lambda r: ROLE_NAMES.get(r, r))
filtered = [m for m in members if search.strip().casefold() in m["riot_id"].casefold() and (position in (None, "전체") or m["main_role"] == position)]
can_edit = bool(actor and actor["role"] == "admin")
with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    st.markdown(f"**회원 {len(filtered)}명**")
    table = member_table(filtered)
    table["주력 챔피언"] = [", ".join(champion["name"] for champion in (member.get("riot_profile") or {}).get("champions", [])) or "미조회" for member in filtered]
    st.download_button("목록 다운로드", core.csv_bytes(table.to_dict("records")), "클랜원.csv", "text/csv", icon=":material/download:")
if filtered:
    widths = [2.1, 0.7, 0.7, 1.05, 1.3, 0.85, 1.25, 1.05, 3.0 if can_edit else 1.5]
    labels = ("회원", "주포지션", "부포지션", "클랜 티어", "현재 티어", "전력점수", "일반내전", "우승 기호", "프로필 · 관리" if can_edit else "프로필")
    for column, label in zip(st.columns(widths, gap="small"), labels):
        column.caption(label)
    with st.container(height=min(620, max(100, len(filtered) * 72 + 20)),
                      border=True, gap="small", key="member_list_rows"):
        for member in filtered:
            row = st.columns(widths, gap="small", vertical_alignment="center")
            row[0].text(member["riot_id"])
            row[1].text(ROLE_NAMES[member["main_role"]])
            row[2].text(ROLE_NAMES[member["sub_role"]])
            row[3].text(saved_tier(member["clan_tier"]))
            row[4].text(saved_tier(member["current_tier"], member["current_tier_lp"]))
            row[5].text(f"{member['score']:,} P")
            games = member["wins"] + member["losses"]
            record = f"{member['wins']}승 {member['losses']}패"
            if games:
                record += f" · {member['wins'] / games:.0%}"
            row[6].text(record)
            row[7].text(award_label(member))
            with row[8].container(horizontal=True, wrap=False, gap="xsmall"):
                if st.button("프로필 상세보기", key=f"member_profile_{member['id']}", width="stretch"):
                    from roly.member_profile_page import open_member_profile
                    open_member_profile(member["id"], origin="members")
                if can_edit and st.button("회원 정보 수정", key=f"member_edit_{member['id']}",
                                          icon=":material/edit:", width="stretch",
                                          help="주 포지션·부 포지션·클랜 티어를 직접 수정합니다."):
                    from roly.member_editor import show_member_editor
                    show_member_editor(core, token, actor, member["id"])
else:
    st.caption("조건에 맞는 회원이 없습니다.")
