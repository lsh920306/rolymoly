"""Administrator tools for member care, policies, accounts, and audit history."""
from uuid import uuid4
from hashlib import sha256

import streamlit as st

from roly.core import ROLES
from roly.ui import context, heading, perform, require_staff
from roly.member_editor import member_edit_form


TIER_POINTS = {
    "언랭크": 0,
    **{f"아이언 {division}": 10 for division in (4, 3, 2, 1)},
    **{f"브론즈 {division}": points for division, points in zip((4, 3, 2, 1), (20, 30, 40, 50))},
    **{f"실버 {division}": points for division, points in zip((4, 3, 2, 1), (60, 70, 80, 90))},
    **{f"골드 {division}": points for division, points in zip((4, 3, 2, 1), (120, 130, 140, 150))},
    **{f"플래티넘 {division}": points for division, points in zip((4, 3, 2, 1), (200, 210, 220, 230))},
    **{f"에메랄드 {division}": points for division, points in zip((4, 3, 2, 1), (280, 300, 320, 340))},
    **{f"다이아몬드 {division}": points for division, points in zip((4, 3, 2, 1), (390, 420, 450, 480))},
    "마스터 0~99 LP": 550,
    "마스터 100~199 LP": 600,
    "마스터 200 LP 이상": 700,
    "그랜드마스터": 800,
    "챌린저": 1000,
}
FLEX_TIERS = ["언랭크"] + [
    f"{tier} {division}"
    for tier in ("아이언", "브론즈", "실버", "골드", "플래티넘", "에메랄드", "다이아몬드")
    for division in (4, 3, 2, 1)
] + ["마스터", "그랜드마스터", "챌린저"]
STATUS_LABELS = {"PENDING": "승인 대기", "APPROVED": "활동 중", "KICKED": "탈퇴"}
ACCOUNT_ROLES = {"organizer": "진행자", "admin": "관리자", "member": "회원"}
AUDIT_ACTIONS = {
    "ADMIN_SETUP": "최초 관리자 설정", "LOGIN": "로그인", "ACCOUNT_CREATE": "계정 생성",
    "ACCOUNT_ROLE": "계정 권한 변경", "MEMBER_JOIN": "가입 신청", "MEMBER_APPROVE": "회원 승인",
    "MEMBER_KICK": "회원 탈퇴", "MEMBER_RESTORE": "회원 복귀", "MEMBER_UPDATE": "회원 정보 수정", "SCORE_ADJUST": "점수 보정",
    "POLICY_CREATE": "점수 정책 변경", "GAME_RECORD": "경기 결과 확정", "GAME_CORRECT": "경기 결과 정정",
    "GAME_VOID": "경기 무효 처리", "AWARD_GRANT": "업적 지급·회수", "ACCOUNT_MEMBER_LINK": "계정·회원 연결",
}


def member_label(member):
    status = "보완 요청" if member.get("registration_status") == "REJECTED" else STATUS_LABELS.get(member["status"], member["status"])
    return f"{member['riot_id']} · {status}"


def reset_recovery_confirmation():
    st.session_state.admin_reset_checked = False
    st.session_state.pop("admin_reset_receipt", None)


def grant_manual_award(core, token, member_ids, units, reason):
    """Keep a submitted award key until storage succeeds, then prepare the next form."""
    result = core.grant_award(
        token, member_ids, units, reason, st.session_state["admin_award_request"]
    )
    st.session_state["admin_award_request"] = f"manual-award:{uuid4().hex}"
    return result


def apply_manual_score(core, token, member_id, amount, reason, request_state_key):
    result = core.adjust_score(token, member_id, amount, reason,
                               request_key=st.session_state[request_state_key])
    st.session_state[request_state_key] = uuid4().hex
    return result


core, competition, token, actor = context()
heading("운영 관리", "회원 승인, 점수·업적 조정, 운영 계정과 변경 기록을 관리합니다.")
require_staff(actor, admin=True)
st.session_state.setdefault("admin_award_request", f"manual-award:{uuid4().hex}")

members = core.list_members(include_pending=True)
member_map = {member["id"]: member for member in members}
pending = [member for member in members if member["status"] == "PENDING" and member.get("registration_status") != "REJECTED"]
active_members = [member for member in members if member["status"] == "APPROVED"]

with st.container(horizontal=True):
    st.metric("승인 대기", f"{len(pending)}명", border=True)
    st.metric("활동 회원", f"{len(active_members)}명", border=True)
    st.metric("현재 일반내전 증감", f"±{core.policy()['k']}점", border=True)

