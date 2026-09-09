"""Read-only preparation cards: saved rosters with identity-bound Riot cache."""
from contextlib import closing
import html
from pathlib import Path
import sqlite3

import streamlit as st

from .auction_components import player_data
from .core import identity
from .riot_profile import public_profile
from .member_ranks import RANK_SELECT, RANK_JOINS, profile_projection
from .ui import ROLE_NAMES


STYLES = Path(__file__).resolve().parents[1] / "static" / "preparation_teams.css"


def cached_team_profiles(core, event):
    """One database read; a renamed member cannot lend a new identity's cache."""
    people = [player for team in event["teams"] for player in team["players"]]
    member_ids = list(dict.fromkeys(player["member_id"] for player in people))
    if not member_ids or core is None:
        return {}
    marks = ",".join("?" for _ in member_ids)
    with closing(core.connect()) as db:
        rows = db.execute(f"SELECT m.id,m.canonical_id,rp.payload,{RANK_SELECT} FROM members m JOIN riot_profiles rp "
                          f"ON rp.member_id=m.id AND rp.canonical_id=m.canonical_id {RANK_JOINS} "
                          f"WHERE m.id IN ({marks}) AND m.status='APPROVED'", member_ids).fetchall()
    saved = {row["id"]: row for row in rows}
    profiles = {}
    for player in people:
        row = saved.get(player["member_id"])
        if row is None:
            continue
        try:
            matching = identity(player["riot_id"])[1] == row["canonical_id"]
        except ValueError:
            matching = False
        profile = public_profile(profile_projection(row["payload"], row)) if matching else None
        if profile:
            profiles[player["member_id"]] = profile
    return profiles


def preparation_cards_data(event, profiles=None):
    profiles = profiles or {}
    cards = []
    for index, team in enumerate(event["teams"]):
        people = []
        for player in sorted(team["players"], key=lambda p: (p["member_id"] != team["captain_id"],
                                                            list(ROLE_NAMES).index(p["role"]), p["member_id"])):
            current = dict(player, riot_profile=public_profile(profiles.get(player["member_id"])))
            person = player_data(current)
            person.update(member_id=player["member_id"], role_code=player["role"],
                          main_role_label=ROLE_NAMES.get(player.get("main_role_snapshot"), "기록 없음"),
                          sub_role_label=ROLE_NAMES.get(player.get("sub_role_snapshot"), "기록 없음"),
                          captain=player["member_id"] == team["captain_id"])
            people.append(person)
        cards.append({"id": team["id"], "number": index + 1, "name": team["name"], "people": people,
                      "count": len(people), "points": f"{team['remaining']:,} P",
                      "power": f"{team['total_score']:g} P", "empty": max(0, 5 - len(people)),
                      "slots": [{"code": code, "name": name, "occupied": any(p["role_code"] == code for p in people)}
                                for code, name in ROLE_NAMES.items()]})
    return cards


def _person_html(person):
    escape = html.escape
    icon = person["profile_icon_url"]
    avatar = (f'<img src="{escape(icon, quote=True)}" alt="소환사 아이콘" loading="lazy">' if icon else
              f'<span>{escape(person["nickname"][:1])}</span>')
    badge = '<span class="prep-captain-badge">팀장</span>' if person["captain"] else ''
    champions = []
    for champion in person["champions"]:
        title = escape(champion["tooltip"], quote=True)
        visual = (f'<img src="{escape(champion["icon_url"], quote=True)}" alt="{escape(champion["name"], quote=True)}" loading="lazy">'
                  if champion["icon_url"] else f'<span>{escape(champion["name"][:2])}</span>')
        champions.append(f'<span class="prep-mastery" title="{title}">{visual}</span>')
    mastery = ''.join(champions) or '<span class="prep-mastery-empty">숙련도 정보 없음</span>'
    return f'''<article class="prep-person" data-member-id="{int(person['member_id'])}">
      <div class="prep-person-header"><div class="prep-avatar">{avatar}</div><div class="prep-person-info">
        <div class="prep-person-name"><strong>{escape(person['nickname'])}</strong>{badge}</div>
        <div class="prep-riot-id" title="{escape(person['riot_id'], quote=True)}">{escape(person['riot_id'])}</div>
      </div></div>
      <div class="prep-rank-pills"><span title="{escape(person['profile_source'], quote=True)}">솔로 {escape(person['current_tier'])}</span><span>클랜 {escape(person['clan_tier'])}</span></div>
      <div class="prep-position-pills"><span class="prep-main-role">주 {escape(person['main_role_label'])}</span><span class="prep-sub-role">부 {escape(person['sub_role_label'])}</span></div>
      <div class="prep-masteries" aria-label="저장된 숙련도 상위 5개">{mastery}</div>
    </article>'''


def team_cards_html(cards):
    escape = html.escape
    rendered = []
    for card in cards:
        people = ''.join(_person_html(person) for person in card["people"])
        rendered.append(f'''<section class="prep-team prep-team-{(card['number'] - 1) % 8 + 1}" data-team-id="{int(card['id'])}" aria-label="{escape(card['name'], quote=True)}">
          <header class="prep-team-header"><h3>Team {card['number']}</h3><span class="prep-count">{card['count']}/5</span></header>
          <div class="prep-team-values"><span>포인트 <strong>{escape(card['points'])}</strong></span><small>팀 전력 {escape(card['power'])}</small></div>
          <div class="prep-people">{people}</div>
        </section>''')
    return '<div class="roly-preparation-teams">' + ''.join(rendered) + '</div>'


def render_preparation_teams(event, core=None):
    if not event["teams"]:
        st.caption("팀장을 지정하면 참가 팀이 표시됩니다.")
        return
    try:
        profiles = cached_team_profiles(core, event)
    except sqlite3.Error:
        profiles = {}
        st.caption("저장된 Riot 정보를 불러오지 못해 명단 정보로 표시합니다.")
    st.html(STYLES)
    st.html(team_cards_html(preparation_cards_data(event, profiles)))
