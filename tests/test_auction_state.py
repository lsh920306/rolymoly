"""Committed shared state, revocable bulk auth and recovery from missed wakes."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from roly import auction_state
from roly.auction_state import read_snapshot
from roly.live_auction import LiveAuction
from roly.postgres import _batch_select
from tests import test_live_auction as fixtures
from tests import test_live_view_reads as view_fixtures

class BatchedSQLite(view_fixtures.BatchedSQLite):
    """Validate with the production PG guard before running equivalent SQL."""
    def fetch_snapshot_batches(self, statements):
        for query, parameters in statements:
            _batch_select(query, parameters)
        return super().fetch_snapshot_batches(statements)


class AuctionStateTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    setUp = fixtures.LiveAuctionTests.setUp
    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start
    bid = fixtures.LiveAuctionTests.bid
    close = fixtures.LiveAuctionTests.close
    cache = view_fixtures.LiveViewReadTests.cache

    def read(self, previous=None, tokens=None):
        return read_snapshot(self.live, self.event, self.tokens if tokens is None else tokens,
                             base_state=previous["state"] if previous else None, base_version=previous)

    def test_committed_bid_is_hot_rollback_is_invisible_and_unchanged_has_no_state_reads(self):
        lot = self.start(bid_seconds=30)
        self.cache(self.pool[0])
        before = self.read()
        self.assertTrue(before["details_changed"])
        self.assertEqual(before["actors"][self.tokens[0]]["member_id"], self.captains[0])
        with self.core.transaction() as db:
            db.execute("UPDATE live_lots SET highest_bid=999 WHERE id=?", (lot["id"],))
            rolled_back = db.execute("SELECT revision FROM _auction_versions WHERE event_id=?", (self.event,)).fetchone()[0]
            self.assertGreater(rolled_back, 0)
            db.rollback()
        with patch.object(self.live, "_view_statements", side_effect=AssertionError("unchanged state SELECT")):
            stable = self.read(before)
        self.assertEqual(stable["revision"], before["revision"])
        self.assertFalse(stable["changed"])
        self.assertIsNone(stable["state"])
        self.bid(0, 5, lot=lot)
        queries = []
        original = self.core.connect

        def traced():
            db = original()
            db.set_trace_callback(queries.append)
            return db

        with patch.object(self.core, "connect", side_effect=traced), patch("roly.riot_profile.attach_profile", side_effect=AssertionError("hot profile parsing")):
            after = self.read(before)
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(after["detail_revision"], before["detail_revision"])
        self.assertFalse(after["details_changed"])
        self.assertEqual(after["state"]["current_lot"]["highest_bid"], 5)
        self.assertEqual(after["state"]["current_lot"]["highest_team_name"], before["state"]["teams"][0]["name"])
        self.assertEqual(after["state"]["current_lot"]["riot_profile"], before["state"]["current_lot"]["riot_profile"])
        self.assertIsNone(before["state"]["current_lot"]["highest_bid"])
        self.assertFalse(any("LIMIT 300" in query or "FROM riot_profiles" in query or "JOIN riot_profiles" in query for query in queries))
        self.assertEqual(sum("FROM sessions s" in query for query in queries), 2)  # Probe plus changed snapshot.

    def test_external_worker_profile_and_administrator_writes_force_complete_details(self):
        self.start()
        previous = self.read()
        for query, values, inspect in (
            ("UPDATE competition_teams SET budget=1234 WHERE id=?", (previous["state"]["teams"][0]["id"],), lambda state: state["teams"][0]["budget"] == 1234),
            ("UPDATE competition_events SET title='external admin' WHERE id=?", (self.event,), lambda state: state["event"]["title"] == "external admin"),
            ("UPDATE accounts SET display_name='renamed host' WHERE id=(SELECT created_by FROM competition_events WHERE id=?)", (self.event,), lambda state: state["event"]["host_name"] == "renamed host"),
            ("UPDATE live_lots SET status='CANCELLED' WHERE event_id=? AND status='QUEUED'", (self.event,), lambda state: state["queued_count"] == 0),
        ):
            with self.subTest(query=query):
                with self.core.transaction() as db:
                    db.execute(query, values)
                current = self.read(previous)
                self.assertGreater(current["detail_revision"], previous["detail_revision"])
                self.assertTrue(current["details_changed"])
                self.assertTrue(inspect(current["state"]))
                previous = current
        self.cache(self.pool[0])
        current = self.read(previous)
        self.assertGreater(current["detail_revision"], previous["detail_revision"])
        self.assertEqual(current["state"]["current_lot"]["riot_profile"]["flex_lp"], 45)
        self.bid(0, 10)
        current = self.read(current)
        self.close()
        settled = self.read(current)
        self.assertGreater(settled["detail_revision"], current["detail_revision"])
        self.assertEqual(settled["state"]["teams"][0]["remaining"], 1224)

    def test_sessions_are_bulk_checked_even_without_a_revision_change(self):
        self.start()
        previous = self.read(tokens=(*self.tokens, "unknown"))
        self.assertIsNone(previous["actors"]["unknown"])
        with patch("roly.auction_state.now", return_value="9999-12-31T00:00:00+00:00"):
            expired = self.read(previous, tokens=self.tokens)
        self.assertFalse(expired["changed"])
        self.assertTrue(all(actor is None for actor in expired["actors"].values()))
        self.core.logout(self.tokens[0])
        revoked = self.read(previous)
        self.assertGreater(revoked["revision"], previous["revision"])
        self.assertEqual(revoked["detail_revision"], previous["detail_revision"])
        self.assertIsNone(revoked["actors"][self.tokens[0]])
        self.assertIsNotNone(revoked["actors"][self.tokens[1]])

    def test_pg_contract_uses_one_pipeline_and_one_identity_query_for_eighty_viewers(self):
        self.start()
        batches = []
        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path,
                                 connect=lambda: BatchedSQLite(self.core.connect(), batches))
        live = LiveAuction(facade, self.comp)
        tokens = (*self.tokens, *(f"viewer-{index}" for index in range(76)))
        previous = read_snapshot(live, self.event, tokens)
        self.assertEqual(len(batches), 1)
        self.assertEqual(sum("FROM sessions s" in query for query, _ in batches[0]), 1)
        self.assertEqual(len(previous["actors"]), 80)
        self.assertEqual(sum(actor is not None for actor in previous["actors"].values()), 4)
        self.assertIn("clock_timestamp()", batches[0][-1][0])
        batches.clear()
        unchanged = read_snapshot(live, self.event, tokens, base_state=previous["state"], base_version=previous)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 3)  # Versions, all identities, final DB clock.
        self.bid(0, 5)
        batches.clear()
        hot = read_snapshot(live, self.event, tokens, base_state=previous["state"], base_version=previous)
        self.assertFalse(hot["details_changed"])
        self.assertEqual(hot["state"]["current_lot"]["highest_bid"], 5)
        self.assertEqual(len(batches), 2)
        self.assertFalse(any("LIMIT 300" in query for batch in batches for query, _ in batch))

    def test_session_expiring_during_checkout_close_or_projection_is_rejected_on_read_completion(self):
        self.start()
        token = self.tokens[0]
        initial = "2027-01-01T00:00:00.000000+00:00"
        expires = "2027-01-01T00:00:01.000000+00:00"
        with self.core.transaction() as db:
            db.execute("UPDATE sessions SET expires_at=? WHERE account_id=?", (expires, self.account_ids[0]))
        for delay in ("none", "checkout", "close", "projection", "unchanged"):
            with self.subTest(delay=delay):
                clock = [initial]

                def finish():
                    clock[0] = expires

                def connect():
                    if delay == "checkout":
                        finish()
                    return BatchedSQLite(self.core.connect(), [], on_close=finish if delay in ("close", "unchanged") else None)

                facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path, connect=connect)
                live = LiveAuction(facade, self.comp)
                with patch("roly.auction_state.now", side_effect=lambda: clock[0]):
                    if delay == "projection":
                        assemble = live._assemble_view

                        def delayed(*args, **kwargs):
                            state = assemble(*args, **kwargs)
                            finish()
                            return state

                        with patch.object(live, "_assemble_view", side_effect=delayed):
                            snapshot = read_snapshot(live, self.event, (token,))
                    elif delay == "unchanged":
                        previous = self.read(tokens=(token,))
                        clock[0] = initial
                        snapshot = read_snapshot(live, self.event, (token,), base_state=previous["state"], base_version=previous)
                        self.assertFalse(snapshot["changed"])
                    else:
                        snapshot = read_snapshot(live, self.event, (token,))
                if delay == "none":
                    # The simulated DB clock is in 2033. Auth still follows
                    # Core's 2027 UTC cutoff rather than auction deadline time.
                    self.assertIsNotNone(snapshot["actors"][token])
                else:
                    self.assertIsNone(snapshot["actors"][token])

    def test_sqlite_lot_primary_id_change_invalidates_cached_detail_topology(self):
        self.live.configure(self.admin, self.event, order=self.pool)
        previous = self.read()
        old_id = previous["state"]["lots"][0]["id"]
        new_id = old_id + 10000
        with self.core.transaction() as db:
            db.execute("UPDATE live_lots SET id=? WHERE id=?", (new_id, old_id))
        after = self.read(previous)
        self.assertTrue(after["details_changed"])
        self.assertGreater(after["detail_revision"], previous["detail_revision"])
        self.assertIn(new_id, {lot["id"] for lot in after["state"]["lots"]})
        self.assertNotIn(old_id, {lot["id"] for lot in after["state"]["lots"]})

    def test_details_committed_between_probe_and_hot_read_cannot_reuse_stale_profiles(self):
        self.start()
        previous = self.read()
        self.bid(0, 5)
        batches = auction_state._batches
        calls = []

        def raced(*args, **kwargs):
            result = batches(*args, **kwargs)
            calls.append(result)
            if len(calls) == 1:
                self.cache(self.pool[0])
            return result

        with patch("roly.auction_state._batches", side_effect=raced):
            after = self.read(previous)
        self.assertEqual(len(calls), 3)
        self.assertTrue(after["details_changed"])
        self.assertEqual(after["state"]["current_lot"]["riot_profile"]["flex_lp"], 45)

    def test_revision_and_auth_belong_to_same_snapshot_as_display(self):
        self.start()
        previous = self.read()
        changed = [False]

        def write_after_actor():
            if changed[0]:
                return
            changed[0] = True
            with self.core.transaction() as db:
                db.execute("UPDATE competition_events SET title='new snapshot' WHERE id=?", (self.event,))
                db.execute("UPDATE accounts SET active=0 WHERE id=?", (self.account_ids[0],))

        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path,
                                 connect=lambda: BatchedSQLite(self.core.connect(), [], write_after_actor))
        live = LiveAuction(facade, self.comp)
        during = read_snapshot(live, self.event, self.tokens)
        self.assertEqual(during["revision"], previous["revision"])
        self.assertEqual(during["state"]["event"]["title"], previous["state"]["event"]["title"])
        self.assertIsNotNone(during["actors"][self.tokens[0]])
        after = read_snapshot(live, self.event, self.tokens)
        self.assertGreater(after["revision"], during["revision"])
        self.assertEqual(after["state"]["event"]["title"], "new snapshot")
        self.assertIsNone(after["actors"][self.tokens[0]])


if __name__ == "__main__":
    unittest.main()