join_tab, members_tab, policy_tab, awards_tab, accounts_tab, audit_tab = st.tabs(
    ["가입 승인", "회원·점수", "점수 정책", "업적 관리", "운영 계정", "변경 기록"]
)

with join_tab:
    st.subheader("가입 신청 확인")
    if not pending:
        st.info("대기 중인 가입 신청이 없습니다.")
    else:
        pending_rows = [
            {"Riot ID": member["riot_id"], "주 포지션": member["main_role"],
             "부 포지션": member["sub_role"]}
            for member in pending
        ]
        if any(member["application_notes"] for member in pending):
            for row, member in zip(pending_rows, pending):
                row["이전 신청 메시지"] = member["application_notes"]
        st.dataframe(pending_rows, hide_index=True)
        pending_id = st.selectbox(
            "승인할 회원", [member["id"] for member in pending],
            format_func=lambda member_id: member_map[member_id]["riot_id"],
            index=None, placeholder="가입 신청자를 선택하세요", key="admin_pending_id",
        )
        if pending_id is not None:
            current_applicant = member_map[pending_id]
            context_key = f"admin_approval_context_{actor['id']}_{pending_id}"
            if context_key not in st.session_state:
                st.session_state[context_key] = dict(current_applicant)
            applicant = st.session_state[context_key]
            stale = applicant["updated_at"] != current_applicant["updated_at"]
            if stale:
                st.warning("신청자가 정보를 변경했습니다. 최신 신청을 불러온 뒤 점수와 메모를 다시 확인해주세요.")
                if st.button("최신 신청 불러오기", key=f"{context_key}_reload"):
                    st.session_state[context_key] = dict(current_applicant)
                    st.rerun()
            version = sha256(applicant["updated_at"].encode()).hexdigest()[:16]
            form_key = f"admin_review_{actor['id']}_{pending_id}_{version}"
            st.caption(f"검토 중인 신청 · {applicant['riot_id']} · 주 포지션 {applicant['main_role']} · 부 포지션 {applicant['sub_role']}")
            if applicant.get("current_tier"):
                lp_text = f" · {applicant['current_tier_lp']} LP" if applicant.get("current_tier_lp") is not None else ""
                st.caption(f"저장된 현재 티어 · {applicant['current_tier']}{lp_text}")
            else:
                st.caption("현재 티어는 가입 승인 후 프로필의 갱신하기로 가져옵니다.")
            if applicant["application_notes"]:
                st.caption("이전 신청 메시지")
                st.text(applicant["application_notes"])
            mode = st.segmented_control(
                "기본점수 입력 방식", ["직접 입력", "티어 배점"],
                default="직접 입력", key=f"{form_key}_mode", disabled=stale,
            )
            suggested = 0
            if mode == "티어 배점":
                solo_tier = st.selectbox("솔로랭크 티어", list(TIER_POINTS), key=f"{form_key}_solo", disabled=stale)
                flex_tier = st.selectbox("자유랭크 티어", FLEX_TIERS, key=f"{form_key}_flex", disabled=stale)
                suggested = TIER_POINTS[solo_tier] + FLEX_TIERS.index(flex_tier)
                st.caption(f"솔로랭크 {TIER_POINTS[solo_tier]}점 + 자유랭크 {FLEX_TIERS.index(flex_tier)}점 = {suggested}점. 기본점수 계산용이며 회원의 현재 티어는 변경하지 않습니다.")
            with st.form(f"{form_key}_approve_{mode}_{suggested}"):
                approval_score = st.number_input(
                    "승인 기본점수", min_value=0, max_value=10000, value=suggested, step=1,
                    help="입력한 기본점수에 이후 경기 증감과 운영진 점수 보정이 더해집니다.",
                    key=f"{form_key}_score_{mode}_{suggested}", disabled=stale,
                )
                approval_notes = st.text_area("운영 메모", value=applicant["notes"], max_chars=2000, key=f"{form_key}_notes", disabled=stale, help="관리자에게만 표시됩니다. 신청 메시지와 별도로 보관합니다.")
                approve_submit = st.form_submit_button("가입 승인", type="primary", icon=":material/person_check:", disabled=stale, key=f"{form_key}_approve")
            if approve_submit and not stale:
                perform(
                    lambda: core.approve_member(token, pending_id, int(approval_score), approval_notes, expected_updated_at=applicant["updated_at"]),
                    f"{applicant['riot_id']} 님의 가입을 승인했습니다.",
                )
            with st.expander("가입 신청 거절"):
                with st.form(f"{form_key}_reject_form"):
                    reject_reason = st.text_input("거절 사유", max_chars=1000, key=f"{form_key}_reject_reason", disabled=stale)
                    reject_submit = st.form_submit_button("신청 거절", disabled=stale, key=f"{form_key}_reject")
                if reject_submit and not stale:
                    perform(lambda: core.reject_registration(token, pending_id, reject_reason, expected_updated_at=applicant["updated_at"]), "가입 신청을 거절했습니다. 신청자는 같은 계정에서 보완 후 다시 신청할 수 있습니다.")

