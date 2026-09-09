"""Persisted random draws and atomic admin sale corrections on disposable DBs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from roly.competition import Competition, ROLES
from roly.core import Core
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService


class Clock:
    def __init__(self):
        self.value = 2_000_000_000.0

    def __call__(self):
        return self.value


class LiveCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dir = tempfile.TemporaryDirectory(prefix="roly-correction-base-")
        cls.base = Core(Path(cls.base_dir.name) / "synthetic.sqlite3")
        password = secrets.token_urlsafe(24)
        cls.base.setup_admin("admin", password)
        cls.admin = cls.base.login("admin", password)
        cls.members = []
        for index in range(20):
            member = cls.base.join_member(f"Correction{index}#TEST", ROLES[index % 5], ROLES[(index + 1) % 5])
            cls.base.approve_member(cls.admin, member, 100)
            cls.members.append(member)
        cls.captains = cls.members[::5]
        cls.tokens = []
        for index, member in enumerate(cls.captains):
            password = secrets.token_urlsafe(24)
            username = f"captain{index}"
            cls.base.create_account(cls.admin, username, password, role="member", member_id=member)
            cls.tokens.append(cls.base.login(username, password))
        password = secrets.token_urlsafe(24)
        cls.base.create_account(cls.admin, "host", password)
        cls.host = cls.base.login("host", password)

    @classmethod
    def tearDownClass(cls):
        cls.base_dir.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="roly-correction-case-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.sqlite3"
        with closing(self.base.connect()) as source, closing(sqlite3.connect(self.path)) as target:
            source.backup(target)
        self.core = Core(self.path)
        self.comp = Competition(self.core)
        self.service = TournamentService(self.core, self.comp)
        self.clock = Clock()
        self.live = LiveAuction(self.core, self.comp, clock=self.clock)
        self.addCleanup(self.live.stop_worker)
        self.event = self.service.create(self.admin, "Synthetic correction", "2030-01-01T20:00:00+09:00",
                                         build_mode="AUCTION", team_count=4, format_name="TOURNAMENT")
        self.service.open_recruitment(self.admin, self.event)
        self.service.set_participants(self.admin, self.event, [
            {"member_id": member, "role": ROLES[index % 5]} for index, member in enumerate(self.members)
        ])
        self.service.confirm_participants(self.admin, self.event)
        self.service.set_captains(self.admin, self.event, self.captains)
        self.service.prepare_auction(self.admin, self.event)
        self.pool = [member for member in self.members if member not in self.captains]
        self.teams = self.comp.get_event(self.event)["teams"]
        self.budgets = {team["id"]: 100 for team in self.teams}

    def state(self):
        return self.live.get_state(self.event)

    def start(self):
        self.live.configure(self.admin, self.event, order=self.pool, team_budgets=self.budgets)
        return self.live.start(self.admin, self.event)["current_lot"]

    def sell(self, team=0, amount=10):
        lot = self.state()["current_lot"]
        receipt = self.live.place_bid(self.tokens[team], self.event, lot["id"], amount, str(uuid.uuid4()))
        self.clock.value = receipt["closes_at"]
        self.live.settle_due()
        return lot["id"]

    def next_lot(self):
        self.clock.value = self.state()["next_at"]
        self.live.settle_due()
        return self.state()["current_lot"]

    def paused_sale(self):
        self.start()
        lot_id = self.sell(amount=40)
        self.live.pause(self.admin, self.event)
        return lot_id

    def complete(self):
        self.start()
        for _ in range(32):
            state = self.state()
            if state["status"] == "COMPLETED":
                return state
            if state["status"] == "WAITING":
                self.next_lot()
            else:
                role = state["current_lot"]["role"]
                index = next(index for index, team in enumerate(state["teams"])
                             if role not in {player["role"] for player in team["players"]})
                self.sell(index)
        self.fail("Auction did not complete within its bounded queue")

    def request(self, lot_id, team_id=None, amount=0):
        preview = self.live.preview_sale_correction(self.admin, self.event, lot_id, team_id=team_id, amount=amount)
        return preview, dict(team_id=team_id, amount=amount, reason="Synthetic correction reason",
                             request_id=str(uuid.uuid4()), expected_fingerprint=preview["fingerprint"])

    def corrections(self):
        return [row for row in self.state()["events"] if row["type"] == "SALE_CORRECTION"]

    def test_random_default_is_persisted_across_reads_settings_and_reconnect(self):
        with patch("roly.live_auction.secrets.SystemRandom") as generator:
            generator.return_value.shuffle.side_effect = lambda sequence: sequence.reverse()
            initial = self.live.configure(self.admin, self.event)
            stored = [lot["member_id"] for lot in initial["lots"]]
            self.assertEqual(stored, list(reversed(self.pool)))
            self.assertEqual(len(set(stored)), 16)
            self.assertTrue(set(stored).isdisjoint(self.captains))
            for _ in range(2):
                self.assertEqual([lot["member_id"] for lot in self.state()["lots"]], stored)
            saved = self.live.configure(self.admin, self.event, bid_seconds=15)
            restored = LiveAuction(Core(self.path), self.comp, clock=self.clock).get_state(self.event)
            self.assertEqual([lot["member_id"] for lot in saved["lots"]], stored)
            self.assertEqual([lot["member_id"] for lot in restored["lots"]], stored)
            self.assertEqual(self.live.start(self.admin, self.event)["current_lot"]["member_id"], stored[0])
            generator.return_value.shuffle.assert_called_once()

    def test_explicit_fixture_order_and_random_retry_do_not_remix_old_lots(self):
        with patch("roly.live_auction.secrets.SystemRandom") as generator:
            self.start()
            generator.assert_not_called()
        for index in range(16):
            self.clock.value = self.state()["current_lot"]["closes_at"]
            self.live.settle_due()
            if index != 15:
                self.next_lot()
        original = self.state()["lots"]
        chosen = self.pool[:4]
        with patch("roly.live_auction.secrets.SystemRandom") as generator:
            generator.return_value.shuffle.side_effect = lambda sequence: sequence.reverse()
            state = self.live.retry_unsold(self.admin, self.event, chosen)
        self.assertEqual(state["lots"][:16], original)
        self.assertEqual([lot["member_id"] for lot in state["lots"][16:]], list(reversed(chosen)))
        self.assertEqual([lot["attempt"] for lot in state["lots"][16:]], [2] * 4)
        self.assertEqual(self.next_lot()["member_id"], chosen[-1])

    def test_admin_reassign_refunds_preserves_bids_and_replays_after_resume(self):
        lot_id = self.paused_sale()
        before = self.state()
        preview, request = self.request(lot_id, self.teams[1]["id"], 25)
        self.assertEqual(self.state(), before)
        self.assertEqual((preview["team_balances"][0]["before"], preview["team_balances"][0]["after"]), (60, 100))
        self.assertEqual((preview["team_balances"][1]["before"], preview["team_balances"][1]["after"]), (100, 75))
        receipt = self.live.correct_sale(self.admin, self.event, lot_id, **request)
        after = self.state()
        self.assertEqual([team["remaining"] for team in after["teams"]], [100, 75, 100, 100])
        self.assertEqual(after["bids"], before["bids"])
        self.assertEqual(after["current_lot"]["highest_team_id"], self.teams[1]["id"])
        self.assertEqual(after["current_lot"]["highest_bid"], 25)
        self.assertEqual(after["status"], "PAUSED")
        self.assertEqual(len(self.corrections()), 1)
        detail = json.loads(self.corrections()[0]["detail"])
        self.assertEqual(detail["before"]["amount"], 40)
        self.assertEqual(detail["after"]["amount"], 25)
        self.live.resume(self.admin, self.event)
        replay = self.live.correct_sale(self.admin, self.event, lot_id, **request)
        self.assertEqual(replay, {**receipt, "replayed": True})
        self.assertEqual(len(self.corrections()), 1)
        with self.assertRaises(ValueError):
            self.live.correct_sale(self.admin, self.event, lot_id, **{**request, "amount": 26})
        self.assertTrue(all(member["score"] == 100 for member in self.core.list_members()))

    def test_concurrent_cancel_uuid_refunds_and_appends_one_attempt(self):
        lot_id = self.paused_sale()
        original_lots = self.state()["lots"]
        _, request = self.request(lot_id)
        barrier = threading.Barrier(2)
        second = LiveAuction(Core(self.path), self.comp, clock=self.clock)

        def cancel(live):
            barrier.wait(timeout=5)
            return live.correct_sale(self.admin, self.event, lot_id, **request)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(cancel, (self.live, second)))
        self.assertEqual(sum(not receipt["replayed"] for receipt in results), 1)
        self.assertEqual(len({receipt["new_lot_id"] for receipt in results}), 1)
        state = self.state()
        self.assertEqual(len(state["lots"]), 17)
        self.assertEqual(state["lots"][1:16], original_lots[1:])
        self.assertEqual(state["lots"][0]["status"], "CANCELLED")
        self.assertIsNone(state["lots"][0]["highest_team_id"])
        self.assertEqual((state["lots"][-1]["member_id"], state["lots"][-1]["attempt"]), (self.pool[0], 2))
        self.assertEqual([team["remaining"] for team in state["teams"]], [100] * 4)
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(len(state["bids"]), 1)

    def test_concurrent_different_corrections_reject_stale_preview(self):
        lot_id = self.paused_sale()
        requests = [self.request(lot_id, self.teams[0]["id"], amount)[1] for amount in (20, 30)]
        barrier = threading.Barrier(2)

        def apply(request):
            barrier.wait(timeout=5)
            try:
                return self.live.correct_sale(self.admin, self.event, lot_id, **request)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, requests))
        accepted = [result for result in results if result is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.state()["current_lot"]["highest_bid"], accepted[0]["amount"])
        self.assertEqual(len(self.corrections()), 1)

    def test_old_preview_is_rejected_after_sale_changes_and_returns_to_same_price(self):
        lot_id = self.paused_sale()
        team_id = self.teams[0]["id"]
        _, stale = self.request(lot_id, team_id, 25)
        for amount in (30, 40):
            _, request = self.request(lot_id, team_id, amount)
            self.live.correct_sale(self.admin, self.event, lot_id, **request)
        # Even at one injected timestamp, an intervening correction invalidates
        # the old dialog after the visible winner/price happen to match again.
        with self.assertRaises(ValueError):
            self.live.correct_sale(self.admin, self.event, lot_id, **stale)
        self.assertEqual(self.state()["current_lot"]["highest_bid"], 40)
        self.assertEqual(len(self.corrections()), 2)

    def test_correction_requires_admin_pause_valid_target_and_approved_member(self):
        self.start()
        lot_id = self.sell(amount=40)
        with self.assertRaises(ValueError):
            self.live.preview_sale_correction(self.admin, self.event, lot_id)
        self.live.pause(self.admin, self.event)
        _, valid_request = self.request(lot_id)
        for token in (self.host, self.tokens[0], None, "invented"):
            with self.subTest(token_kind=token is None), self.assertRaises(PermissionError):
                self.live.preview_sale_correction(token, self.event, lot_id)
            with self.subTest(apply_token_kind=token is None), self.assertRaises(PermissionError):
                self.live.correct_sale(token, self.event, lot_id, **valid_request)
        before = self.state()
        for options in (dict(team_id=99999, amount=0), dict(team_id=self.teams[0]["id"], amount=101),
                        dict(team_id=None, amount=1), dict(team_id=self.teams[0]["id"], amount=-1),
                        dict(team_id=self.teams[0]["id"], amount=True)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.live.preview_sale_correction(self.admin, self.event, lot_id, **options)
        self.assertEqual(self.state(), before)
        for changed in (dict(reason=""), dict(reason="x" * 1001), dict(request_id="not-a-uuid"),
                        dict(expected_fingerprint="")):
            with self.subTest(invalid_field=next(iter(changed))), self.assertRaises(ValueError):
                self.live.correct_sale(self.admin, self.event, lot_id, **{**valid_request, **changed})
        self.assertEqual(self.state(), before)
        self.core.kick_member(self.admin, self.pool[0], "Synthetic membership change")
        with self.assertRaises(ValueError):
            self.live.preview_sale_correction(self.admin, self.event, lot_id)
        self.assertEqual(len(self.corrections()), 0)

    def test_foreign_event_team_is_rejected_without_refunding_original_sale(self):
        lot_id = self.paused_sale()
        other_event = self.comp.create_auction(self.admin, self.members, self.captains)
        foreign_team = self.comp.get_event(other_event)["teams"][0]["id"]
        before = self.state()
        with self.assertRaises(ValueError):
            self.live.preview_sale_correction(self.admin, self.event, lot_id, team_id=foreign_team, amount=10)
        self.assertEqual(self.state(), before)

    def test_full_team_blocks_reassignment_but_same_team_price_can_change(self):
        self.start()
        first = self.sell(team=0, amount=10)
        last = None
        for _ in range(4):
            self.next_lot()
            last = self.sell(team=1, amount=5)
        self.live.pause(self.admin, self.event)
        with self.assertRaises(ValueError):
            self.live.preview_sale_correction(self.admin, self.event, first, team_id=self.teams[1]["id"], amount=1)
        _, request = self.request(last, self.teams[1]["id"], 2)
        self.live.correct_sale(self.admin, self.event, last, **request)
        self.assertEqual(len(self.state()["teams"][1]["players"]), 5)
        self.assertEqual(self.state()["teams"][1]["remaining"], 83)

    def test_correction_preserves_open_lot_and_its_accepted_budget(self):
        self.start()
        first = self.sell(amount=40)
        opened = self.next_lot()
        self.live.place_bid(self.tokens[1], self.event, opened["id"], 70, str(uuid.uuid4()))
        self.live.pause(self.admin, self.event)
        before = self.state()
        with self.assertRaises(ValueError):
            self.live.preview_sale_correction(self.admin, self.event, first, team_id=self.teams[1]["id"], amount=40)
        _, request = self.request(first, self.teams[1]["id"], 20)
        self.live.correct_sale(self.admin, self.event, first, **request)
        after = self.state()
        self.assertEqual(after["current_lot"], before["current_lot"])
        self.assertEqual(after["pause_remaining"], before["pause_remaining"])
        self.assertEqual(after["bids"], before["bids"])
        self.live.resume(self.admin, self.event)
        self.clock.value = self.state()["current_lot"]["closes_at"]
        self.live.settle_due()
        self.assertEqual(self.state()["current_lot"]["status"], "SOLD")
        self.assertEqual(self.state()["teams"][1]["remaining"], 10)

    def test_audit_failure_rolls_back_refund_cancel_and_new_attempt(self):
        lot_id = self.paused_sale()
        _, request = self.request(lot_id)
        before = self.state()
        with patch.object(self.comp, "_audit", side_effect=RuntimeError("Synthetic audit failure")):
            with self.assertRaises(RuntimeError):
                self.live.correct_sale(self.admin, self.event, lot_id, **request)
        self.assertEqual(self.state(), before)
        self.live.correct_sale(self.admin, self.event, lot_id, **request)
        self.assertEqual(len(self.corrections()), 1)

    def test_completed_price_change_and_cancel_need_explicit_resume_and_three_seconds(self):
        completed = self.complete()
        lot_id = completed["lots"][0]["id"]
        team_id = completed["lots"][0]["highest_team_id"]
        _, price_request = self.request(lot_id, team_id, 7)
        self.live.correct_sale(self.admin, self.event, lot_id, **price_request)
        self.assertEqual(self.state()["status"], "COMPLETED")
        _, cancel_request = self.request(lot_id)
        receipt = self.live.correct_sale(self.admin, self.event, lot_id, **cancel_request)
        state = self.state()
        self.assertEqual((state["status"], state["paused_phase"], state["pause_remaining"]), ("PAUSED", "WAITING", 3))
        self.assertEqual(self.comp.get_event(self.event)["status"], "AUCTION")
        self.clock.value += 100
        self.assertEqual(self.live.settle_due(), [])
        self.live.resume(self.admin, self.event)
        deadline = self.state()["next_at"]
        self.clock.value = deadline - .01
        self.assertEqual(self.live.settle_due(), [])
        self.clock.value = deadline
        self.live.settle_due()
        self.assertEqual(self.state()["current_lot"]["id"], receipt["new_lot_id"])
        target_index = next(index for index, team in enumerate(self.teams) if team["id"] == team_id)
        self.sell(target_index, 8)
        self.assertEqual(self.state()["status"], "COMPLETED")
        self.assertEqual(sum(lot["status"] == "SOLD" for lot in self.state()["lots"]), 16)
        self.assertEqual(len(self.state()["bids"]), 17)
        self.assertTrue(self.live.correct_sale(self.admin, self.event, lot_id, **cancel_request)["replayed"])

    def test_bracket_creation_blocks_sale_correction_even_before_games_played(self):
        completed = self.complete()
        lot_id = completed["lots"][0]["id"]
        _, request = self.request(lot_id)
        self.service.build_bracket(self.admin, self.event)
        after_bracket = self.state()
        fixtures = self.comp.get_event(self.event)['games']
        self.assertFalse(completed['event']['has_games'])
        self.assertTrue(after_bracket['event']['has_games'])
        self.assertTrue(fixtures)
        self.assertEqual(self.core.list_games(), [])
        with self.assertRaises(ValueError):
            self.live.preview_sale_correction(self.admin, self.event, lot_id)
        with self.assertRaises(ValueError):
            self.live.correct_sale(self.admin, self.event, lot_id, **request)
        self.assertEqual(self.state(), after_bracket)
        self.assertEqual(self.comp.get_event(self.event)['games'], fixtures)
        self.assertEqual(len(self.corrections()), 0)


if __name__ == "__main__":
    unittest.main()
