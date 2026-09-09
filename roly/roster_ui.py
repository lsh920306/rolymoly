"""Shared searchable roster editor for normal matches and auction recruitment."""
from hashlib import sha256
import re

import streamlit as st

from roly.ui import ROLE_NAMES
from roly.core import identity


MATCH_LABELS = {"MATCHED": "일치", "DUPLICATE": "명단 중복", "UNMATCHED": "등록 회원 없음",
                "UNAPPROVED": "승인 전·탈퇴 회원", "INVALID": "Riot ID 형식 오류", "AMBIGUOUS": "회원 정보 중복 확인 필요"}
MATCH_REASONS = {
    "MATCHED": "승인 회원의 Riot ID와 정확히 일치합니다.",
    "DUPLICATE": "앞선 줄에 같은 Riot ID가 있습니다.",
    "UNMATCHED": "추출한 Riot ID로 등록된 회원을 찾지 못했습니다.",
    "UNAPPROVED": "가입 승인 전이거나 탈퇴한 회원입니다.",
    "INVALID": "첫 / 앞에 닉네임#태그를 입력해 주세요.",
    "AMBIGUOUS": "일치하는 회원 정보를 하나로 정할 수 없습니다. Riot ID를 확인해 주세요.",
}


def match_roster_text(members, text):
    """Extract numbered Kakao roster IDs; ignore every field after the first slash.

    Exact registry matches only. Tiers, roles, availability and other notes are
    never used to create or update a member or their assignment.
    """
    if not isinstance(text, str) or len(text) > 20000 or len(text.splitlines()) > 200:
        raise ValueError("명단은 200줄, 20,000자 이내로 입력해 주세요.")
    by_canonical = {}
    for member in members:
        try:
            _, canonical = identity(member["riot_id"])
        except (KeyError, ValueError):
            continue
        by_canonical.setdefault(canonical, []).append(member)
    rows, matched, seen = [], [], set()
    for line_number, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        head = raw.split("/", 1)[0].strip()
        candidate = re.sub(r"^[0-9]+[.)]\s*", "", head, count=1)
        row = {"line": line_number, "input": raw, "extracted_id": candidate,
               "status": "INVALID", "member_id": None, "riot_id": "", "reason": MATCH_REASONS["INVALID"]}
        try:
            if any(ord(char) < 32 for char in head):
                raise ValueError("제어 문자")
            # A literal nickname such as '1.Name' may also be registered. Keep
            # an exact literal match, or ask for clarification when both the
            # literal and the number-stripped ID identify registered members.
            try:
                literal = identity(head)
            except ValueError:
                literal = None
            try:
                extracted = identity(candidate)
            except ValueError:
                if literal is None or literal[1] not in by_canonical:
                    raise
                extracted = literal
            if candidate != head and literal is not None and literal[1] in by_canonical:
                if extracted[1] != literal[1] and extracted[1] in by_canonical:
                    row.update(status="AMBIGUOUS", reason="순번을 포함한 닉네임과 순번을 뺀 닉네임이 모두 등록되어 있습니다. 검색에서 정확한 회원을 선택해 주세요.")
                    rows.append(row)
                    continue
                extracted = literal
            display, canonical = extracted
            nickname, tag = display.split("#", 1)
            row["extracted_id"] = f"{nickname}#{tag.upper()}"
        except ValueError:
            rows.append(row)
            continue
        if canonical in seen:
            row["status"] = "DUPLICATE"
        else:
            seen.add(canonical)
            found = by_canonical.get(canonical, [])
            if not found:
                row["status"] = "UNMATCHED"
            elif len(found) != 1:
                row["status"] = "AMBIGUOUS"
            elif found[0].get("status", "APPROVED") != "APPROVED":
                row.update(status="UNAPPROVED", riot_id=found[0]["riot_id"])
            else:
                member = found[0]
                row.update(status="MATCHED", member_id=member["id"], riot_id=member["riot_id"])
                matched.append(member["id"])
        row["reason"] = MATCH_REASONS[row["status"]]
        rows.append(row)
    return {"rows": rows, "member_ids": matched}


def _save_role_edits(editor_key, selected_ids, roles_key):
    roles = dict(st.session_state.get(roles_key, {}))
    edits = st.session_state.get(editor_key, {}).get("edited_rows", {})
    for index, changes in edits.items():
        index = int(index)
        role = changes.get("배정 포지션")
        if 0 <= index < len(selected_ids) and role in ROLE_NAMES:
            roles[selected_ids[index]] = role
    st.session_state[roles_key] = roles