with members_tab:
    st.subheader("회원 정보와 점수 보정")
    if not members:
        st.info("회원이 등록되면 이곳에서 정보와 점수를 관리할 수 있습니다.")
    else:
        target_id = st.selectbox(
            "관리할 회원", list(member_map), format_func=lambda member_id: member_label(member_map[member_id]),
            index=None, placeholder="Riot ID로 검색하세요", key="admin_member_id",
        )
        if target_id is not None:
            target = member_map[target_id]
            with st.container(horizontal=True):
                st.metric("기본점수", target["base_score"])
                st.metric("경기·수기 증감", target["score"] - target["base_score"])
                st.metric("현재 전력점수", target["score"])
            member_edit_form(core, token, actor, target_id, prefix="admin")
            with st.expander("점수 보정", expanded=True):
                st.caption("현재 점수에 입력한 만큼 더하거나 뺍니다. 보정 사유와 처리자가 기록됩니다.")
                database_key = sha256(str(core.db_path).encode()).hexdigest()[:12]
                score_request_key = f"admin_adjust_request_{database_key}_{actor['id']}_{target_id}"
                st.session_state.setdefault(score_request_key, uuid4().hex)
                with st.form(f"admin_adjust_{target_id}_{st.session_state[score_request_key]}"):
                    adjustment = st.number_input("점수 보정량", -10000, 10000, value=0, step=1)
                    adjustment_reason = st.text_input("점수 보정 사유", max_chars=1000)
                    adjustment_submit = st.form_submit_button("점수 보정 적용")
                if adjustment_submit:
                    perform(
                        lambda: apply_manual_score(core, token, target_id, int(adjustment), adjustment_reason, score_request_key),
                        f"{target['riot_id']} 님의 점수를 {int(adjustment):+d}점 보정했습니다.",
                    )
            lifecycle_key = f"admin_membership_context_{sha256(str(core.db_path).encode()).hexdigest()[:12]}_{actor['id']}_{target_id}"
            st.session_state.setdefault(lifecycle_key, dict(target))
            reviewed_member = st.session_state[lifecycle_key]
            lifecycle_stale = reviewed_member["updated_at"] != target["updated_at"]
            lifecycle_version = sha256(reviewed_member["updated_at"].encode()).hexdigest()[:16]
            if lifecycle_stale:
                st.warning("회원 정보나 상태가 변경되었습니다. 최신 상태를 확인한 뒤 탈퇴·복귀를 처리해주세요.")
                if st.button("최신 회원 상태 불러오기", key=f"{lifecycle_key}_reload"):
                    st.session_state[lifecycle_key] = dict(target)
                    st.rerun()
            if target["status"] == "KICKED":
                with st.expander("회원 복귀"):
                    st.caption("이전에 승인된 회원은 활동 상태로, 미승인 신청자는 보완·승인 대기 상태로 돌아갑니다. 점수와 내부 메모는 유지됩니다.")
                    with st.form(f"admin_restore_{target_id}_{lifecycle_version}"):
                        restore_reason = st.text_input("복귀 사유", max_chars=1000, disabled=lifecycle_stale)
                        restore_submit = st.form_submit_button("회원 복귀 처리", disabled=lifecycle_stale)
                    if restore_submit:
                        if not restore_reason.strip():
                            st.error("복귀 사유를 입력해주세요.")
                        else:
                            perform(
                                lambda: core.restore_member(token, target_id, restore_reason, expected_updated_at=reviewed_member["updated_at"]),
                                "회원의 복귀 상태를 저장했습니다. 기존 계정으로 다시 로그인할 수 있습니다.",
                            )
            else:
                with st.expander("회원 탈퇴 처리"):
                    affected = core.member_active_events(token, target_id)
                    if affected:
                        st.warning("진행 중인 내전·경매가 있습니다. 탈퇴 즉시 로그인과 팀장 입찰·운영 권한이 중단되므로 아래 내전의 진행 방안을 먼저 확인해주세요.")
                        st.dataframe([{"내전": row["title"], "종류": row["kind"], "상태": row["status"], "팀장": "팀장" if row["is_captain"] else "", "내전 번호": row["id"]} for row in affected], hide_index=True)
                    else:
                        st.caption("참여하거나 개설한 진행 중 내전·경매가 없습니다.")
                    with st.form(f"admin_kick_{target_id}_{lifecycle_version}"):
                        kick_reason = st.text_input("탈퇴 처리 사유", max_chars=1000, disabled=lifecycle_stale)
                        kick_submit = st.form_submit_button("탈퇴 처리", disabled=lifecycle_stale)
                    if kick_submit:
                        perform(lambda: core.kick_member(token, target_id, kick_reason, expected_updated_at=reviewed_member["updated_at"]), "회원을 탈퇴 처리했습니다.")
        adjustment_rows = core.list_adjustments(token=token)
        if adjustment_rows:
            with st.expander("최근 점수 보정 내역"):
                st.dataframe([
                    {"회원": row["riot_id"], "보정량": row["amount"], "사유": row["reason"],
                     "처리자": row["actor_name"], "처리 시각 (UTC)": row["created_at"]}
                    for row in adjustment_rows[:100]
                ], hide_index=True)

