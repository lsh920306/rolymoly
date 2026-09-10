"""Shared UI/push display caching, authenticated snapshots and v8/v9 safety."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from roly.auction_shared import SharedSnapshots
from roly import auction_state
from roly.auction_state import initialize_changes, read_snapshot, shared_view
from roly.live_auction import LiveAuction
from tests import test_live_auction as fixtures
from tests import test_live_view_reads as view_fixtures
from tests.test_auction_state import BatchedSQLite


class SharedAuctionTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    setUp = fixtures.LiveAuctionTests.setUp
    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start
    bid = fixtures.LiveAuctionTests.bid
    close = fixtures.LiveAuctionTests.close
    cache = view_fixtures.LiveViewReadTests.cache

    def test_sixty_distinct_sessions_share_one_initial_display_and_keep_current_auth(self):
        self.start(bid_seconds=30)
        tokens = []
        with self.core.transaction() as db:
            for index in range(60):
                token = f"synthetic-shared-{self.event}-{index}"
                db.execute("INSERT INTO sessions(token_hash,account_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (sha256(token.encode()).hexdigest(), self.account_ids[index % 4], "2999-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
                tokens.append(token)
        cache = SharedSnapshots()
        gate = threading.Barrier(12)
        def read(index):
            if index < 12:
                gate.wait(3)
            return cache.snapshot(self.live, self.event, (tokens[index],))
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(read, range(60)))
        self.assertEqual(cache.stats()["reads"], 60)
        self.assertEqual(cache.stats()["full_states"], 1)
        self.assertEqual(cache.stats()["unchanged"], 59)
        self.assertTrue(all(result["actors"][token] is not None for result, token in zip(results, tokens)))
        self.assertTrue(all(result["state"]["current_lot"]["id"] == results[0]["state"]["current_lot"]["id"] for result in results))
        self.core.logout(tokens[0])
        rejected = cache.snapshot(self.live, self.event, (tokens[0], tokens[1]))
        self.assertIsNone(rejected["actors"][tokens[0]])
        self.assertIsNotNone(rejected["actors"][tokens[1]])
        self.assertNotIn(tokens[0], repr(cache._entries))
        self.assertTrue(all(not hasattr(entry, "actors") for entry in cache._entries.values()))

    def test_ui_and_hub_share_display_without_aliasing_caller_mutations(self):
        self.start()
        self.cache(self.pool[0])
        cache = SharedSnapshots()
        with patch("roly.auction_shared._shared", cache):
            actor, initial = shared_view(self.live, self.tokens[0], self.event)
            initial["current_lot"]["riot_profile"]["champions"].clear()
            initial["teams"][0]["budget"] = -99
            actor["member_id"] = -99
            first = cache.snapshot(self.live, self.event, (self.tokens[1],))
            self.assertNotEqual(first["state"]["teams"][0]["budget"], -99)
            self.assertTrue(first["state"]["current_lot"]["riot_profile"]["champions"])
            unchanged = cache.snapshot(self.live, self.event, (self.tokens[0],), base_state=first["state"], base_version=first)
            self.assertFalse(unchanged["changed"])
            self.assertIsNone(unchanged["state"])
            self.assertEqual(unchanged["actors"][self.tokens[0]]["member_id"], self.captains[0])
            self.assertEqual(cache.stats()["full_states"], 1)

    def test_allocation_and_next_lot_reuse_profiles_but_match_full_database_state(self):
        self.start(bid_seconds=30)
        self.cache(self.pool[0])
        self.cache(self.pool[1])
        batches = []
        facade = SimpleNamespace(is_postgres=True, db_path=self.core.db_path,
                                 connect=lambda: BatchedSQLite(self.core.connect(), batches))
        live = LiveAuction(facade, self.comp, clock=self.clock)
        with patch("roly.auction_state.time.monotonic", return_value=1000):
            previous = read_snapshot(live, self.event, self.tokens)
            starting_budget = previous["state"]["teams"][0]["budget"]
            self.bid(0, 10)
            previous = read_snapshot(live, self.event, self.tokens, base_state=previous["state"], base_version=previous)
            self.close()
            for phase in ("settled", "next_lot"):
                if phase == "next_lot":
                    self.clock.value += 3
                    self.live.settle_due()
                batches.clear()
                with patch("roly.riot_profile.attach_profile", side_effect=AssertionError("allocation must not parse profiles")):
                    current = read_snapshot(live, self.event, self.tokens, base_state=previous["state"], base_version=previous, changed_hint=True)
                self.assertTrue(current["details_changed"])
                self.assertFalse(current["display_changed"])
                self.assertEqual(current["display_revision"], previous["display_revision"])
                self.assertFalse(any("riot_profiles" in query for batch in batches for query, _ in batch))
                full = read_snapshot(live, self.event, self.tokens)
                self.assertEqual(current["state"], full["state"])
                self.assertEqual(current["state"]["teams"][0]["remaining"], starting_budget - 10)
                previous = current

    def test_snapshot_display_fields_and_profile_changes_force_full_refresh(self):
        self.start()
        previous = read_snapshot(self.live, self.event, self.tokens)
        with self.core.transaction() as db:
            db.execute("UPDATE competition_players SET score=score+1,clan_tier_snapshot='DIAMOND' WHERE event_id=? AND member_id=?", (self.event, self.pool[0]))
        changed = read_snapshot(self.live, self.event, self.tokens, base_state=previous["state"], base_version=previous)
        self.assertTrue(changed["display_changed"])
        self.assertGreater(changed["display_revision"], previous["display_revision"])
        self.assertEqual(changed["state"]["current_lot"]["clan_tier_snapshot"], "DIAMOND")
        self.cache(self.pool[0])
        updated = read_snapshot(self.live, self.event, self.tokens, base_state=changed["state"], base_version=changed)
        self.assertTrue(updated["display_changed"])
        self.assertEqual(updated["state"]["current_lot"]["riot_profile"]["flex_lp"], 45)
        self.assertEqual(updated["state"]["current_lot"]["score"], changed["state"]["current_lot"]["score"])
        with self.core.transaction() as db:
            db.execute("DELETE FROM riot_profiles WHERE member_id=?", (self.pool[0],))
        deleted = read_snapshot(self.live, self.event, self.tokens, base_state=updated["state"], base_version=updated)
        self.assertTrue(deleted["display_changed"])
        self.assertIsNone(deleted["state"]["current_lot"]["riot_profile"])

    def test_unrelated_member_profile_keeps_event_display_revision_and_is_not_downloaded(self):
        self.start()
        other = self.core.join_member("UnrelatedShared#KR1", "TOP", "JG")
        previous = read_snapshot(self.live, self.event, self.tokens)
        self.cache(other)
        queries = []
        original = self.core.connect
        def traced():
            db = original()
            db.set_trace_callback(queries.append)
            return db
        with patch.object(self.core, "connect", side_effect=traced), \
             patch("roly.riot_profile.attach_profile", side_effect=AssertionError("unrelated profile parse")):
            current = read_snapshot(self.live, self.event, self.tokens, base_state=previous["state"], base_version=previous)
        self.assertEqual(current["display_revision"], previous["display_revision"])
        self.assertGreater(current["revision"], previous["revision"])
        self.assertFalse(current["display_changed"])
        self.assertFalse(any("riot_profiles" in query for query in queries))
        self.assertNotIn(other, {p["member_id"] for p in current["state"]["event"]["players"]})

    def test_profile_and_revocation_racing_allocation_falls_back_to_matching_full_snapshot(self):
        self.start()
        previous = read_snapshot(self.live, self.event, self.tokens)
        with self.core.transaction() as db:
            db.execute("UPDATE competition_teams SET budget=1200 WHERE event_id=?", (self.event,))
        original = auction_state._batches
        stages = []
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            stages.append(result)
            if len(stages) == 1:
                self.cache(self.pool[0])
                self.core.logout(self.tokens[0])
            return result
        with patch.object(auction_state, "_batches", side_effect=read):
            current = read_snapshot(self.live, self.event, self.tokens, base_state=previous["state"], base_version=previous)
        self.assertEqual(len(stages), 3)
        self.assertTrue(current["display_changed"])
        self.assertIsNone(current["actors"][self.tokens[0]])
        self.assertEqual(current["state"]["current_lot"]["riot_profile"]["flex_lp"], 45)
        self.assertEqual(current["state"]["teams"][0]["budget"], 1200)

    def test_cache_eviction_is_bounded_and_a_new_owner_cannot_reuse_another_owner_state(self):
        self.start()
        cache = SharedSnapshots(max_entries=2)
        for event_id in (self.event, self.event + 100, self.event + 101):
            cache.snapshot(self.live, event_id, self.tokens)
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertEqual(cache.stats()["evictions"], 1)
        other = LiveAuction(self.core, self.comp, clock=self.clock)
        cache.snapshot(other, self.event, self.tokens)
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertEqual(cache.stats()["full_states"], 4)

    def test_late_auth_expiry_after_cached_copy_is_rejected(self):
        self.start()
        token = self.tokens[0]
        cache = SharedSnapshots()
        cache.snapshot(self.live, self.event, (token,))
        with patch("roly.auction_state.now", return_value="9999-01-01T00:00:00+00:00"):
            result = cache.snapshot(self.live, self.event, (token,))
        self.assertIsNone(result["actors"][token])

    def make_legacy_versions(self):
        with self.core.transaction() as db:
            rows = db.execute("SELECT event_id,revision,detail_revision FROM _auction_versions").fetchall()
            triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'auction_change_%'").fetchall()
            for row in triggers:
                db.execute('DROP TRIGGER "' + row[0] + '"')
            db.execute("DROP TABLE _auction_versions")
            db.execute("CREATE TABLE _auction_versions(event_id BIGINT PRIMARY KEY,revision BIGINT NOT NULL,detail_revision BIGINT NOT NULL)")
            db.executemany("INSERT INTO _auction_versions VALUES(?,?,?)", rows)
        return [tuple(row) for row in rows]

    def test_v8_read_falls_back_full_then_atomic_migration_preserves_versions_and_is_idempotent(self):
        self.start()
        versions = self.make_legacy_versions()
        previous = read_snapshot(self.live, self.event, self.tokens)
        self.assertIsNone(previous["display_revision"])
        with patch.object(self.live, "_assemble_view", wraps=self.live._assemble_view) as assemble:
            current = read_snapshot(self.live, self.event, self.tokens, base_state=previous["state"], base_version=previous)
        self.assertEqual(assemble.call_count, 1)
        self.assertTrue(current["display_changed"])
        with self.core.transaction() as db:
            initialize_changes(db)
            db.rollback()
        with closing(self.core.connect()) as db:
            self.assertNotIn("display_revision", {row[1] for row in db.execute("PRAGMA table_info(_auction_versions)")})
        with self.core.transaction() as db:
            initialize_changes(db)
            initialize_changes(db)
            self.assertEqual([tuple(row) for row in db.execute("SELECT event_id,revision,detail_revision FROM _auction_versions")], versions)
            self.assertTrue(all(row[0] == 0 for row in db.execute("SELECT display_revision FROM _auction_versions")))
        with self.core.transaction() as db:
            db.execute("UPDATE competition_players SET price=price+5 WHERE event_id=? AND member_id=?", (self.event, self.pool[0]))
        allocation = read_snapshot(self.live, self.event, self.tokens)
        self.assertEqual(allocation["display_revision"], 0)
        with self.core.transaction() as db:
            db.execute("UPDATE competition_players SET score=score+1 WHERE event_id=? AND member_id=?", (self.event, self.pool[0]))
        display = read_snapshot(self.live, self.event, self.tokens)
        self.assertEqual(display["display_revision"], 1)

    def test_pg_migration_uses_same_display_columns_and_no_security_definer(self):
        calls = []
        db = SimpleNamespace(in_transaction=True, execute=lambda query, *args: calls.append(query))
        initialize_changes(db, postgres=True)
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS" in query and "display_revision" in query for query in calls))
        body = next(query for query in calls if "CREATE OR REPLACE FUNCTION" in query)
        self.assertNotIn("SECURITY DEFINER", body)
        for field in auction_state._PLAYER_DISPLAY:
            self.assertIn(f"(current_row->'{field}') IS DISTINCT FROM (previous_row->'{field}')", body)
        self.assertIn("pg_catalog.pg_notify", body)


class SharedCapacityTests(unittest.TestCase):
    def test_active_entry_is_pinned_and_capacity_fallback_does_not_grow_map(self):
        entered, release = threading.Event(), threading.Event()
        owner = object()
        def read(live, event, tokens, **kwargs):
            if event == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test reader gate timed out")
            return {"revision": 1, "detail_revision": 1, "display_revision": 1,
                    "changed": True, "details_changed": True, "display_changed": True,
                    "state": None, "actors": {}, "server_now": 1, "sampled_at": time.monotonic()}
        cache = SharedSnapshots(max_entries=1, reader=read)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.snapshot, owner, 1)
            self.assertTrue(entered.wait(2))
            try:
                cache.snapshot(owner, 2)
                self.assertEqual(cache.stats()["entries"], 1)
                self.assertEqual(cache.stats()["capacity_fallbacks"], 1)
                self.assertIn((owner, 1), cache._entries)
            finally:
                release.set()
            first.result(3)

    def test_revision_regression_is_not_served_as_old_price(self):
        def read(live, event, tokens, **kwargs):
            old = kwargs["base_version"]
            revision = 1 if old else 2
            return {"revision": revision, "detail_revision": revision, "display_revision": revision,
                    "changed": True, "details_changed": True, "display_changed": True,
                    "state": None, "actors": {}, "server_now": 1, "sampled_at": time.monotonic()}
        owner, cache = object(), SharedSnapshots(reader=read)
        cache.snapshot(owner, 1)
        with self.assertRaisesRegex(ValueError, "moved backwards"):
            cache.snapshot(owner, 1)


if __name__ == "__main__":
    unittest.main()
