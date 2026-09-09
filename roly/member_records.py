"""Public member history assembled from saved games and roster snapshots."""
from contextlib import closing
from datetime import datetime, timedelta, timezone

import streamlit as st

from roly.ui import ROLE_NAMES, STATUS, award_label


def korean_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=9))).strftime("%Y.%m.%d %H:%M")


def saved_tier(value, lp=None):
    if value is None:
        return "기록 없음"
    if not value:
        return "미등록"
    return f"{value} · {lp} LP" if lp is not None else str(value)


def member_records(core, member_id):
    with closing(core.connect()) as db:
        db.execute("BEGIN")
        member = core.get_member(member_id, conn=db)
        if member["status"] != "APPROVED":
            raise ValueError("현재 활동 중인 회원의 기록만 조회할 수 있습니다.")
        games = [dict(row) for row in db.execute("""
            SELECT g.id,g.kind,g.played_at,g.winner,p.team,p.delta,p.score_before,
                   p.riot_id_snapshot AS riot_id,p.clan_tier_snapshot,p.current_tier_snapshot,p.current_tier_lp_snapshot,
                   e.title,ta.name AS team_a_name,tb.name AS team_b_name
            FROM game_players p JOIN games g ON g.id=p.game_id
            LEFT JOIN competition_games cg ON cg.core_game_id=g.id
            LEFT JOIN competition_events e ON e.id=cg.event_id
            LEFT JOIN competition_teams ta ON ta.id=cg.team_a
            LEFT JOIN competition_teams tb ON tb.id=cg.team_b
            WHERE p.member_id=? AND g.status='CONFIRMED' ORDER BY g.played_at DESC,g.id DESC
        """, (member_id,))]
        tournaments = [dict(row) for row in db.execute("""
            SELECT e.id,e.title,e.kind,e.status,e.created_at,t.name AS team_name,
                   p.role,p.score,p.price,t.captain_id,p.riot_id,
                   p.clan_tier_snapshot,p.current_tier_snapshot,p.current_tier_lp_snapshot
            FROM competition_players p JOIN competition_events e ON e.id=p.event_id
            LEFT JOIN competition_teams t ON t.id=p.team_id
            WHERE p.member_id=? AND p.participation_status='SELECTED' ORDER BY e.id DESC
        """, (member_id,))]
    # Explicit public projection: account links and private operational notes stay out.
    return {
        "member": {key: member[key] for key in ("id", "riot_id", "main_role", "sub_role", "score", "cats", "stars", "medals", "trophies",
                    "clan_tier", "current_tier", "current_tier_lp")},
        "games": games, "tournaments": tournaments,
        "wins": sum(game["team"] == game["winner"] for game in games),
        "losses": sum(game["team"] != game["winner"] for game in games),
    }


@st.dialog("회원 기록", width="large")
def show_member_record(core, member_id):
    data = member_records(core, member_id)
    member = data["member"]
    st.subheader(member["riot_id"])
    st.caption(f"{ROLE_NAMES[member['main_role']]} / {ROLE_NAMES[member['sub_role']]} · 전력점수 {member['score']:,} P · 우승 기호 {award_label(member)}")
    st.caption(f"클랜 티어 {saved_tier(member['clan_tier'])} · 현재 티어 {saved_tier(member['current_tier'], member['current_tier_lp'])}")
    from .riot_ui import refresh_control
    # The nested fragment refreshes its own saved data without closing this
    # dialog or rerunning the surrounding lounge/member page.
    refresh_control(core, st.session_state.get("token"), [member_id],
                    key=f"record_{member_id}", show_details=True, rerun_on_update=False)
    st.write(f"내전·경매 명단 등록 {len(data['tournaments'])}개 · 확정 경기 {len(data['games'])}판 · {data['wins']}승 {data['losses']}패")
    if data["games"]:
        st.markdown("**최근 경기**")
        st.dataframe([
            {"일시 (한국 시간)": korean_time(game["played_at"]), "내전·경매": game["title"] or "개별 경기",
             "구분": "일반내전" if game["kind"] == "NORMAL" else "경매",
             "당시 Riot ID": game["riot_id"] or "기록 없음",
             "당시 클랜 티어": saved_tier(game["clan_tier_snapshot"]),
             "당시 현재 티어": saved_tier(game["current_tier_snapshot"], game["current_tier_lp_snapshot"]),
             "대진": f"{game['team_a_name'] or 'A팀'} vs {game['team_b_name'] or 'B팀'}",
             "결과": "승" if game["team"] == game["winner"] else "패", "점수 증감": game["delta"]}
            for game in data["games"][:50]
        ], hide_index=True)
    else:
        st.caption("아직 확정된 경기 기록이 없습니다.")
    if data["tournaments"]:
        st.markdown("**팀·낙찰 이력**")
        st.dataframe([
            {"내전·경매": item["title"], "상태": STATUS.get(item["status"], item["status"]), "팀": item["team_name"] or "미배정",
             "당시 Riot ID": item["riot_id"], "당시 클랜 티어": saved_tier(item["clan_tier_snapshot"]),
             "당시 현재 티어": saved_tier(item["current_tier_snapshot"], item["current_tier_lp_snapshot"]),
             "역할": "팀장" if item["captain_id"] == member_id else "선수",
             "포지션": ROLE_NAMES[item["role"]], "당시 전력": item["score"],
             "낙찰가": item["price"] if item["kind"] == "AUCTION" and item["captain_id"] != member_id and item["team_name"] else None}
            for item in data["tournaments"]
        ], hide_index=True)
    st.caption("확정된 일반내전과 경매 경기를 함께 표시합니다. 회원표의 일반내전 승률과 집계 범위가 다릅니다.")
    st.caption("당시 이름·티어는 저장된 참가·경기 기록을 표시합니다. 과거에 저장하지 않은 티어는 현재 값으로 채우지 않습니다.")