with policy_tab:
    st.subheader("일반내전 점수 정책")
    current_policy = core.policy()
    st.write(f"현재 일반내전은 승리 **+{current_policy['k']}점**, 패배 **-{current_policy['k']}점**입니다.")
    st.caption("변경한 증감량은 이후 새로 개설하는 일반내전부터 적용됩니다. 기존 내전은 개설 당시 규칙을 유지합니다. 경매는 전력점수 증감 없이 우승 업적으로 보상합니다.")
    with st.form("admin_policy"):
        policy_k = st.number_input("승패 공통 증감량", min_value=1, max_value=100, value=int(current_policy["k"]), step=1)
        policy_submit = st.form_submit_button("점수 정책 저장", type="primary")
    if policy_submit:
        if int(policy_k) == current_policy["k"] and current_policy["mode"] == "fixed":
            st.info("현재 정책과 같습니다.")
        else:
            perform(lambda: core.set_policy(token, mode="fixed", k=int(policy_k)), "일반내전 점수 정책을 변경했습니다.")
    with st.expander("점수 정책 변경 내역"):
        st.dataframe([
            {"정책 번호": row["id"], "승리": row["k"], "패배": -row["k"], "적용 시각 (UTC)": row["effective_at"]}
            for row in core.policy_history()
        ], hide_index=True)

with awards_tab:
    st.subheader("우승 업적 지급과 회수")
    st.caption("고양이 5개 = 별 1개 · 별 5개 = 메달 1개 · 메달 5개 = 트로피 1개")
    if not active_members:
        st.info("승인된 회원이 등록되면 업적을 지급할 수 있습니다.")
    else:
        st.dataframe([
            {"회원": member["riot_id"], "트로피": member["trophies"], "메달": member["medals"],
             "별": member["stars"], "고양이": member["cats"]}
            for member in active_members
        ], hide_index=True)
        with st.form(f"admin_awards_{st.session_state.admin_award_request}"):
            award_ids = st.multiselect(
                "업적을 조정할 회원", [member["id"] for member in active_members],
                format_func=lambda member_id: member_map[member_id]["riot_id"],
            )
            award_action = st.selectbox("조정 방식", ["지급", "회수"])
            award_unit = st.selectbox("업적 종류", ["고양이", "별", "메달", "트로피"])
            award_count = st.number_input("회원 1명당 개수", min_value=1, max_value=1000, value=1, step=1)
            award_reason = st.text_input("업적 조정 사유", max_chars=1000)
            award_submit = st.form_submit_button("업적 조정 적용", type="primary")
        if award_submit:
            award_units = {"고양이": 1, "별": 5, "메달": 25, "트로피": 125}.get(award_unit)
            if award_action not in ("지급", "회수") or award_units is None:
                st.error("업적 종류와 조정 방식을 확인해주세요.")
            else:
                signed_units = int(award_count) * award_units * (1 if award_action == "지급" else -1)
                perform(
                    lambda: grant_manual_award(core, token, award_ids, signed_units, award_reason),
                    f"{len(award_ids)}명의 {award_unit} {int(award_count)}개 {award_action}를 기록했습니다.",
                )

