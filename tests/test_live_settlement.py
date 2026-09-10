"""Settlement preserves writer ownership while batching real local SQL."""
from collections import Counter
from contextlib import closing
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from roly.postgres import _batch_dml, _batch_select
from tests import test_live_auction as fixtures


class TracedConnection:
    """Run SQLite SQL; exercise PG batch guards without a driver or network."""
    def __init__(self, db, trace):
        self.db, self.trace = db, trace

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, query, parameters=None):
        self.trace.append(("execute", query))
        return self.db.execute(query, parameters or ())

    def execute_batch(self, statements):
        _batch_dml(statements)
        self.trace.append(("write_batch", len(statements)))
        return [self.execute(query, parameters) for query, parameters in statements]

    def fetch_batches(self, statements):
        for query, parameters in statements:
            _batch_select(query, parameters)
        self.trace.append(("read_batch", len(statements)))
        return [self.execute(query, parameters).fetchall() for query, parameters in statements]

    def commit(self):
        self.trace.append(("commit", "COMMIT"))
        return self.db.commit()

    def rollback(self):
        self.trace.append(("rollback", "ROLLBACK"))
        return self.db.rollback()


def measure_transition(case, kind):
    lot = case.start()
    if kind in ("sold", "next"):
        case.bid(0, 5)
    if kind == "next":
        case.close()
        case.clock.value = case.live.get_state(case.event)["next_at"]
    else:
        case.clock.value = case.live.get_state(case.event)["current_lot"]["closes_at"]
    trace, connect = [], case.core.connect
    with patch.object(case.core, "connect", side_effect=lambda: TracedConnection(connect(), trace)):
        changed = case.live.settle_due()
    state = case.live.get_state(case.event)
    queries = [query for method, query in trace if method == "execute"]
    verbs = Counter(query.lstrip().split()[0].upper() for query in queries)
    writer_queries = queries[queries.index("BEGIN IMMEDIATE") + 1:]
    return {"kind": kind, "changed": changed == [case.event], "explicit_sql": sum(verbs.get(v, 0) for v in ("SELECT", "INSERT", "UPDATE", "DELETE")),
        "sql_counts": dict(verbs), "commit_count": sum(method == "commit" for method, _ in trace),
        "writer_sql_count": len(writer_queries),
        "read_batches": [value for method, value in trace if method == "read_batch"],
        "write_batches": [value for method, value in trace if method == "write_batch"],
        "queries": queries, "status": state["status"], "lot_status": state["current_lot"]["status"],
        "price": state["lots"][0]["highest_bid"], "team_members": [len(team["players"]) for team in state["teams"]],
        "first_lot_status": state["lots"][0]["status"], "current_sequence": state["current_lot"]["sequence"],
        "event_types": [entry["type"] for entry in state["events"]]}


class LiveSettlementTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    setUp = fixtures.LiveAuctionTests.setUp
    tearDown = fixtures.LiveAuctionTests.tearDown
    start = fixtures.LiveAuctionTests.start
    bid = fixtures.LiveAuctionTests.bid
    close = fixtures.LiveAuctionTests.close

    def test_successful_sale_keeps_one_outer_commit(self):
        result = measure_transition(self, "sold")
        self.assertEqual(result["commit_count"], 1)
        self.assertEqual(result["first_lot_status"], "SOLD")
        self.assertEqual(result["team_members"], [2, 1, 1, 1])
        self.assertEqual(result["event_types"].count("SOLD"), 1)
        self.assertEqual(result["read_batches"], [3])
        self.assertEqual(result["write_batches"], [4, 2])
        self.assertFalse(any("score_ledger" in sql or "award_ledger" in sql for sql in result["queries"]))

    def test_unsold_keeps_one_outer_commit_and_no_assignment(self):
        result = measure_transition(self, "unsold")
        self.assertEqual(result["commit_count"], 1)
        self.assertEqual(result["first_lot_status"], "UNSOLD")
        self.assertEqual(result["team_members"], [1, 1, 1, 1])

    def test_next_player_keeps_one_outer_commit_and_opens_only_next_lot(self):
        result = measure_transition(self, "next")
        self.assertEqual(result["commit_count"], 1)
        self.assertEqual(result["first_lot_status"], "SOLD")
        self.assertEqual(result["lot_status"], "OPEN")
        self.assertEqual(result["current_sequence"], 1)
        self.assertEqual(result["write_batches"], [4])

    def test_late_batch_error_rolls_back_every_candidate_then_retry_sells_once(self):
        first = self.start()
        self.bid(0, 5)
        second_event = self.comp.create_auction(self.admin, self.ids, self.captains)
        with self.core.transaction() as db:
            db.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (second_event,))
        self.live.configure(self.admin, second_event, order=self.pool)
        second = self.live.start(self.admin, second_event)["current_lot"]
        self.live.place_bid(self.tokens[1], second_event, second["id"], 10, "00000000-0000-4000-8000-000000000123")
        self.clock.value = max(self.live.get_state(event)["current_lot"]["closes_at"] for event in (self.event, second_event))
        trace, connect = [], self.core.connect

        class Failing(TracedConnection):
            def execute(inner, query, parameters=None):
                if "INSERT INTO live_events" in query and parameters[0] == second_event:
                    raise sqlite3.OperationalError("Synthetic second-candidate log failure")
                return super().execute(query, parameters)

        with patch.object(self.core, "connect", side_effect=lambda: Failing(connect(), trace)):
            with self.assertRaisesRegex(sqlite3.OperationalError, "second-candidate"):
                self.live.settle_due()
        self.assertEqual(sum(method == "commit" for method, _ in trace), 0)
        self.assertEqual(sum(method == "rollback" for method, _ in trace), 1)
        for event in (self.event, second_event):
            state = self.live.get_state(event)
            self.assertEqual(state["current_lot"]["status"], "OPEN")
            self.assertEqual([len(team["players"]) for team in state["teams"]], [1, 1, 1, 1])
            self.assertFalse(any(entry["type"] == "SOLD" for entry in state["events"]))
        self.assertEqual(self.live.settle_due(), [self.event, second_event])
        self.assertEqual(self.live.settle_due(), [])
        for event in (self.event, second_event):
            self.assertEqual(sum(entry["type"] == "SOLD" for entry in self.live.get_state(event)["events"]), 1)

    def test_batched_settlement_rechecks_approval_roster_budget_and_capacity(self):
        lot = self.start()
        self.bid(0, 5)
        team = self.live.get_state(self.event)["teams"][0]
        changes = [
            ("UPDATE members SET status='KICKED' WHERE id=?", (lot["member_id"],)),
            ("UPDATE members SET status='KICKED' WHERE id=?", (self.captains[0],)),
            ("UPDATE competition_players SET participation_status='WITHDRAWN' WHERE event_id=? AND member_id=?", (self.event, lot["member_id"])),
            ("UPDATE competition_teams SET budget=0 WHERE id=?", (team["id"],)),
            ("UPDATE competition_players SET team_id=? WHERE event_id=? AND member_id IN (?,?,?,?)", (team["id"], self.event, *self.pool[1:5])),
        ]
        for query, parameters in changes:
            with self.subTest(query=query, parameters=parameters), self.core.transaction() as raw:
                raw.execute(query, parameters)
                db = TracedConnection(raw, [])
                session = self.live._session(db, self.event)
                self.live._close_lot(db, session, self.clock.value)
                after = raw.execute("SELECT status FROM live_lots WHERE id=?", (lot["id"],)).fetchone()[0]
                self.assertEqual(after, "UNSOLD")
                self.assertIsNone(raw.execute("SELECT team_id FROM competition_players WHERE event_id=? AND member_id=?", (self.event, lot["member_id"])).fetchone()[0])
                raw.rollback()  # Every subcase starts from the same accepted bid.

    def test_worker_scan_combines_active_check_and_ages_its_nearest_deadline(self):
        lot = self.start()
        self.clock.value = lot["closes_at"] - .05
        trace, connect = [], self.core.connect
        with patch.object(self.core, "connect", side_effect=lambda: TracedConnection(connect(), trace)), \
                patch("roly.live_auction.time.monotonic", return_value=1000.0):
            self.assertEqual(self.live._due_candidates(), [])
            self.assertTrue(self.live._worker_scan.result["active"])
            self.assertAlmostEqual(self.live._worker_delay(.25), .05, places=5)
        self.assertEqual(sum(method == "execute" and value.startswith("SELECT") for method, value in trace), 1)
        self.assertFalse(any(value == "BEGIN IMMEDIATE" for method, value in trace))
        with patch("roly.live_auction.time.monotonic", return_value=1000.04):
            self.assertAlmostEqual(self.live._worker_delay(.25), .01, places=5)
        with patch("roly.live_auction.time.monotonic", return_value=1000.06):
            self.assertEqual(self.live._worker_delay(.25), 0)

    def test_worker_coalesces_a_burst_received_during_scan_without_losing_the_wake(self):
        self.start()
        entered, release, followed = threading.Event(), threading.Event(), threading.Event()
        original, scans = self.live._due_candidates, []
        def delayed():
            result = original()
            scans.append(result)
            if len(scans) == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Synthetic worker scan gate timed out")
            else:
                followed.set()
            return result
        with patch.object(self.live, "_due_candidates", side_effect=delayed), \
                patch.object(self.live, "has_active_sessions", side_effect=AssertionError("duplicate active query")):
            self.live.ensure_worker(interval=1)
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(100):
                    self.live.wake_worker()
                release.set()
                self.assertTrue(followed.wait(.5))  # Cannot be the 1s periodic recovery.
            finally:
                release.set()
                self.live.stop_worker()
        self.assertEqual(len(scans), 2)

    def test_worker_reaches_a_near_deadline_without_waiting_the_full_scan_interval(self):
        lot = self.start()
        origin, stamp = time.monotonic(), self.clock.value
        self.live._clock = lambda: stamp + time.monotonic() - origin
        with self.core.transaction() as db:
            db.execute("UPDATE live_lots SET closes_at=? WHERE id=?", (stamp + .1, lot["id"]))
        settled, original = threading.Event(), self.live.settle_due
        def observed():
            result = original()
            if result:
                settled.set()
            return result
        with patch.object(self.live, "settle_due", side_effect=observed):
            self.live.ensure_worker(interval=1)
            try:
                self.assertTrue(settled.wait(.7))
            finally:
                self.live.stop_worker()
        state = self.live.get_state(self.event)
        self.assertEqual(state["current_lot"]["status"], "UNSOLD")
        self.assertGreaterEqual(state["current_lot"]["closed_at"], stamp + .1)

    def test_notify_during_idle_scan_prevents_worker_retirement_after_new_session(self):
        entered, release, followed = threading.Event(), threading.Event(), threading.Event()
        original = self.live._due_candidates
        def delayed():
            result = original()
            if not entered.is_set():
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Synthetic idle scan gate timed out")
            else:
                followed.set()
            return result
        with patch.object(self.live, "_due_candidates", side_effect=delayed):
            worker = self.live.ensure_worker(interval=1)
            try:
                self.assertTrue(entered.wait(2))
                self.live.configure(self.admin, self.event)
                self.live.wake_worker()  # No ensure_worker call to rescue retirement.
                release.set()
                self.assertTrue(followed.wait(.5))
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                self.live.stop_worker()


if __name__ == "__main__":
    unittest.main()
