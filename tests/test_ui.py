"""Exercise the real multipage app with isolated demonstration databases."""
import os
from pathlib import Path
import tempfile
import unittest
from tests.test_native_blocks import native_blocks
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.ui import member_table, services


ROOT = Path(__file__).resolve().parents[1]


class AppUITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(native_blocks())
        self.temporary = tempfile.TemporaryDirectory(prefix="roly-ui-test-")
        self.environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.temporary.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(services.clear)
        worker = patch.object(LiveAuction, "ensure_worker")
        worker.start()
        self.addCleanup(worker.stop)
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertHealthy()
        database_path = Path(self.app.session_state["db_path"]).resolve()
        self.assertTrue(database_path.is_relative_to(Path(self.temporary.name).resolve()))
        self.core = Core(database_path)
        self.competition = Competition(self.core)
        self.token = self.app.session_state["token"]

    def assertHealthy(self):
        self.assertFalse(self.app.exception, [error.message for error in self.app.exception])

    def page(self, filename):
        self.app.switch_page(f"app_pages/{filename}.py").run()
        self.assertHealthy()

    def widget(self, kind, label):
        if label in ("입찰하기", "입찰할 포인트"):
            from tests.live_panel_client import panel_client
            return panel_client(self.app).widget(kind, label)
        matches = [widget for widget in getattr(self.app, kind)
                   if (kind == "button" and label == "입찰하기" and (widget.key or "").startswith("live_bid_"))
                   or (label != "입찰하기" and widget.label == label)]
        if kind == "button" and label == "경매 내전 만들기":
            matches = [widget for widget in matches if widget.proto.is_form_submitter]
        self.assertEqual(len(matches), 1, f"Expected exactly one {kind}: {label}")
        return matches[0]

    def click(self, label):
        self.widget("button", label).click().run()
        self.assertHealthy()

    def test_all_seven_pages_render(self):
        for page in ("home", "members", "normal", "auction", "events", "join", "admin"):
            with self.subTest(page=page):
                self.page(page)
                self.assertTrue(self.app.title)
                self.assertFalse(self.app.error, [error.value for error in self.app.error])

    def test_join_request_and_admin_approval(self):
        self.app.session_state.token = None
        self.page("join")
        self.widget("text_input", "사용할 로그인 아이디").set_value("ui-new-member")
        self.widget("text_input", "사용할 비밀번호 (10자 이상)").set_value("synthetic-new-password")
        self.widget("text_input", "비밀번호 확인").set_value("synthetic-new-password")
        self.widget("text_input", "Riot ID").set_value("UI신청회원#KR1")
        self.widget("selectbox", "주 포지션").set_value("MID")
        self.widget("selectbox", "부 포지션").set_value("SUP")
        self.app.checkbox[0].check()
        self.click("회원가입")
        member = next(member for member in self.core.list_members(True) if member["riot_id"] == "UI신청회원#KR1")
        self.assertEqual(member["status"], "PENDING")
        self.assertNotIn(member["id"], [row["id"] for row in self.core.list_members()])
        member_token = self.app.session_state.token
        self.assertEqual(self.core.session(member_token)["member_id"], member["id"])
        self.app.session_state.token = self.token
        self.page("admin")
        self.app.selectbox(key="admin_pending_id").set_value(member["id"]).run()
        self.assertHealthy()
        self.widget("number_input", "승인 기본점수").set_value(280)
        self.click("가입 승인")
        approved = self.core.get_member(member["id"])
        self.assertEqual((approved["status"], approved["base_score"], approved["score"]), ("APPROVED", 280, 280))
        self.assertEqual((approved["main_role"], approved["sub_role"]), ("MID", "SUP"))
        self.app.session_state.token = member_token
        self.page("members")
        self.assertTrue(self.app.button(key=f"member_profile_{member['id']}"))
        self.assertIn("UI신청회원#KR1", " ".join(item.value for item in self.app.text))

    def test_recovery_identity_confirmation_is_bound_to_selected_account(self):
        self.app.session_state.admin_active_tab = "운영 계정"
        self.page("admin")
        accounts = [row for row in self.core.list_accounts(self.token) if row["role"] == "member"]
        self.assertGreaterEqual(len(accounts), 2)
        self.app.selectbox(key="admin_reset_account").select(accounts[0]["id"]).run()
        self.app.checkbox(key="admin_reset_checked").check().run()
        self.assertFalse(self.widget("button", "30분 유효 코드 발급").disabled)
        self.app.session_state.admin_reset_receipt = {"account_id": accounts[0]["id"], "token": "synthetic-code", "expires_at": "2030-01-01"}
        self.app.selectbox(key="admin_reset_account").select(accounts[1]["id"]).run()
        self.assertHealthy()
        self.assertFalse(self.app.checkbox(key="admin_reset_checked").value)
        self.assertTrue(self.widget("button", "30분 유효 코드 발급").disabled)
        self.assertNotIn("admin_reset_receipt", self.app.session_state)

    def test_member_page_displays_ledger_award_symbols(self):
        member = self.core.list_members()[0]
        self.core.grant_award(self.token, [member["id"]], 67, "회원표 기호 확인", "ui-member-symbols")
        self.page("members")
        public_text = " ".join(item.value for item in [*self.app.markdown, *self.app.caption, *self.app.text])
        self.assertIn("🏅🏅 ⭐⭐⭐ 🐱🐱", public_text)
        self.assertIn(f"{member['score']:,} P", public_text)
        self.assertTrue(self.app.button(key=f"member_profile_{member['id']}"))
        for private in ("notes", "created_at", "base_score"):
            self.assertNotIn(private, public_text)

    def test_normal_form_rejects_nine_and_creates_exactly_ten(self):
        with patch.object(Core, "list_members", autospec=True, side_effect=Core.list_members) as loaded:
            self.page("normal")
        self.assertEqual(loaded.call_count, 1)
        self.assertEqual(loaded.call_args.kwargs, {"include_pending": True})
        before = {event["id"] for event in self.competition.list_events()}
        chosen_by_role = {
            role: [member["id"] for member in self.core.list_members() if member["main_role"] == role][:2]
            for role in ROLES
        }
        self.widget("text_input", "내전 이름").set_value("UI 10인 검증")
        chosen = [mid for role, ids in chosen_by_role.items() for mid in (ids[:1] if role == "SUP" else ids)]
        next(w for w in self.app.multiselect if (w.key or "").startswith("normal_roster_10_") and w.key.endswith("_members")).set_value(chosen).run()
        self.click("일반 내전 만들기")
        self.assertTrue(self.app.error)
        self.assertEqual({event["id"] for event in self.competition.list_events()}, before)
        next(w for w in self.app.multiselect if (w.key or "").startswith("normal_roster_10_") and w.key.endswith("_members")).set_value([mid for ids in chosen_by_role.values() for mid in ids]).run()
        self.click("일반 내전 만들기")
        created = [event for event in self.competition.list_events() if event["id"] not in before]
        self.assertEqual(len(created), 1)
        event = self.competition.get_event(created[0]["id"])
        self.assertEqual((event["kind"], event["title"], event["format"]), ("NORMAL", "UI 10인 검증", "SINGLE"))
        self.assertEqual(len(event["teams"]), 2)
        actual_members = []
        for team in event["teams"]:
            self.assertEqual(len(team["players"]), 5)
            self.assertEqual({player["role"] for player in team["players"]}, set(ROLES))
            actual_members.extend(player["member_id"] for player in team["players"])
        self.assertEqual(set(actual_members), {member_id for members in chosen_by_role.values() for member_id in members})
        self.assertEqual(len(set(actual_members)), 10)
        self.assertEqual(self.app.session_state["focus_event"], event["id"])

    def test_auction_entry_prepares_saved_roster_and_starts_live_auction(self):
        # Freeze the server clock so UI rendering cannot consume the 10-second
        # auction window. Authentication and bidding still use the real services.
        clock = [2_000_000_000.0]
        live = LiveAuction(self.core, self.competition, clock=lambda: clock[0])
        service_patch = patch("roly.auction_ui.live_service", return_value=live)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.page("auction")
        event_id = self.app.selectbox(key="auction_event").value
        initial = self.competition.get_event(event_id)
        self.assertIsNone(initial["current_player_id"])
        initial_scores = {member["id"]: member["score"] for member in self.core.list_members()}
        self.assertFalse(any(button.label in ("다음 선수 추첨", "낙찰 확정") for button in self.app.button))
        self.assertEqual(self.competition.get_event(event_id)["status"], "AUCTION_READY")
        self.click("경매 설정하기")
        self.widget("selectbox", "선수별 입찰 시간 (초)").set_value(10)
        self.click("경매 설정 저장")
        state = live.get_state(event_id)
        self.assertEqual((state["status"], state["bid_seconds"]), ("READY", 10))
        self.assertEqual(len(state["lots"]), 16)
        self.click("경매 시작")
        state = live.get_state(event_id)
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(state["current_lot"]["status"], "OPEN")
        self.assertEqual(self.competition.get_event(event_id)["status"], "AUCTION")
        details = next(expander for expander in self.app.expander if expander.label == "경매 정보")
        self.assertFalse(details.proto.expanded)
        self.assertEqual(self.app.selectbox(key="auction_event").value, event_id)
        self.assertFalse(self.app.title)
        self.assertEqual([team["remaining"] for team in state["teams"]], [team["remaining"] for team in initial["teams"]])
        self.assertTrue(all(len(team["players"]) == 1 for team in state["teams"]))
        self.assertEqual({member["id"]: member["score"] for member in self.core.list_members()}, initial_scores)
        self.assertFalse(any((button.key or "").startswith("live_bid_") for button in self.app.button))
        self.assertTrue(any(button.key == "live_pause" for button in self.app.button))
        self.app.selectbox(key="demo_account_choice").set_value("demo_captain_1").run()
        self.assertHealthy()
        captain = self.core.session(self.app.session_state["token"])
        self.assertEqual(captain["role"], "member")
        self.assertEqual(captain["member_id"], initial["teams"][0]["captain_id"])
        self.assertFalse(any(button.key == "live_pause" for button in self.app.button))
        self.widget("number_input", "입찰할 포인트").set_value(5).run()
        self.click("입찰하기")
        bid = live.get_state(event_id)["bids"][0]
        self.assertEqual((bid["team_id"], bid["amount"]), (initial["teams"][0]["id"], 5))
        self.app.selectbox(key="demo_account_choice").set_value("demo").run()
        self.assertHealthy()
        self.assertEqual(self.core.session(self.app.session_state["token"])["role"], "admin")
        self.assertTrue(any(button.key == "live_pause" for button in self.app.button))
        # The compact information panel still supports creating another auction
        # without changing the running auction's saved bid or selection state.
        self.app.button(key="t_open_create").click().run()
        self.assertHealthy()
        next(w for w in self.app.text_input if (w.key or "").startswith("t_create_title_")).set_value("진행 중 새 경매")
        self.click("경매 내전 만들기")
        created_id = self.app.selectbox(key="auction_event").value
        self.assertNotEqual(created_id, event_id)
        self.assertEqual(self.competition.get_event(created_id)["status"], "DRAFT")
        self.assertEqual(live.get_state(event_id)["status"], "RUNNING")
        self.assertEqual(live.get_state(event_id)["bids"][0]["amount"], 5)

    def test_admin_adjustments_awards_policy_accounts_and_access(self):
        self.app.session_state.admin_active_tab = "회원·점수"
        self.page("admin")
        self.assertEqual(len(self.app.tabs), 6)
        member = self.core.list_members()[0]
        self.app.selectbox(key="admin_member_id").set_value(member["id"]).run()
        self.assertHealthy()
        self.widget("number_input", "점수 보정량").set_value(25)
        self.click("점수 보정 적용")
        self.assertTrue(self.app.error)
        self.assertEqual(self.core.get_member(member["id"])["score"], member["score"])
        self.widget("number_input", "점수 보정량").set_value(25)
        self.widget("text_input", "점수 보정 사유").set_value("UI 점수 보정 검증")
        self.click("점수 보정 적용")
        self.assertEqual(self.core.get_member(member["id"])["score"], member["score"] + 25)
        self.app.session_state.admin_active_tab = "점수 정책"
        self.app.run()
        self.widget("number_input", "승패 공통 증감량").set_value(12)
        self.click("점수 정책 저장")
        self.assertEqual((self.core.policy()["mode"], self.core.policy()["k"]), ("fixed", 12))
        self.app.session_state.admin_active_tab = "업적 관리"
        self.app.run()
        self.widget("multiselect", "업적을 조정할 회원").set_value([member["id"]])
        self.widget("selectbox", "업적 종류").set_value("별")
        self.widget("number_input", "회원 1명당 개수").set_value(2)
        self.widget("text_input", "업적 조정 사유").set_value("UI 업적 지급 검증")
        self.click("업적 조정 적용")
        self.assertEqual(self.core.get_member(member["id"])["award_units"], member["award_units"] + 10)
        registered = self.core.register_member("uiorganizer", "ui-test-password-123", "UIOrganizer#QA",
            "TOP", "JG", request_key=str(uuid4()))
        self.core.approve_member(self.token, registered["member_id"], 100)
        self.app.session_state.admin_active_tab = "운영 계정"
        self.app.run()
        self.app.selectbox(key="admin_account_id").set_value(registered["account_id"]).run()
        self.widget("selectbox", "변경할 권한").set_value("organizer")
        self.click("운영 계정 변경 저장")
        accounts = self.core.list_accounts(self.token)
        account = next(account for account in accounts if account["username"] == "uiorganizer")
        self.assertEqual(account["role"], "organizer")
        self.assertEqual(account["id"], registered["account_id"])
        self.assertEqual(account["member_id"], registered["member_id"])
        self.app.session_state.admin_active_tab = "변경 기록"
        self.app.run()
        self.assertTrue(self.app.get("download_button"))
        self.app.session_state["token"] = self.core.login("uiorganizer", "ui-test-password-123")
        self.app.run()
        self.assertHealthy()
        self.assertFalse(self.app.tabs)
        self.assertTrue(self.app.info)
        self.assertTrue(any("관리자 권한이 필요한 화면" in message.value for message in self.app.info))
        self.assertFalse(any(button.label == "점수 정책 저장" for button in self.app.button))