with accounts_tab:
    st.subheader("계정 관리")
    st.caption("승인된 회원은 일반내전과 경매를 개설하고 본인이 만든 내전을 진행합니다. 이 경매의 팀장으로 지정된 회원만 본인 팀으로 입찰합니다.")
    accounts = core.list_accounts(token)
    st.dataframe([
        {"로그인 아이디": account["username"], "표시 이름": account["display_name"],
         "권한": ACCOUNT_ROLES[account["role"]], "연결 회원": member_map.get(account.get("member_id"), {}).get("riot_id", "-"), "활성": bool(account["active"])}
        for account in accounts
    ], hide_index=True)
    st.caption("운영진도 기존 개인계정을 사용합니다. 승인된 회원에게 진행자·관리자 권한을 부여하세요. 회원 연결은 유지되며 탈퇴 중에는 운영 권한도 사용할 수 없습니다.")
    with st.expander("권한과 계정 활성 상태 변경"):
        account_map = {account["id"]: account for account in accounts}
        account_id = st.selectbox(
            "권한을 변경할 계정", list(account_map), index=None,
            format_func=lambda account_id: f"{account_map[account_id]['display_name']} ({account_map[account_id]['username']})",
            placeholder="승인된 회원 또는 운영 계정을 선택하세요", key="admin_account_id",
        )
        if account_id is not None:
            account = account_map[account_id]
            role_options = list(ACCOUNT_ROLES) if account["member_id"] is None or account["member_status"] == "APPROVED" else [role for role in ACCOUNT_ROLES if role in ("member", account["role"])]
            if account["member_id"] is not None and account["member_status"] != "APPROVED":
                st.caption("운영 권한은 가입 승인과 활동 상태를 확인한 뒤 부여할 수 있습니다.")
            with st.form(f"admin_account_role_{account_id}"):
                changed_role = st.selectbox("변경할 권한", role_options, index=role_options.index(account["role"]), format_func=ACCOUNT_ROLES.get)
                account_active = st.checkbox("활성 계정", value=bool(account["active"]))
                st.caption("저장하면 해당 계정의 현재 로그인이 해제됩니다.")
                role_submit = st.form_submit_button("운영 계정 변경 저장")
            if role_submit:
                perform(
                    lambda: core.set_account_role(token, account_id, changed_role, account_active),
                    "운영 계정의 권한과 활성 상태를 변경했습니다.",
                )

    with st.expander("회원 비밀번호 재설정"):
        st.caption("카카오톡에서 본인을 확인한 후 일회용 코드를 발급해 직접 전달해 주세요. 발급 즉시 기존 로그인은 해제됩니다.")
        st.caption("본인의 비밀번호는 ‘내 계정 → 비밀번호 변경’에서 변경합니다.")
        reset_accounts = {row["id"]: row for row in accounts if row["active"] and row["member_status"] != "KICKED" and row["id"] != actor["id"]}
        reset_account = st.selectbox("재설정할 계정", list(reset_accounts), index=None,
            format_func=lambda aid: f"{reset_accounts[aid]['display_name']} ({reset_accounts[aid]['username']})", key="admin_reset_account", on_change=reset_recovery_confirmation)
        identity_checked = st.checkbox("계정 소유자 본인 확인을 마쳤습니다.", key="admin_reset_checked")
        if st.button("30분 유효 코드 발급", disabled=reset_account is None or not identity_checked):
            def issue_reset():
                result = core.issue_password_reset(token, reset_account, lifetime_minutes=30)
                st.session_state.admin_reset_receipt = {**result, "account_id": reset_account}
            perform(issue_reset, "일회용 재설정 코드를 발급했습니다.")
        receipt = st.session_state.get("admin_reset_receipt")
        if receipt and receipt["account_id"] == reset_account:
            st.code(receipt["token"], language=None)
            st.caption(f"유효 기한 (UTC) · {receipt['expires_at']}")
            if st.button("코드 표시 닫기"):
                st.session_state.pop("admin_reset_receipt", None)
                st.rerun()

with audit_tab:
    st.subheader("변경 기록")
    audit_limit = st.selectbox("조회 개수", [100, 300, 1000], key="admin_audit_limit")
    audit_rows = core.audit_log(limit=audit_limit, token=token)
    if not audit_rows:
        st.info("기록된 변경 내역이 없습니다.")
    else:
        audit_filter = st.text_input("변경 기록 검색", placeholder="작업·처리자·대상·사유로 검색하세요", key="admin_audit_filter")
        audit_display = [
            {"기록 번호": row["id"], "작업": AUDIT_ACTIONS.get(row["action"], row["action"]),
             "처리자": row["actor_name"] or "가입 신청자", "대상": row["target"],
             "상세": row["details"], "기록 시각 (UTC)": row["created_at"]}
            for row in audit_rows
        ]
        if audit_filter.strip():
            audit_display = [row for row in audit_display if audit_filter.casefold().strip() in " ".join(map(str, row.values())).casefold()]
        st.dataframe(audit_display, hide_index=True)
        st.download_button(
            "표시된 변경 기록 CSV 다운로드", data=core.csv_bytes(audit_display),
            file_name="rolymoly_audit.csv", mime="text/csv", icon=":material/download:",
        )
