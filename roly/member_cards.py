"""Isolated, presentation-only member cards for the lounge's filtered list."""
from __future__ import annotations

from math import isfinite
from uuid import uuid4

import streamlit as st


ROLE_NAMES = {"TOP": "탑", "JG": "정글", "MID": "미드", "AD": "원딜", "SUP": "서포터"}

HTML = """
<ul class="member-list" aria-label="클랜원 목록"></ul>
"""

CSS = """
:host {
  display: block;
  width: 100%;
  min-width: 0;
  font-family: var(--st-font, "Pretendard", sans-serif);
  color: #142238;
}
*, *::before, *::after { box-sizing: border-box; }
.member-list {
  list-style: none;
  height: auto;
  max-height: 480px;
  margin: 0;
  padding: 2px 8px 2px 2px;
  overflow: auto;
  overflow-x: hidden;
  scrollbar-width: thin;
  scrollbar-color: #ebcbd8 transparent;
}
.member-list::-webkit-scrollbar { width: 6px; }
.member-list::-webkit-scrollbar-track { background: transparent; }
.member-list::-webkit-scrollbar-thumb { background: #ebcbd8; border-radius: 6px; }
.member-item { list-style: none; margin: 0 0 8px; padding: 0; }
.member-item:last-child { margin-bottom: 0; }
.member-card {
  appearance: none;
  -webkit-appearance: none;
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  align-items: center;
  column-gap: 12px;
  width: 100%;
  min-width: 0;
  min-height: 84px;
  margin: 0;
  padding: 12px;
  border: 1px solid #f9e6e6;
  border-radius: 16px;
  background: #fff;
  color: #142238;
  font-family: inherit;
  font-size: 15px;
  font-weight: 400;
  line-height: 20px;
  letter-spacing: normal;
  text-align: left;
  text-transform: none;
  cursor: pointer;
}
.member-card:hover { border-color: #ff9f9c; }
.member-card:focus-visible { outline: 2px solid #e57672; outline-offset: 1px; }
.member-avatar {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: #ff9f9c;
  color: #76282b;
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
}
.member-text {
  display: grid;
  grid-template-rows: 20px 18px 18px;
  row-gap: 3px;
  min-width: 0;
  text-align: left;
}
.member-name, .member-meta, .member-record {
  display: block;
  min-width: 0;
  margin: 0;
  padding: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  text-align: left;
}
.member-name { font-size: 15px; font-weight: 700; line-height: 20px; }
.member-meta, .member-record { font-size: 13px; font-weight: 400; line-height: 19px; color: #536176; }
.member-empty { padding: 12px; font-size: 13px; line-height: 19px; color: #536176; }
"""

JS = """
export default function (component) {
  const { parentElement, data, setTriggerValue } = component;
  const list = parentElement.querySelector('.member-list');
  if (!list) return;
  const doc = list.ownerDocument;
  const members = Array.isArray(data?.members)
    ? data.members.filter(member => Number.isSafeInteger(member.member_id) && member.member_id > 0)
    : [];
  const signature = members.map(member => member.member_id).join(',');
  const sameMembers = list.dataset.members === signature;
  const scrollTop = sameMembers ? list.scrollTop : 0;
  const focusedId = parentElement.activeElement?.dataset?.memberId;
  const buttons = [];
  const items = members.map(member => {
    const item = doc.createElement('li');
    item.className = 'member-item';
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'member-card';
    button.dataset.memberId = String(member.member_id);
    button.title = String(member.riot_id);
    button.setAttribute('aria-label', `${member.riot_id} 회원 기록 · ${member.meta} · ${member.record}`);
    const avatar = doc.createElement('span');
    avatar.className = 'member-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.textContent = String(member.initial);
    const content = doc.createElement('span');
    content.className = 'member-text';
    for (const [className, value] of [
      ['member-name', member.name],
      ['member-meta', member.meta],
      ['member-record', member.record]
    ]) {
      const line = doc.createElement('span');
      line.className = className;
      line.textContent = String(value);
      line.title = String(value);
      content.appendChild(line);
    }
    button.appendChild(avatar);
    button.appendChild(content);
    button.onclick = () => setTriggerValue('selected', member.member_id);
    item.appendChild(button);
    buttons.push(button);
    return item;
  });
  if (!items.length) {
    const empty = doc.createElement('li');
    empty.className = 'member-empty';
    empty.textContent = '조건에 맞는 회원이 없습니다.';
    items.push(empty);
  }
  list.replaceChildren(...items);
  list.dataset.members = signature;
  list.scrollTop = scrollTop;
  if (sameMembers && focusedId) {
    buttons.find(button => button.dataset.memberId === focusedId)?.focus({ preventScroll: true });
  }
  return () => { buttons.forEach(button => { button.onclick = null; }); };
}
"""


@st.cache_resource(scope="session", show_spinner=False)
def _register_member_cards(session_key: str):
    # Register once on first use in the current Streamlit session. Deferring the
    # declaration avoids binding to the bare-mode registry during a data-only
    # import, and session scope also supports independent AppTest runtimes.
    return st.components.v2.component("lounge_member_cards", html=HTML, css=CSS, js=JS, isolate_styles=True)


def _member_cards_renderer():
    # AppTest reuses a session ID across independent session states. A small
    # serializable nonce keeps their cached declarations separate as well.
    if "_member_cards_scope" not in st.session_state:
        st.session_state["_member_cards_scope"] = str(uuid4())
    return _register_member_cards(st.session_state["_member_cards_scope"])


def _count(member, field):
    value = member.get(field, 0)
    if type(value) is not int or value < 0:
        raise ValueError("회원 전적과 업적 수를 확인해 주세요.")
    return value


def card_data(members):
    """Whitelist public display fields; never forward notes or account data."""
    members = list(members)
    cards, seen = [], set()
    for member in members:
        member_id = member.get("id")
        if type(member_id) is not int or not 0 < member_id <= 9007199254740991 or member_id in seen:
            raise ValueError("서로 다른 유효한 회원 번호를 전달해 주세요.")
        seen.add(member_id)
        riot_id = str(member.get("riot_id", "")).strip()
        if not riot_id:
            raise ValueError("회원 Riot ID를 확인해 주세요.")
        score = member.get("score")
        if type(score) not in (int, float) or not isfinite(score):
            raise ValueError("회원 전력점수를 확인해 주세요.")
        role = member.get("main_role")
        if role not in ROLE_NAMES:
            raise ValueError("회원의 주 포지션을 확인해 주세요.")
        awards = []
        for field, symbol in (("trophies", "🏆"), ("medals", "🏅"), ("stars", "⭐"), ("cats", "🐱")):
            count = _count(member, field)
            if count:
                awards.append(symbol * count if count <= 4 else f"{symbol} × {count}")
        name = riot_id.split("#", 1)[0]
        cards.append({"member_id": member_id, "riot_id": riot_id, "name": name,
            "initial": name[:1].upper(), "meta": f"{ROLE_NAMES[role]} · 전력 {score:,g} P",
            "record": f"일반내전 {_count(member, 'wins') + _count(member, 'losses'):,}판 · {' '.join(awards) or '업적 없음'}"})
    return cards


def render_member_cards(members, key):
    """Render all filtered members and return a clicked ID from this list, or None."""
    cards = card_data(members)
    result = _member_cards_renderer()(data={"members": cards}, key=key, width="stretch", height="content", on_selected_change=lambda: None)
    selected = result.selected
    return selected if type(selected) is int and selected in {card["member_id"] for card in cards} else None