class MemberAwardDisplayTests(unittest.TestCase):
    """Check display against normalized balances returned by the real award ledger."""

    def setUp(self):
        self.enterContext(native_blocks())
        self.temporary = tempfile.TemporaryDirectory(prefix="roly-award-display-test-")
        self.addCleanup(self.temporary.cleanup)
        self.core = Core(Path(self.temporary.name) / "awards.sqlite3")
        self.core.setup_admin("displayadmin", "display-test-password")
        self.token = self.core.login("displayadmin", "display-test-password")
        self.member_id = self.core.join_member("기호회원#KR1", "TOP", "JG", notes="비공개 운영 메모")
        self.core.approve_member(self.token, self.member_id, 280)

    def grant(self, units, request_key):
        self.core.grant_award(self.token, [self.member_id], units, "표시 검증", request_key)

    def assertSymbols(self, expected, units):
        member = self.core.get_member(self.member_id)
        table = member_table([member])
        self.assertEqual(member["award_units"], units)
        self.assertEqual(table.iloc[0]["우승 기호"], expected)
        self.assertEqual(table.iloc[0]["전력점수"], 280)
        self.assertEqual(list(table.columns), ["Riot ID", "우승 기호", "클랜 티어", "현재 티어", "현재 LP", "주 포지션", "부 포지션", "전력점수", "일반내전", "승률"])
        self.assertEqual((table.iloc[0]["클랜 티어"], table.iloc[0]["현재 티어"], table.iloc[0]["현재 LP"]), ("미입력", "미입력", None))
        return table

    def test_zero_and_empty_member_list(self):
        self.assertSymbols("-", 0)
        empty = member_table([])
        self.assertTrue(empty.empty)
        self.assertIn("우승 기호", empty.columns)

    def test_normalized_one_through_four_repeat_each_symbol(self):
        for count in range(1, 5):
            with self.subTest(count=count):
                self.grant(156, f"repeated-{count}")
                self.assertSymbols(" ".join(symbol * count for symbol in ("🏆", "🏅", "⭐", "🐱")), 156 * count)

    def test_promotions_and_reversal_follow_ledger_balance(self):
        for units, expected, balance, request_key in (
            (4, "🐱🐱🐱🐱", 4, "cats"),
            (1, "⭐", 5, "star"),
            (20, "🏅", 25, "medal"),
            (100, "🏆", 125, "trophy"),
            (-1, "🏅🏅🏅🏅 ⭐⭐⭐⭐ 🐱🐱🐱🐱", 124, "reverse-trophy"),
            (-124, "-", 0, "reverse-all"),
        ):
            with self.subTest(request_key=request_key):
                self.grant(units, request_key)
                self.assertSymbols(expected, balance)

    def test_large_trophy_balance_is_counted_and_exported(self):
        self.grant(2526, "many-trophies")
        table = self.assertSymbols("🏆 × 20 🏅 🐱", 2526)
        exported = self.core.csv_bytes(table.to_dict("records")).decode("utf-8-sig")
        self.assertIn("우승 기호", exported)
        self.assertIn("🏆 × 20 🏅 🐱", exported)
        self.assertNotIn("비공개 운영 메모", exported)


if __name__ == "__main__":
    unittest.main()
