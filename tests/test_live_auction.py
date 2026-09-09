"""Real SQLite concurrency, clock boundaries and linked-account authorization."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService


class Clock:
    def __init__(self):
        self.value = 2_000_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class LiveAuctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_temp = tempfile.TemporaryDirectory(prefix="roly-live-fixture-")
        cls.base = Core(Path(cls.base_temp.name) / "base.sqlite3")
        cls.base.setup_admin("admin", "admin-password-1234")
        cls.admin = cls.base.login("admin", "admin-password-1234")
        Competition(cls.base)
        cls.ids = []
        for index in range(20):
            member_id = cls.base.join_member(f"Live{index}#KR1", ROLES[index % 5], ROLES[(index + 1) % 5])
            cls.base.approve_member(cls.admin, member_id, 100 + index)
            cls.ids.append(member_id)
        cls.captains = cls.ids[::5]
        cls.tokens = []
        cls.account_ids = []
        for index, member_id in enumerate(cls.captains):
            cls.account_ids.append(cls.base.create_account(cls.admin, f"captain{index}", "captain-password", role="member", member_id=member_id))
            cls.tokens.append(cls.base.login(f"captain{index}", "captain-password"))
        cls.host_id = cls.base.create_account(cls.admin, "host", "host-password-1234")
        cls.host = cls.base.login("host", "host-password-1234")
        cls.base.create_account(cls.admin, "player", "player-password-1234", role="member", member_id=cls.ids[1])
        cls.player_token = cls.base.login("player", "player-password-1234")

    @classmethod
    def tearDownClass(cls):
        cls.base_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-live-test-")
        self.path = Path(self.temp.name) / "auction.sqlite3"
        with closing(self.base.connect()) as source, closing(sqlite3.connect(self.path)) as target:
            source.backup(target)
        self.core = Core(self.path)
        self.comp = Competition(self.core)
        self.clock = Clock()
        self.live = LiveAuction(self.core, self.comp, clock=self.clock)
        self.event = self.comp.create_auction(self.admin, self.ids, self.captains)
        # A narrow fixture for the new stage; TournamentService tests own the
        # preceding recruitment, attendance and captain-confirmation workflow.
        with self.core.transaction() as db:
            db.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (self.event,))
        self.pool = [mid for mid in self.ids if mid not in self.captains]

    def tearDown(self):
        self.live.stop_worker()
        self.temp.cleanup()

    def start(self, **settings):
        settings.setdefault("order", self.pool)
        self.live.configure(self.admin, self.event, **settings)
        return self.live.start(self.admin, self.event)["current_lot"]

    def bid(self, index, amount, lot=None, request_id=None):
        lot = lot or self.live.get_state(self.event)["current_lot"]
        return self.live.place_bid(self.tokens[index], self.event, lot["id"], amount, request_id or str(uuid.uuid4()))

    def close(self):
        lot = self.live.get_state(self.event)["current_lot"]
        self.clock.value = lot["closes_at"]
        self.live.settle_due()

    def next(self):
        self.clock.advance(3)
        self.live.settle_due()
        return self.live.get_state(self.event)["current_lot"]

    def test_member_link_approval_uniqueness_and_staff_boundary(self):
        self.assertEqual(self.core.session(self.tokens[0])["member_id"], self.captains[0])
        with self.core.transaction() as db:
            with self.assertRaises(PermissionError):
                self.core.require_staff(db, self.tokens[0])
        with self.assertRaises(PermissionError):
            self.core.create_account(self.tokens[0], "intruder", "intruder-password")
        with self.assertRaises(ValueError):
            self.core.create_account(self.admin, "duplicate", "duplicate-password", role="member", member_id=self.captains[0])
        with self.assertRaises(ValueError):
            self.core.create_account(self.admin, "unlinked", "unlinked-password", role="member")
        pending = self.core.join_member("Pending#KR1", "TOP", "JG")
        with self.assertRaises(ValueError):
            self.core.create_account(self.admin, "pending", "pending-password", role="member", member_id=pending)
        with self.assertRaises(PermissionError):
            self.core.link_account_member(self.host, self.account_ids[0], self.ids[2], "not admin")
        with self.assertRaises(ValueError):
            self.core.link_account_member(self.admin, self.account_ids[0], self.ids[2], "")
        with self.assertRaises(ValueError):
            self.core.link_account_member(self.admin, self.account_ids[0], self.ids[2], "active event identity change")
        self.assertEqual(self.core.session(self.tokens[0])["member_id"], self.captains[0])
        self.start()
        receipt = self.bid(0, 0)
        self.assertEqual(receipt["team_id"], self.live.get_state(self.event)["teams"][0]["id"])
        self.comp.cancel_event(self.admin, self.event, "finish synthetic membership linkage scenario")
        self.core.link_account_member(self.admin, self.account_ids[0], self.ids[2], "confirmed identity")
        self.assertIsNone(self.core.session(self.tokens[0]))
        renewed = self.core.login("captain0", "captain-password")
        self.assertEqual(self.core.session(renewed)["member_id"], self.ids[2])
        self.core.kick_member(self.admin, self.ids[2], "membership ended")
        self.assertIsNone(self.core.session(renewed))
        with self.assertRaises(PermissionError):
            self.core.login("captain0", "captain-password")
        # Deactivation must still work after the linked member was kicked.
        self.core.set_account_role(self.admin, self.account_ids[0], "member", active=False)

    def test_existing_accounts_migrate_without_losing_sessions_or_foreign_keys(self):
        with closing(self.core.connect()) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            # Simulate the preceding schema with only the original admin/host.
            db.execute("DELETE FROM sessions WHERE account_id IN (SELECT id FROM accounts WHERE role='member')")
            db.execute("DELETE FROM audit WHERE actor_id IN (SELECT id FROM accounts WHERE role='member')")
            db.execute("DELETE FROM accounts WHERE role='member'")
            db.execute("CREATE TABLE accounts_old(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,salt TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','organizer')),active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL)")
            db.execute("INSERT INTO accounts_old SELECT id,username,display_name,password_hash,salt,role,active,created_at FROM accounts")
            db.execute("DROP TABLE accounts")
            db.execute("ALTER TABLE accounts_old RENAME TO accounts")
            db.commit()
        migrated = Core(self.path)
        self.assertEqual(migrated.session(self.admin)["role"], "admin")
        self.assertIsNone(migrated.session(self.admin)["member_id"])
        migrated.create_account(self.admin, "migrated", "migrated-password", role="member", member_id=self.captains[0])
        with closing(migrated.connect()) as db:
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
        Core(self.path)  # Repeat migration is harmless.

    def test_staff_ownership_and_captain_identity_no_admin_bid_override(self):
        for token in (self.tokens[0], self.host, "invented"):
            with self.assertRaises((PermissionError, ValueError)):
                self.live.configure(token, self.event)
        lot = self.start()
        for token in (self.admin, self.host, self.player_token, "invented"):
            with self.assertRaises(PermissionError):
                self.live.place_bid(token, self.event, lot["id"], 0, str(uuid.uuid4()))
        receipt = self.bid(0, 0)
        state = self.live.get_state(self.event)
        self.assertEqual(receipt["team_id"], state["teams"][0]["id"])
        with self.assertRaises(PermissionError):
            self.live.pause(self.tokens[0], self.event)
        with self.assertRaises(PermissionError):
            self.live.pause(self.host, self.event)

    def test_order_zero_first_bid_strict_increase_and_three_second_transition(self):
        lot = self.start(order=list(reversed(self.pool)))
        self.assertEqual(lot["member_id"], self.pool[-1])
        self.bid(0, 0)
        for amount in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.bid(1, amount)
        self.bid(1, 5)
        self.close()
        state = self.live.get_state(self.event)
        self.assertEqual(state["status"], "WAITING")
        self.assertEqual(state["current_lot"]["status"], "SOLD")
        self.assertEqual(state["teams"][1]["players"][-1]["price"], 5)
        self.clock.advance(2.99)
        self.live.settle_due()
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["id"], lot["id"])
        self.clock.advance(.02)
        self.live.settle_due()
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["member_id"], self.pool[-2])

    def test_exact_deadline_rejects_bid_and_replay_has_no_second_sale(self):
        lot = self.start()
        request = str(uuid.uuid4())
        original = self.bid(0, 10, request_id=request)
        self.clock.value = original["closes_at"]
        with self.assertRaises(ValueError):
            self.bid(1, 20)
        self.assertEqual(self.live.settle_due(), [self.event])
        self.assertEqual(self.live.settle_due(), [])
        replay = self.bid(0, 10, lot=lot, request_id=request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], original["id"])
        self.assertEqual(len(self.live.get_state(self.event)["bids"]), 1)
        with self.assertRaises(ValueError):
            self.bid(0, 11, lot=lot, request_id=request)
        with self.assertRaises(ValueError):
            self.bid(1, 10, lot=lot, request_id=request)

    def test_five_second_extension_pause_remaining_and_paused_bids(self):
        lot = self.start()
        self.clock.advance(7)
        receipt = self.bid(0, 0)
        self.assertEqual(receipt["closes_at"], lot["closes_at"] + 5)
        self.clock.advance(4)
        state = self.live.pause(self.admin, self.event)
        self.assertEqual(state["current_lot"]["remaining_seconds"], 4)
        self.clock.advance(100)
        self.assertEqual(self.live.settle_due(), [])
        with self.assertRaises(ValueError):
            self.bid(1, 5)
        state = self.live.resume(self.admin, self.event)
        self.assertEqual(state["current_lot"]["closes_at"], self.clock() + 4)
        self.close()
        self.clock.advance(1)
        state = self.live.pause(self.admin, self.event)
        self.assertEqual(state["next_in_seconds"], 2)
        self.clock.advance(50)
        state = self.live.resume(self.admin, self.event)
        self.assertEqual(state["next_in_seconds"], 2)
        self.clock.advance(2)
        self.live.settle_due()
        self.assertEqual(self.live.get_state(self.event)["status"], "RUNNING")

    def test_concurrent_equal_and_increasing_bids_are_serialized(self):
        original_lot = self.start()
        barrier = threading.Barrier(4)

        def attempt(index):
            barrier.wait()
            try:
                return self.bid(index, 10)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)
        barrier = threading.Barrier(4)

        def increasing(index):
            barrier.wait()
            try:
                return self.bid(index, 20 + index)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(increasing, range(4)))
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["highest_bid"], 23)
        pending = self.live.get_state(self.event)
        self.assertEqual(pending["current_lot"]["closes_at"], original_lot["closes_at"])
        self.assertEqual(pending["current_lot"]["remaining_seconds"], 10)
        self.clock.value = pending["current_lot"]["closes_at"]
        second = LiveAuction(Core(self.path), self.comp, clock=self.clock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            settlements = list(pool.map(lambda live: live.settle_due(), (self.live, second)))
        self.assertEqual(sum(self.event in value for value in settlements), 1)
        state = self.live.get_state(self.event)
        self.assertEqual(len(state["teams"][3]["players"]), 2)
        self.assertEqual(sum(e["type"] == "SOLD" for e in state["events"]), 1)

    def test_concurrent_retransmission_uuid_payload_is_checked(self):
        lot = self.start()
        self.clock.value = lot["closes_at"] - 2
        request = str(uuid.uuid4())
        barrier = threading.Barrier(6)

        def retry(_):
            barrier.wait()
            return self.bid(0, 0, request_id=request)

        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(retry, range(6)))
        self.assertEqual(len({receipt["id"] for receipt in receipts}), 1)
        self.assertEqual(sum(not receipt["replayed"] for receipt in receipts), 1)
        self.assertEqual(len(self.live.get_state(self.event)["bids"]), 1)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["closes_at"], lot["closes_at"] + 5)
        with self.assertRaises(ValueError):
            self.bid(0, 5, request_id=request)
        with self.assertRaises(ValueError):
            self.bid(0, 5, request_id="not-a-uuid")

    def test_each_valid_bid_adds_five_seconds_even_for_old_fixed_sessions(self):
        with self.assertRaisesRegex(ValueError, "해제"):
            self.live.configure(self.admin, self.event, reset_on_bid=False)
        lot = self.start(bid_seconds=10)
        original_deadline = lot["closes_at"]
        with self.core.transaction() as db:
            db.execute("UPDATE live_sessions SET reset_on_bid=0 WHERE event_id=?", (self.event,))
        self.clock.value = original_deadline - 2
        request = str(uuid.uuid4())
        first = self.bid(0, 5, request_id=request)
        self.assertEqual(first["closes_at"], original_deadline + 5)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["remaining_seconds"], 7)
        self.clock.value = original_deadline
        self.assertEqual(self.live.settle_due(), [])
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["status"], "OPEN")
        self.clock.value = first["closes_at"] - 1
        second = self.bid(1, 10)
        self.assertEqual(second["closes_at"], original_deadline + 10)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["remaining_seconds"], 6)
        self.clock.value = first["closes_at"]
        self.assertEqual(self.live.settle_due(), [])
        replay = self.bid(0, 5, request_id=request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["closes_at"], first["closes_at"])
        budget = self.live.get_state(self.event)["teams"][0]["budget"]
        for attempt in (
            lambda: self.bid(0, 10),
            lambda: self.bid(0, budget + 1),
            lambda: self.bid(0, 15, request_id=request),
            lambda: self.live.place_bid(self.player_token, self.event, lot["id"], 15, str(uuid.uuid4())),
        ):
            with self.assertRaises((ValueError, PermissionError)):
                attempt()
            self.assertEqual(self.live.get_state(self.event)["current_lot"]["closes_at"], second["closes_at"])
        self.clock.value = second["closes_at"]
        with self.assertRaisesRegex(ValueError, "마감"):
            self.bid(0, 15)
        self.assertEqual(self.live.settle_due(), [self.event])
        state = self.live.get_state(self.event)
        self.assertEqual(state["current_lot"]["status"], "SOLD")
        self.assertEqual(state["current_lot"]["closes_at"], original_deadline + 10)
        self.assertEqual(state["current_lot"]["highest_team_id"], state["teams"][1]["id"])
        self.assertEqual(len(state["bids"]), 2)

    def test_default_ten_second_cap_and_legacy_settings_are_normalized(self):
        self.live.configure(self.admin, self.event)
        for saved, expected in ((1, 5), (7, 10), (37, 30), (120, 30)):
            with self.core.transaction() as db:
                db.execute("UPDATE live_sessions SET bid_seconds=? WHERE event_id=?", (saved, self.event))
            configured = self.live.get_state(self.event)
            self.assertEqual(configured["bid_seconds"], expected)
            self.assertEqual(configured["max_remaining_seconds"], expected)
        with self.core.transaction() as db:
            db.execute("UPDATE live_sessions SET bid_seconds=10,transition_seconds=30,reset_on_bid=0 WHERE event_id=?", (self.event,))
        configured = self.live.get_state(self.event)
        self.assertEqual(configured["bid_seconds"], 10)
        self.assertEqual(configured["transition_seconds"], 3)
        start = self.clock()
        lot = self.live.start(self.admin, self.event)["current_lot"]
        self.assertEqual(lot["closes_at"], start + 10)
        self.clock.advance(3)  # 7 -> 10, not 12.
        first = self.bid(0, 5)
        self.assertEqual(first["closes_at"], start + 13)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["remaining_seconds"], 10)
        self.clock.advance(8)  # 2 -> 7.
        second = self.bid(1, 10)
        self.assertEqual(second["closes_at"], start + 18)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["remaining_seconds"], 7)
        self.clock.value = first["closes_at"]
        self.assertEqual(self.live.settle_due(), [])
        self.clock.advance(4)  # 1 -> 6.
        third = self.bid(0, 15)
        self.assertEqual(third["closes_at"], start + 23)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["remaining_seconds"], 6)
        self.clock.value = second["closes_at"]
        self.assertEqual(self.live.settle_due(), [])
        self.close()
        state = self.live.get_state(self.event)
        self.assertEqual(state["next_at"], third["closes_at"] + 3)
        self.assertEqual(state["current_lot"]["status"], "SOLD")
        next_lot = self.next()
        self.assertEqual(next_lot["closes_at"] - self.clock(), 10)
        # A running legacy lot is capped when its next valid bid is accepted.
        with self.core.transaction() as db:
            db.execute("UPDATE live_lots SET closes_at=? WHERE id=?", (self.clock() + 120, next_lot["id"]))
        capped = self.bid(1, 0)
        self.assertEqual(capped["closes_at"] - self.clock(), 10)

    def test_each_initial_time_option_caps_extensions_at_its_selected_value(self):
        for seconds in (5, 10, 15, 20, 25, 30):
            with self.subTest(seconds=seconds):
                self.event = self.comp.create_auction(self.admin, self.ids, self.captains)
                with self.core.transaction() as db:
                    db.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (self.event,))
                lot = self.start(bid_seconds=seconds)
                self.assertEqual(lot["remaining_seconds"], seconds)
                self.clock.advance(2)
                first = self.bid(0, 0)
                self.assertEqual(first["closes_at"] - self.clock(), seconds)
                self.assertEqual(self.live.get_state(self.event)["max_remaining_seconds"], seconds)
                self.clock.value = first["closes_at"] - 1
                second = self.bid(1, 5)
                self.assertEqual(second["closes_at"] - self.clock(), min(6, seconds))

    def test_custom_starting_budgets_persist_and_control_real_bids(self):
        original = self.comp.get_event(self.event)
        old_budgets = {team["id"]: team["budget"] for team in original["teams"]}
        budgets = dict(zip(old_budgets, (0, 100, 700, 1500)))
        configured = self.live.configure(self.admin, self.event, team_budgets=budgets)
        self.assertEqual({team["id"]: team["remaining"] for team in configured["teams"]}, budgets)
        detail = json.loads(configured["events"][0]["detail"])
        self.assertEqual(detail["team_budgets_before"], {str(key): value for key, value in old_budgets.items()})
        self.assertEqual(detail["team_budgets"], {str(key): value for key, value in budgets.items()})
        self.assertEqual(self.comp.get_event(self.event)["policy_snapshot"], original["policy_snapshot"])
        reconnected = LiveAuction(Core(self.path), Competition(self.core), clock=self.clock)
        saved = reconnected.configure(self.admin, self.event, bid_seconds=15)
        self.assertEqual({team["id"]: team["budget"] for team in saved["teams"]}, budgets)
        self.live.start(self.admin, self.event)
        with self.assertRaisesRegex(ValueError, "예산"):
            self.bid(0, 1)
        self.bid(0, 0)
        with self.assertRaisesRegex(ValueError, "예산"):
            self.bid(1, 101)
        self.bid(1, 100)
        self.close()
        sold = self.live.get_state(self.event)
        self.assertEqual(sold["teams"][1]["remaining"], 0)
        bought = next(player for player in sold["teams"][1]["players"] if player["member_id"] == sold["current_lot"]["member_id"])
        self.assertEqual(bought["price"], 100)
        with self.assertRaises(ValueError):
            self.live.configure(self.admin, self.event, team_budgets=old_budgets)
        self.assertEqual({team["id"]: team["budget"] for team in self.live.get_state(self.event)["teams"]}, budgets)

    def test_budget_settings_reject_bad_values_and_unauthorized_changes_atomically(self):
        original = self.live.configure(self.admin, self.event)
        budgets = {team["id"]: team["budget"] for team in original["teams"]}
        first_team = next(iter(budgets))
        invalid = [[], {}, {key: value for key, value in budgets.items() if key != first_team},
                   {**budgets, 999999: 100}, {**budgets, str(first_team): 100}]
        invalid.extend({**budgets, first_team: value} for value in (-1, True, 1.5, "1.5", 10**30))
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.live.configure(self.admin, self.event, bid_seconds=5, team_budgets=values)
            state = self.live.get_state(self.event)
            self.assertEqual(state["bid_seconds"], 10)
            self.assertEqual({team["id"]: team["budget"] for team in state["teams"]}, budgets)
            self.assertEqual([lot["id"] for lot in state["lots"]], [lot["id"] for lot in original["lots"]])
            self.assertEqual(len(state["events"]), len(original["events"]))
        for token in (self.tokens[0], self.player_token, self.host, "invented"):
            with self.subTest(token=token), self.assertRaises((ValueError, PermissionError)):
                self.live.configure(token, self.event, team_budgets={key: 0 for key in budgets})
        for seconds in (0, -1, 1, 7, 11, 31, 120, True, 1.5):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.live.configure(self.admin, self.event, bid_seconds=seconds)

    def test_deadline_is_checked_after_waiting_for_transaction_lock(self):
        lot = self.start()
        waiting = threading.Event()

        def blocked_bid():
            waiting.set()
            return self.bid(0, 0, lot=lot)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.core.transaction():
                future = pool.submit(blocked_bid)
                self.assertTrue(waiting.wait(timeout=2))
                self.clock.value = lot["closes_at"]
            with self.assertRaises(ValueError):
                future.result(timeout=3)
        self.assertEqual(self.live.get_state(self.event)["bids"], [])
        self.live.settle_due()
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["status"], "UNSOLD")

    def test_pause_at_cutoff_settles_first_and_legacy_assignment_is_blocked(self):
        lot = self.start()
        team_id = self.live.get_state(self.event)["teams"][0]["id"]
        for operation in (
            lambda: self.comp.bid(self.admin, self.event, lot["member_id"], team_id, 0),
            lambda: self.comp.mark_unsold(self.admin, self.event, lot["member_id"]),
            lambda: self.comp.set_player_role(self.admin, self.event, lot["member_id"], "TOP"),
            lambda: self.comp.finalize_auction(self.admin, self.event),
        ):
            with self.assertRaises(ValueError):
                operation()
        receipt = self.bid(0, 0)
        self.clock.value = receipt["closes_at"]
        state = self.live.pause(self.admin, self.event)
        self.assertEqual(state["current_lot"]["status"], "SOLD")
        self.assertEqual(state["paused_phase"], "WAITING")
        self.assertEqual(state["pause_remaining"], 3)

    def test_budget_capacity_and_completed_auction_do_not_create_bracket(self):
        self.start()
        budget = self.live.get_state(self.event)["teams"][0]["budget"]
        with self.assertRaises(ValueError):
            self.bid(0, budget + 1)
        for index in range(16):
            team = index // 4
            amount = budget if index == 0 else 0
            if index == 1:
                with self.assertRaises(ValueError):
                    self.bid(0, 1)
            if index == 4:
                with self.assertRaises(ValueError):
                    self.bid(0, 0)
            self.bid(team, amount)
            self.close()
            if index != 15:
                self.next()
        state = self.live.get_state(self.event)
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual([len(t["players"]) for t in state["teams"]], [5] * 4)
        self.assertEqual(state["teams"][0]["remaining"], 0)
        event = self.comp.get_event(self.event)
        self.assertEqual(event["status"], "BRACKET_SETUP")
        self.assertEqual(event["games"], [])
        self.assertEqual(self.core.list_games(), [])

    def test_unsold_needs_explicit_retry_and_old_attempt_remains(self):
        self.start()
        for index in range(16):
            self.close()
            if index != 15:
                self.next()
        state = self.live.get_state(self.event)
        self.assertEqual(state["unsold_count"], 16)
        self.assertEqual(state["status"], "WAITING")
        self.assertIsNone(state["next_at"])
        self.clock.advance(1000)
        self.assertEqual(self.live.settle_due(), [])
        state = self.live.retry_unsold(self.admin, self.event, [self.pool[1], self.pool[0]])
        self.assertEqual(state["queued_count"], 2)
        retry_order = [row["member_id"] for row in state["lots"] if row["status"] == "QUEUED"]
        self.assertEqual(set(retry_order), {self.pool[0], self.pool[1]})
        with self.assertRaises(ValueError):
            self.live.retry_unsold(self.admin, self.event, [self.pool[1]])
        lot = self.next()
        self.assertEqual(lot["member_id"], retry_order[0])
        self.assertEqual(lot["attempt"], 2)
        self.bid(0, 0)
        self.close()
        attempts = [row for row in self.live.get_state(self.event)["lots"] if row["member_id"] == retry_order[0]]
        self.assertEqual([row["status"] for row in attempts], ["UNSOLD", "SOLD"])

    def test_retry_rejects_open_lot_and_paused_or_running_transition(self):
        self.start()
        with self.assertRaises(ValueError):
            self.live.retry_unsold(self.admin, self.event)
        self.live.pause(self.admin, self.event)
        with self.assertRaises(ValueError):
            self.live.retry_unsold(self.admin, self.event)
        self.live.resume(self.admin, self.event)
        self.close()
        with self.assertRaises(ValueError):
            self.live.retry_unsold(self.admin, self.event)
        self.live.pause(self.admin, self.event)
        state = self.live.get_state(self.event)
        self.assertEqual(state['paused_phase'], 'WAITING')
        self.assertEqual(state['pause_remaining'], 3)
        with self.assertRaises(ValueError):
            self.live.retry_unsold(self.admin, self.event)
        self.assertEqual(len(self.live.get_state(self.event)['lots']), 16)

    def test_paused_finished_round_retries_unsold_once_then_repeats_until_completion(self):
        self.start()
        sold_member = self.pool[0]
        self.bid(0, 10)
        for index in range(16):
            self.close()
            if index != 15:
                self.next()
        paused = self.live.pause(self.admin, self.event)
        self.assertEqual((paused['status'], paused['paused_phase'], paused['pause_remaining']), ('PAUSED', 'WAITING', None))
        old_lots = [dict(lot) for lot in paused['lots']]
        barrier = threading.Barrier(2)

        def retry():
            barrier.wait(timeout=5)
            try:
                self.live.retry_unsold(self.admin, self.event)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(retry) for _ in range(2)]
            self.assertEqual(sum(future.result(timeout=10) for future in futures), 1)
        state = self.live.get_state(self.event)
        queued = [lot for lot in state['lots'] if lot['status'] == 'QUEUED']
        self.assertEqual({lot['member_id'] for lot in queued}, set(self.pool) - {sold_member})
        self.assertEqual(len(queued), 15)
        self.assertTrue(all(lot['attempt'] == 2 for lot in queued))
        self.assertEqual(state['lots'][:16], old_lots)
        self.assertEqual((state['status'], state['next_at'], state['paused_phase'], state['pause_remaining']), ('WAITING', self.clock.value + 3, None, None))
        self.assertEqual(sum(item['type'] == 'RETRY_UNSOLD' for item in state['events']), 1)
        self.clock.advance(2.9)
        self.live.settle_due()
        self.assertEqual(self.live.get_state(self.event)['queued_count'], 15)
        self.clock.advance(.1)
        self.live.settle_due()
        for index in range(15):
            self.close()
            if index != 14:
                self.next()
        self.assertEqual(self.live.get_state(self.event)['unsold_count'], 15)
        self.live.retry_unsold(self.admin, self.event)
        self.next()
        for index in range(15):
            state = self.live.get_state(self.event)
            team = next(i for i, team in enumerate(state['teams']) if len(team['players']) < 5)
            self.bid(team, 0)
            self.close()
            if index != 14:
                self.next()
        finished = self.live.get_state(self.event)
        self.assertEqual((finished['status'], finished['event']['status']), ('COMPLETED', 'BRACKET_SETUP'))
        self.assertEqual([len(team['players']) for team in finished['teams']], [5, 5, 5, 5])
        self.assertEqual(finished['unsold_count'], 0)
        self.assertEqual(len([lot for lot in finished['lots'] if lot['member_id'] == sold_member]), 1)
        self.assertEqual(len(finished['bids']), 16)

    def test_restart_and_worker_close_without_browser_and_single_worker_per_db(self):
        self.start()
        self.bid(2, 5)
        self.clock.advance(20)
        recovered = LiveAuction(Core(self.path), Competition(self.core), clock=self.clock)
        self.assertTrue(recovered.has_active_sessions())
        worker = recovered.ensure_worker(interval=.02)
        self.assertIs(worker, self.live.ensure_worker(interval=.02))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with closing(self.core.connect()) as db:
                status = db.execute("SELECT status FROM live_lots WHERE event_id=? ORDER BY sequence LIMIT 1", (self.event,)).fetchone()[0]
            if status == "SOLD":
                break
            time.sleep(.01)
        self.assertEqual(status, "SOLD")
        recovered.stop_worker()
        self.assertFalse(worker.is_alive())
        self.clock.advance(3)
        second = LiveAuction(Core(self.path), self.comp, clock=self.clock)
        second.settle_due()
        self.assertEqual(second.get_state(self.event)["current_lot"]["member_id"], self.pool[1])

    def test_demo_worker_retires_only_after_sessions_are_inactive(self):
        empty = self.live.ensure_worker(interval=.01)
        empty.join(timeout=2)
        self.assertFalse(empty.is_alive())
        self.assertNotIn(self.core.db_path, LiveAuction._workers)

        self.live.configure(self.admin, self.event)
        worker = self.live.ensure_worker(interval=.01)
        worker.join(timeout=.05)
        self.assertTrue(worker.is_alive(), "READY must retain its worker")
        self.live.start(self.admin, self.event)
        self.live.pause(self.admin, self.event)
        worker.join(timeout=.05)
        self.assertTrue(worker.is_alive(), "PAUSED must retain its worker")
        self.live.resume(self.admin, self.event)
        for index in range(len(self.pool)):
            self.close()
            if index < len(self.pool) - 1:
                self.next()
        waiting = self.live.get_state(self.event)
        self.assertEqual(waiting["status"], "WAITING")
        self.assertIsNone(waiting["next_at"])
        worker.join(timeout=.05)
        self.assertTrue(worker.is_alive(), "Unsold players can still be retried")
        self.comp.cancel_event(self.admin, self.event, "All players declined")
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.live.get_state(self.event)["status"], "CANCELLED")
        self.assertNotIn(self.core.db_path, LiveAuction._workers)

    def test_launcher_worker_stays_on_empty_db_and_promotes_existing_worker(self):
        persistent = self.live.ensure_worker(interval=.01, persistent=True)
        persistent.join(timeout=.05)
        self.assertTrue(persistent.is_alive())
        self.assertIs(persistent, self.live.ensure_worker(interval=.01))
        self.live.stop_worker()
        self.assertFalse(persistent.is_alive())

        self.live.configure(self.admin, self.event)
        demo = self.live.ensure_worker(interval=.01)
        self.assertIs(demo, self.live.ensure_worker(interval=.01, persistent=True))
        self.comp.cancel_event(self.admin, self.event, "Setup cancelled")
        demo.join(timeout=.05)
        self.assertTrue(demo.is_alive(), "Persistent promotion must not be lost")
        self.assertIs(demo, self.live.ensure_worker(interval=.01))

    def test_ensure_during_idle_check_keeps_newly_configured_session_running(self):
        checked = threading.Event()
        release = threading.Event()
        rechecked = threading.Event()
        original = self.live.has_active_sessions

        def delayed_check():
            if not checked.is_set():
                old_result = original()
                checked.set()
                if not release.wait(timeout=3):
                    raise RuntimeError("worker check was not released")
                return old_result
            result = original()
            rechecked.set()
            return result

        with patch.object(self.live, "has_active_sessions", side_effect=delayed_check):
            worker = self.live.ensure_worker(interval=.01)
            try:
                self.assertTrue(checked.wait(timeout=2))
                self.live.configure(self.admin, self.event)
                self.assertIs(worker, self.live.ensure_worker(interval=.01))
            finally:
                release.set()
            self.assertTrue(rechecked.wait(timeout=2))
            self.assertTrue(worker.is_alive())
            self.assertIs(worker, self.live.ensure_worker(interval=.01))

    def test_final_settlement_revalidates_member_and_event_cancellation(self):
        lot = self.start()
        self.bid(0, 0)
        self.core.kick_member(self.admin, lot["member_id"], "membership ended")
        self.close()
        state = self.live.get_state(self.event)
        self.assertEqual(state["current_lot"]["status"], "UNSOLD")
        self.assertEqual(len(state["teams"][0]["players"]), 1)
        with self.core.transaction() as db:
            db.execute("UPDATE competition_events SET status='CANCELLED' WHERE id=?", (self.event,))
        self.live.settle_due()
        state = self.live.get_state(self.event)
        self.assertEqual(state["status"], "CANCELLED")
        self.assertFalse(any(lot["status"] in ("OPEN", "QUEUED") for lot in state["lots"]))
        with self.assertRaises(ValueError):
            self.live.resume(self.admin, self.event)

    def test_configuration_and_public_snapshot_have_no_account_secrets(self):
        self.assertIsNone(self.live.get_state(self.event))
        with self.assertRaises(ValueError):
            self.live.configure(self.admin, self.event, order=self.pool[:-1])
        state = self.live.configure(self.admin, self.event, bid_seconds=5)
        self.assertIsNone(state["current_lot"])
        self.assertEqual(state["bid_seconds"], 5)
        self.live.configure(self.admin, self.event, bid_seconds=10, order=list(reversed(self.pool)))
        self.live.start(self.admin, self.event)
        with self.assertRaises(ValueError):
            self.live.configure(self.admin, self.event, bid_seconds=5)
        self.bid(0, 0)
        state = self.live.get_state(self.event)
        self.assertIn("team_name", state["bids"][0])
        for secret in ("password_hash", "salt", "token_hash", "notes", "canonical_id"):
            self.assertNotIn(secret, repr(state))

    def test_recruitment_to_live_auction_keeps_excluded_history_out_of_queue(self):
        preparation = TournamentService(self.core, self.comp)
        event_id = preparation.create(self.admin, "New staged auction", "2026-09-10T20:00:00+09:00", build_mode="AUCTION", team_count=4)
        preparation.open_recruitment(self.admin, event_id)
        extra = self.core.join_member("Replaced#KR1", "SUP", "TOP")
        self.core.approve_member(self.admin, extra, 100)
        first = self.ids[:-1] + [extra]
        preparation.set_participants(self.admin, event_id, [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(first)])
        preparation.set_participants(self.admin, event_id, [{"member_id": mid, "role": ROLES[index % 5]} for index, mid in enumerate(self.ids)])
        preparation.confirm_participants(self.admin, event_id)
        preparation.set_captains(self.admin, event_id, self.captains)
        preparation.prepare_auction(self.admin, event_id)
        self.live.configure(self.admin, event_id)
        state = self.live.start(self.admin, event_id)
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(len(state["lots"]), 16)
        self.assertNotIn(extra, {lot["member_id"] for lot in state["lots"]})
        self.assertEqual(self.comp.get_event(event_id)["status"], "AUCTION")


if __name__ == "__main__":
    unittest.main()