def roster_editor(members, *, key, team_count, saved=(), strict_roles=True, matching_members=None, revision=None):
    """Return draft assignments. Storage and authorization stay in the services."""
    by_id = {member["id"]: member for member in members if member.get("status", "APPROVED") == "APPROVED"}
    signature = tuple((item["member_id"], item["role"]) for item in saved)
    roles_key = f"{key}_draft_roles"
    selected_key = f"{key}_members"
    if st.session_state.get(f"{key}_saved") != signature or st.session_state.get(f"{key}_revision") != revision:
        st.session_state[f"{key}_saved"] = signature
        st.session_state[f"{key}_revision"] = revision
        st.session_state[selected_key] = [member_id for member_id, _ in signature if member_id in by_id]
        st.session_state[roles_key] = dict(signature)
        st.session_state.pop(f"{key}_base_signature", None)
    # Removed/deactivated members cannot remain as invisible selections.
    current = st.session_state.get(selected_key, [])
    if any(member_id not in by_id for member_id in current):
        st.session_state[selected_key] = [member_id for member_id in current if member_id in by_id]
    with st.expander("카카오톡 명단 붙여넣기 (선택)", expanded=True):
        pasted = st.text_area("Riot ID 명단", placeholder="1. 겨울#kr99 / d2 / ad mid\n2. Kging#kr1 / M / mid jg top\n닉네임#태그만 한 줄씩 입력해도 됩니다.", height=160, max_chars=20000, key=f"{key}_paste_text")
        st.caption("순번과 첫 / 뒤 메모를 빼고 Riot ID만 확인합니다. 티어·포지션은 DB 회원 정보를 사용하며, 정확히 일치하는 승인 회원만 선택에 추가합니다.")
        if pasted.strip():
            try:
                preview = match_roster_text(matching_members if matching_members is not None else members, pasted)
            except ValueError as error:
                st.error(str(error))
            else:
                st.dataframe([{"줄": row["line"], "원문": row["input"], "추출 Riot ID": row["extracted_id"],
                               "확인": MATCH_LABELS[row["status"]], "일치 회원": row["riot_id"], "사유": row["reason"]}
                              for row in preview["rows"]], hide_index=True)
                valid_ids = [mid for mid in preview["member_ids"] if mid in by_id]
                merged = list(dict.fromkeys([*st.session_state.get(selected_key, []), *valid_ids]))
                exceeds = len(merged) > team_count * 5
                if exceeds:
                    st.warning(f"현재 선택과 합치면 {len(merged)}명입니다. 정원 {team_count * 5}명 이내로 정리해 주세요.")
                if st.button(f"일치한 {len(valid_ids)}명 선택에 추가", disabled=not valid_ids or exceeds, key=f"{key}_paste_apply") and valid_ids and not exceeds:
                    # This is still before the multiselect is instantiated.
                    st.session_state[selected_key] = merged
    selected = st.multiselect(
        "참가 회원 검색·선택", sorted(by_id, key=lambda mid: by_id[mid]["riot_id"].casefold()),
        key=selected_key, max_selections=team_count * 5,
        format_func=lambda mid: by_id[mid]["riot_id"],
        placeholder="닉네임 또는 Riot ID로 검색해 참가자를 선택하세요",
    )
    selection = tuple(selected)
    display_signature = tuple((mid, by_id[mid]["riot_id"], by_id[mid]["score"], by_id[mid].get("clan_tier"), by_id[mid].get("current_tier"), by_id[mid].get("current_tier_lp")) for mid in selected)
    roles = st.session_state.get(roles_key, {})
    if st.session_state.get(f"{key}_base_signature") != display_signature:
        st.session_state[f"{key}_base_signature"] = display_signature
        st.session_state[f"{key}_base_rows"] = [
            {"선수": by_id[mid]["riot_id"], "배정 포지션": roles.get(mid, by_id[mid]["main_role"]),
             "클랜 티어": by_id[mid].get("clan_tier") or "미입력", "현재 티어": by_id[mid].get("current_tier") or "미입력",
             "전력점수": by_id[mid]["score"]}
            for mid in selected
        ]
    if selected:
        st.caption("주 포지션으로 먼저 배정합니다. 바꿀 선수의 배정 포지션만 표에서 수정하세요.")
        editor_key = f"{key}_positions_{sha256(repr(display_signature).encode()).hexdigest()[:12]}"
        rows = st.data_editor(
            st.session_state[f"{key}_base_rows"], key=editor_key, hide_index=True,
            disabled=["선수", "클랜 티어", "현재 티어", "전력점수"], height=min(390, 38 + 35 * len(selected)),
            column_config={"배정 포지션": st.column_config.SelectboxColumn(
                "배정 포지션", options=list(ROLE_NAMES), format_func=ROLE_NAMES.get, required=True),
                "전력점수": st.column_config.NumberColumn(format="%d P")},
            on_change=_save_role_edits, args=(editor_key, selection, roles_key),
        )
        assignments = [{"member_id": mid, "role": row["배정 포지션"]} for mid, row in zip(selected, rows)]
    else:
        assignments = []
        st.caption("선택한 회원이 여기에 모입니다.")
    counts = {role: sum(item["role"] == role for item in assignments) for role in ROLE_NAMES}
    st.markdown(f"**선택 {len(assignments)} / {team_count * 5}명**")
    st.caption(" · ".join(f"{label} {counts[role]}/{team_count}" if strict_roles else f"{label} {counts[role]}명" for role, label in ROLE_NAMES.items()))
    if strict_roles:
        missing = [f"{ROLE_NAMES[role]} {team_count - count}명" for role, count in counts.items() if count < team_count]
        excess = [f"{ROLE_NAMES[role]} {count - team_count}명" for role, count in counts.items() if count > team_count]
        if missing or excess:
            st.caption(" · ".join((["부족: " + ", ".join(missing)] if missing else []) + (["초과: " + ", ".join(excess)] if excess else [])))
    return assignments
