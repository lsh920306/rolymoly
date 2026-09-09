"""Auction polling keeps fresh authorization and one consistent public snapshot."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from roly.live_auction import LiveAuction
from roly.member_ranks import save_riot_profile
from roly.riot_profile import attach_profile
from tests import test_live_auction as fixtures


class BatchedSQLite:
    """Exercise the PG batch contract with real local SQL and no driver/network."""
    def __init__(self, connection, batches, after_actor=None, on_close=None):
        self.connection = connection
        self.batches = batches
        self.after_actor = after_actor
        self.on_close = on_close

    def fetch_snapshot_batches(self, statements):
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")
        try:
            return self.fetch_batches(statements)
        finally:
            self.connection.rollback()

    def close(self):
        self.connection.close()
        if self.on_close:
            self.on_close()

    def fetch_batches(self, statements):
        self.batches.append(statements)
        result = []
        for query, params in statements:
            if "clock_timestamp()" in query:
                result.append([(2_000_000_000.0,)])
            else:
                result.append(self.connection.execute(query, params or ()).fetchall())
            if self.after_actor and "FROM sessions s" in query:
                self.after_actor()
        return result


class LiveViewReadTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    setUp = fixtures.LiveAuctionTests.setUp
    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start

    def pg_view(self, after_actor=None):
        batches = []

        @contextmanager
        def snapshot():
            with self.core.read_snapshot() as db:
                yield BatchedSQLite(db, batches, after_actor)

        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path,
                                 read_snapshot=snapshot,
                                 connect=lambda: BatchedSQLite(self.core.connect(), batches, after_actor))
        return LiveAuction(facade, self.comp), batches

    def cache(self, member_id):
        member = self.core.get_member(member_id)
        payload = {"puuid": "private-profile-id", "current_tier": "골드 2", "lp": 30,
                   "flex_current_tier": "플래티넘 4", "flex_lp": 45,
                   "rank_wins": 12, "rank_losses": 8, "flex_rank_wins": 9,
                   "flex_rank_losses": 3, "updated_at": "2033-05-18T03:33:20+00:00",
                   "champions": [{"id": 22, "name": "애쉬", "points": 500, "level": 3}]}
        with self.core.transaction() as db:
            save_riot_profile(db, member_id, member["canonical_id"], payload, self.clock.value)

    def test_pg_view_batches_identity_state_and_final_db_clock_without_extra_reads(self):
        self.start(bid_seconds=30)
        self.cache(self.pool[0])
        live, batches = self.pg_view()
        with patch("roly.live_auction.time.time", side_effect=AssertionError("host deadline clock")):
            actor, state = live.get_view(self.tokens[0], self.event)
        self.assertEqual(actor["member_id"], self.captains[0])
        self.assertEqual(state["current_lot"]["member_id"], self.pool[0])
        self.assertEqual(state["current_lot"]["riot_profile"]["flex_lp"], 45)
        self.assertGreaterEqual(state["server_now"], self.clock.value)
        self.assertLessEqual(state["current_lot"]["remaining_seconds"], 30)
        self.assertEqual(len(batches), 1)
        self.assertIn("FROM sessions s", batches[0][0][0])
        self.assertIn("clock_timestamp()", batches[0][-1][0])
        self.assertNotIn("private-profile-id", json.dumps((actor, state)))
        self.assertFalse(any(key.startswith("_rank_") for row in state["lots"] + state["event"]["players"] for key in row))

    def test_pg_close_elapsed_ages_running_and_transition_clocks_but_not_paused_remaining(self):
        self.start(bid_seconds=30)
        base_time = self.clock.value
        virtual = [100.0]

        @contextmanager
        def snapshot(conn=None):
            if conn is not None:
                yield conn
                return
            with self.core.read_snapshot() as db:
                yield BatchedSQLite(db, [])
            virtual[0] += 2.0  # Deterministic synchronous pool-return delay.

        def closed():
            virtual[0] += 2.0

        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path, read_snapshot=snapshot,
                                 connect=lambda: BatchedSQLite(self.core.connect(), [], on_close=closed))
        live = LiveAuction(facade, self.comp)
        for status in ("RUNNING", "PAUSED", "WAITING"):
            with self.core.transaction() as db:
                db.execute("UPDATE live_sessions SET status=?,paused_phase='RUNNING',pause_remaining=30,next_at=? WHERE event_id=?",
                           (status, base_time + 3 if status == "WAITING" else None, self.event))
            for method in ("get_view", "get_state"):
                with self.subTest(status=status, method=method), patch("roly.live_auction.time.monotonic", side_effect=lambda: virtual[0]):
                    state = live.get_view(self.tokens[0], self.event)[1] if method == "get_view" else live.get_state(self.event)
                self.assertEqual(state["server_now"], base_time + 2)
                if status == "PAUSED":
                    self.assertEqual(state["current_lot"]["remaining_seconds"], 30)
                elif status == "RUNNING":
                    self.assertEqual(state["current_lot"]["remaining_seconds"], 28)
                else:
                    self.assertEqual(state["next_in_seconds"], 1)
        # An externally owned transaction is neither closed nor aged as though
        # a pool-return round trip had occurred.
        with self.core.read_snapshot() as db:
            external = BatchedSQLite(db, [])
            with patch("roly.live_auction.time.monotonic", side_effect=lambda: virtual[0]):
                state = live.get_state(self.event, conn=external)
            self.assertEqual(state["server_now"], base_time)
            self.assertTrue(db.in_transaction)

    def test_poll_authorization_and_event_share_snapshot_then_refresh_together(self):
        self.start()
        before = self.live.get_state(self.event)["event"]["title"]

        def change():
            with self.core.transaction() as db:
                db.execute("UPDATE accounts SET active=0 WHERE id=?", (self.account_ids[0],))
                db.execute("UPDATE competition_events SET title='changed after snapshot' WHERE id=?", (self.event,))

        live, _ = self.pg_view(after_actor=change)
        actor, state = live.get_view(self.tokens[0], self.event)
        self.assertIsNotNone(actor)
        self.assertEqual(state["event"]["title"], before)
        live, _ = self.pg_view()
        actor, state = live.get_view(self.tokens[0], self.event)
        self.assertIsNone(actor)
        self.assertEqual(state["event"]["title"], "changed after snapshot")

    def test_guest_missing_event_and_invalid_session_have_no_cached_identity(self):
        self.start()
        live, batches = self.pg_view()
        actor, state = live.get_view(None, self.event)
        self.assertIsNone(actor)
        self.assertEqual(state["event"]["id"], self.event)
        self.assertFalse(any("FROM sessions s" in query for query, _ in batches[-1]))
        actor, state = live.get_view(self.admin, -1)
        self.assertEqual(actor["role"], "admin")
        self.assertIsNone(state)
        actor, state = live.get_view("not-a-session", self.event)
        self.assertIsNone(actor)
        self.assertIsNotNone(state)

    def test_repeated_resets_preserve_history_but_validate_profile_once_per_member(self):
        self.start()
        self.cache(self.pool[0])
        original_ids = [lot["id"] for lot in self.live.get_state(self.event)["lots"]]
        for _ in range(3):
            preview = self.live.preview_reset(self.admin, self.event)
            self.live.reset(self.admin, self.event, request_id=str(uuid.uuid4()),
                            expected_fingerprint=preview["fingerprint"], reason="QA reset")
        with patch("roly.riot_profile.attach_profile", wraps=attach_profile) as public:
            state = self.live.get_state(self.event)
        self.assertEqual(public.call_count, len(self.ids))
        self.assertEqual(len(state["lots"]), 64)
        self.assertTrue(all(lot["status"] == "CANCELLED" for lot in state["lots"] if lot["id"] in original_ids))
        attempts = [lot for lot in state["lots"] if lot["member_id"] == self.pool[0]]
        self.assertEqual(len(attempts), 4)
        self.assertTrue(all(lot["riot_profile"]["current_tier"] == "골드 2" for lot in attempts))
        attempts[0]["riot_profile"]["champions"].clear()
        self.assertEqual(len(attempts[-1]["riot_profile"]["champions"]), 1)

    def test_new_identity_profile_is_not_attached_to_historical_roster(self):
        self.start()
        self.cache(self.pool[0])
        member = self.core.get_member(self.pool[0])
        self.core.update_member(self.admin, member["id"], "RenamedIdentity#QA", member["main_role"],
                                member["sub_role"], member["base_score"], "verified rename")
        self.cache(self.pool[0])
        state = self.live.get_state(self.event)
        self.assertEqual(state["current_lot"]["riot_id"], member["riot_id"])
        self.assertIsNone(state["current_lot"]["riot_profile"])

    def test_supplied_transaction_is_not_closed_or_committed_by_display_read(self):
        self.start()
        with self.core.transaction() as db:
            db.execute("UPDATE competition_events SET title='inside caller' WHERE id=?", (self.event,))
            state = self.live.get_state(self.event, conn=db)
            self.assertTrue(db.in_transaction)
            self.assertEqual(state["event"]["title"], "inside caller")
            db.rollback()
        self.assertNotEqual(self.live.get_state(self.event)["event"]["title"], "inside caller")


if __name__ == "__main__":
    unittest.main()
