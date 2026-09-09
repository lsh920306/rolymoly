"""Whole-auction reset keeps history, refunds once and rejects stale dialogs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
import threading
import unittest
import uuid
from unittest.mock import patch

from roly.core import Core
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from tests import test_live_auction as fixtures


class AuctionResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LiveAuctionTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.LiveAuctionTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.LiveAuctionTests.setUp(self)
        self.owner = self.player_token
        with self.core.transaction() as db:
            db.execute("UPDATE competition_events SET created_by=? WHERE id=?",
                       (self.core.session(self.owner, db)["id"], self.event))

    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start
    bid = fixtures.LiveAuctionTests.bid
    close = fixtures.LiveAuctionTests.close
    next = fixtures.LiveAuctionTests.next

    def state(self):
        return self.live.get_state(self.event)

    def request(self, token=None):
        preview = self.live.preview_reset(token or self.owner, self.event)
        return preview, {"reason": "처음부터 경매 재진행", "request_id": str(uuid.uuid4()),
                         "expected_fingerprint": preview["fingerprint"]}

    def dump(self):
        with closing(self.core.connect()) as db:
            return tuple(db.iterdump())

    def test_preview_is_read_only_and_only_owner_or_admin_can_reset(self):
        self.live.configure(self.admin, self.event, order=self.pool)
        before = self.dump()
        preview, arguments = self.request()
        self.assertEqual(self.live.preview_reset(self.admin, self.event), preview)
        self.assertEqual((preview["participant_count"], preview["captain_count"], preview["reset_player_count"]), (20, 4, 16))
        self.assertEqual(preview["refund_total"], 0)
        self.assertEqual(self.dump(), before)
        for token in (None, "not-a-session", self.host, self.tokens[0]):
            with self.subTest(actor=token is None):
                with self.assertRaises(PermissionError):
                    self.live.preview_reset(token, self.event)
                with self.assertRaises(PermissionError):
                    self.live.reset(token, self.event, **arguments)
        self.assertEqual(self.dump(), before)

    def test_running_reset_refunds_free_and_paid_sales_and_preserves_all_history(self):
        teams = self.comp.get_event(self.event)["teams"]
        budgets = {team["id"]: index * 100 for index, team in enumerate(teams)}
        self.start(bid_seconds=25, team_budgets=budgets)
        free = self.bid(0, 0)
        self.close()
        self.next()
        self.bid(1, 25)
        self.close()
        self.next()
        self.close()  # One unsold player.
        self.next()
        self.bid(2, 30)  # An accepted bid which has not yet settled.
        before = self.state()
        members = self.core.list_members(True)
        preview, arguments = self.request()
        self.assertEqual((preview["sold_count"], preview["bid_count"], preview["refund_total"]), (2, 3, 25))
        with patch("roly.live_auction.secrets.SystemRandom") as random:
            random.return_value.shuffle.side_effect = lambda values: values.reverse()
            receipt = self.live.reset(self.owner, self.event, **arguments)
            self.assertEqual(receipt["member_ids"], list(reversed(self.pool)))
            replay = self.live.reset(self.owner, self.event, **arguments)
            self.assertTrue(replay["replayed"])
            random.return_value.shuffle.assert_called_once()
        after = self.state()
        self.assertEqual((after["status"], after["event"]["status"], after["bid_seconds"]), ("READY", "AUCTION_READY", 25))
        for key in ("current_lot_id", "current_lot", "next_at", "paused_phase", "pause_remaining"):
            self.assertIsNone(after[key])
        self.assertEqual(after["bids"], before["bids"])
        self.assertEqual(after["events"][1:], before["events"])
        self.assertEqual(after["events"][0]["type"], "RESET")
        feed_detail = json.loads(after["events"][0]["detail"])
        self.assertNotIn("before", feed_detail)
        self.assertEqual(feed_detail["sold_count"], 2)
        with self.core.read_snapshot() as db:
            audit = json.loads(db.execute("SELECT detail FROM competition_audit WHERE event_id=? AND action='RESET'",
                                          (self.event,)).fetchone()[0])
        self.assertEqual(audit["before"]["bid_count"], 3)
        self.assertEqual(len(audit["before"]["lots"]), len(before["lots"]))
        self.assertEqual(audit["result"], receipt)
        old = {lot["id"]: lot for lot in before["lots"]}
        historical = [lot for lot in after["lots"] if lot["id"] in old]
        self.assertEqual(len(historical), len(old))
        self.assertTrue(all(lot["status"] == "CANCELLED" for lot in historical))
        for lot in historical:
            self.assertEqual((lot["highest_team_id"], lot["highest_bid"], lot["opened_at"], lot["closes_at"]),
                             tuple(old[lot["id"]][key] for key in ("highest_team_id", "highest_bid", "opened_at", "closes_at")))
        self.assertEqual(after["queued_count"], 16)
        self.assertEqual([lot["id"] for lot in after["lots"] if lot["status"] == "QUEUED"], receipt["lot_ids"])
        for team in after["teams"]:
            self.assertEqual((team["budget"], team["remaining"]), (budgets[team["id"]], budgets[team["id"]]))
            self.assertEqual([player["member_id"] for player in team["players"]], [team["captain_id"]])
        self.assertEqual(self.core.list_members(True), members)
        self.assertEqual(self.live.settle_due(), [])
        self.assertEqual(self.live.place_bid(self.tokens[0], self.event, free["lot_id"], 0, before["bids"][-1]["request_id"])["id"], free["id"])
        self.assertEqual(self.state()["bids"], before["bids"])
        self.live.start(self.owner, self.event)
        with self.assertRaises(ValueError):
            self.bid(0, 0, lot={"id": free["lot_id"]})

    def test_ready_reconfigure_after_reset_keeps_old_bid_ids_and_random_queue(self):
        self.start()
        self.bid(0, 5)
        self.close()
        before = self.state()["bids"]
        _, arguments = self.request()
        result = self.live.reset(self.owner, self.event, **arguments)
        with patch("roly.live_auction.secrets.SystemRandom") as random:
            changed = self.live.configure(self.owner, self.event, bid_seconds=30)
            random.return_value.shuffle.assert_not_called()
        self.assertEqual([lot["id"] for lot in changed["lots"] if lot["status"] == "QUEUED"], result["lot_ids"])
        self.assertEqual(changed["bids"], before)
        reordered = self.live.configure(self.owner, self.event, bid_seconds=30, order=list(reversed(result["member_ids"])))
        self.assertEqual([lot["member_id"] for lot in reordered["lots"] if lot["status"] == "QUEUED"], list(reversed(result["member_ids"])))
        self.assertEqual(reordered["bids"], before)
        with closing(self.core.connect()) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.live.start(self.owner, self.event)["bid_seconds"], 30)

    def test_pause_wait_and_completed_before_bracket_can_reset_but_built_bracket_cannot(self):
        self.start()
        self.live.pause(self.owner, self.event)
        _, arguments = self.request()
        self.live.reset(self.owner, self.event, **arguments)
        self.live.start(self.owner, self.event)
        self.close()
        self.assertEqual(self.state()["status"], "WAITING")
        _, arguments = self.request()
        self.live.reset(self.owner, self.event, **arguments)
        self.live.start(self.owner, self.event)
        for _ in range(40):
            state = self.state()
            if state["status"] == "COMPLETED":
                break
            if state["status"] == "WAITING":
                self.next()
            else:
                position = state["current_lot"]["role"]
                team = next(index for index, row in enumerate(state["teams"])
                            if position not in {player["role"] for player in row["players"]})
                self.bid(team, 0)
                self.close()
        self.assertEqual(self.state()["status"], "COMPLETED")
        preview, arguments = self.request()
        self.assertEqual((preview["sold_count"], preview["refund_total"]), (16, 0))
        completed_copy = self.path.with_name("completed-copy.sqlite3")
        with closing(self.core.connect()) as source, closing(sqlite3.connect(completed_copy)) as target:
            source.backup(target)
        copied_core = Core(completed_copy)
        copied_live = LiveAuction(copied_core, Competition(copied_core), clock=self.clock)
        copied_result = copied_live.reset(self.owner, self.event, **arguments)
        copied_state = copied_live.get_state(self.event)
        self.assertEqual((copied_result["status"], copied_state["event"]["status"], copied_state["queued_count"]),
                         ("READY", "AUCTION_READY", 16))
        self.assertTrue(all(len(team["players"]) == 1 and team["remaining"] == team["budget"] for team in copied_state["teams"]))
        TournamentService(self.core, self.comp).build_bracket(self.owner, self.event)
        before = self.dump()
        with self.assertRaisesRegex(ValueError, "대진"):
            self.live.reset(self.owner, self.event, **arguments)
        self.assertEqual(self.dump(), before)

    def test_stale_bid_and_pause_resume_aba_reject_even_at_identical_clock(self):
        self.start()
        _, arguments = self.request()
        self.bid(0, 0)
        before = self.dump()
        with self.assertRaisesRegex(ValueError, "미리보기 이후"):
            self.live.reset(self.owner, self.event, **arguments)
        self.assertEqual(self.dump(), before)
        _, arguments = self.request()
        self.live.pause(self.owner, self.event)
        self.live.resume(self.owner, self.event)
        with self.assertRaisesRegex(ValueError, "미리보기 이후"):
            self.live.reset(self.owner, self.event, **arguments)

    def test_concurrent_same_uuid_resets_once_and_payload_or_actor_reuse_is_rejected(self):
        self.start()
        _, arguments = self.request()
        with patch("roly.live_auction.secrets.SystemRandom") as random:
            random.return_value.shuffle.side_effect = lambda values: values.reverse()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.live.reset(self.owner, self.event, **arguments), range(2)))
            random.return_value.shuffle.assert_called_once()
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(results[0]["lot_ids"], results[1]["lot_ids"])
        self.assertEqual(self.state()["queued_count"], 16)
        self.assertEqual(sum(event["type"] == "RESET" for event in self.state()["events"]), 1)
        for actor, changed in ((self.admin, arguments), (self.owner, {**arguments, "reason": "다른 요청"})):
            with self.assertRaises(ValueError):
                self.live.reset(actor, self.event, **changed)
        self.live.start(self.owner, self.event)
        self.assertTrue(self.live.reset(self.owner, self.event, **arguments)["replayed"])
        self.assertEqual(self.state()["status"], "RUNNING")

    def test_simultaneous_bid_and_reset_have_one_consistent_winner(self):
        self.start()
        lot = self.state()["current_lot"]
        _, arguments = self.request()
        gate = threading.Barrier(2)
        def reset():
            gate.wait()
            try:
                self.live.reset(self.owner, self.event, **arguments)
                return "reset"
            except ValueError:
                return "stale"
        def bid():
            gate.wait()
            try:
                self.bid(0, 0, lot=lot)
                return "bid"
            except ValueError:
                return "closed"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(reset), pool.submit(bid)
            result = first.result(timeout=10), second.result(timeout=10)
        self.assertIn(result, (("reset", "closed"), ("stale", "bid")))
        state = self.state()
        self.assertEqual((state["status"], len(state["bids"])), ("READY", 0) if result[0] == "reset" else ("RUNNING", 1))

    def test_audit_failure_rolls_back_refund_history_queue_and_session(self):
        self.start()
        self.bid(0, 20)
        self.close()
        _, arguments = self.request()
        before = self.dump()
        with patch.object(self.comp, "_audit", side_effect=sqlite3.OperationalError("synthetic audit failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.live.reset(self.owner, self.event, **arguments)
        self.assertEqual(self.dump(), before)
        self.assertFalse(self.live.reset(self.owner, self.event, **arguments)["replayed"])

    def test_changed_membership_or_cancelled_event_rejects_without_reset(self):
        self.start()
        _, arguments = self.request()
        self.core.kick_member(self.admin, self.pool[2], "회원 상태 변경")
        before = self.dump()
        with self.assertRaisesRegex(ValueError, "승인"):
            self.live.reset(self.owner, self.event, **arguments)
        self.assertEqual(self.dump(), before)
        self.comp.cancel_event(self.admin, self.event, "경매 취소")
        with self.assertRaises(ValueError):
            self.live.preview_reset(self.admin, self.event)

    def test_repeated_ready_reset_uses_new_ids_and_attempts_even_at_one_clock(self):
        self.start()
        self.bid(0, 0)
        self.close()
        # Reset again at one clock; no earlier lot generation may be reused.
        _, first_args = self.request()
        first = self.live.reset(self.owner, self.event, **first_args)
        _, second_args = self.request()
        second = self.live.reset(self.admin, self.event, **second_args)
        self.assertTrue(set(first["lot_ids"]).isdisjoint(second["lot_ids"]))
        self.assertEqual(self.state()["queued_count"], 16)
        self.assertEqual({lot["attempt"] for lot in self.state()["lots"] if lot["status"] == "QUEUED"}, {3})
        self.assertEqual(len({lot["sequence"] for lot in self.state()["lots"]}), 48)
