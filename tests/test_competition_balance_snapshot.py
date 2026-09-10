"""Offline snapshot races, writer availability and atomic roster batch checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly import competition, tournament
from roly.core import Core


class BatchConnection:
    """SQLite execution plus the production PG DML-batch syntax contract."""
    def __init__(self, raw, batches):
        self.raw, self.batches = raw, batches

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def execute_batch(self, statements):
        from roly.postgres import _batch_dml
        statements = list(statements)
        _batch_dml(statements)
        if not self.raw.in_transaction:
            raise AssertionError("batch must use the caller's transaction")
        self.batches.append(len(statements))
        return [self.raw.execute(query, params) for query, params in statements]


class BalanceSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline tests only"))
        cls.network.start()
        cls.base = Core(":memory:")
        cls.base.setup_admin("admin", "balance-test-password")
        cls.token = cls.base.login("admin", "balance-test-password")
        competition.Competition(cls.base)
        cls.ids = []
        for index in range(25):
            mid = cls.base.join_member(f"Balance{index}#QA", competition.ROLES[index % 5], competition.ROLES[(index + 1) % 5])
            cls.base.approve_member(cls.token, mid, 100)
            cls.ids.append(mid)

    @classmethod
    def tearDownClass(cls):
        cls.base._keeper.close()
        cls.network.stop()

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.core = Core(Path(self.directory.name) / "balance.sqlite3")
        with closing(self.core.connect()) as db:
            self.base._keeper.backup(db)
        self.comp = competition.Competition(self.core)
        self.prep = tournament.TournamentService(self.core, self.comp)

    def tearDown(self):
        self.directory.cleanup()

    def assignments(self, count=10):
        return [{"member_id": mid, "role": competition.ROLES[index % 5]} for index, mid in enumerate(self.ids[:count])]

    def preparing(self, count=10):
        eid = self.prep.create(self.token, "Balance QA", "2030-01-01T10:00:00Z", team_count=count // 5,
                               format_name="SINGLE" if count == 10 else "LEAGUE")
        self.prep.open_recruitment(self.token, eid)
        self.prep.set_participants(self.token, eid, self.assignments(count))
        self.prep.confirm_participants(self.token, eid)
        self.prep.set_captains(self.token, eid, self.ids[:count:5])
        return eid

    def execute(self, sql, params=()):
        with self.core.transaction() as db:
            db.execute(sql, params)

    def scalar(self, sql, params=()):
        with self.core.read_snapshot() as db:
            return db.execute(sql, params).fetchone()[0]

    def test_bulk_snapshot_matches_single_projection_and_submitted_order(self):
        self.core.adjust_score(self.token, self.ids[2], 37, "snapshot score")
        submitted = [self.assignments()[2], self.assignments()[0], self.assignments()[2]]
        with self.core.read_snapshot() as db:
            expected = [self.comp._player_snapshot(db, item["member_id"], item["role"]) for item in submitted]
            self.assertEqual(self.comp._player_snapshots(db, submitted), expected)

    def test_create_rejects_score_change_during_compute_without_partial_event(self):
        original = competition.balance_teams
        def calculate(players):
            self.core.adjust_score(self.token, self.ids[0], 7, "concurrent score")
            return original(players)
        with patch.object(competition, "balance_teams", side_effect=calculate) as called:
            with self.assertRaisesRegex(ValueError, "계산 중"):
                self.comp.create_normal(self.token, self.assignments(), request_key=str(uuid4()))
        self.assertEqual(called.call_count, 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_events"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_creation_requests"), 0)

    def test_create_rechecks_approval_and_session_after_compute(self):
        original = competition.balance_teams
        for change, error in (("member", ValueError), ("session", PermissionError)):
            with self.subTest(change=change):
                def calculate(players):
                    if change == "member":
                        self.execute("UPDATE members SET status='KICKED' WHERE id=?", (self.ids[0],))
                    else:
                        self.core.logout(self.token)
                    return original(players)
                with patch.object(competition, "balance_teams", side_effect=calculate), self.assertRaises(error):
                    self.comp.create_normal(self.token, self.assignments())
                self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_events"), 0)
                self.execute("UPDATE members SET status='APPROVED' WHERE id=?", (self.ids[0],))

    def test_duplicate_uuid_concurrent_creation_and_replay_after_score_change(self):
        barrier = Barrier(2)
        original = competition.balance_teams
        key = str(uuid4())
        def calculate(players):
            barrier.wait(timeout=5)
            return original(players)
        with patch.object(competition, "balance_teams", side_effect=calculate), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.comp.create_normal, self.token, self.assignments(), request_key=key) for _ in range(2)]
            results = [job.result(timeout=10) for job in jobs]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_creation_requests"), 1)
        self.core.adjust_score(self.token, self.ids[0], 11, "after committed request")
        with patch.object(competition, "balance_teams", side_effect=AssertionError("receipt must bypass recompute")):
            self.assertEqual(self.comp.create_normal(self.token, self.assignments(), request_key=key), results[0])
        with self.assertRaisesRegex(ValueError, "같은 생성 요청"):
            self.comp.create_normal(self.token, self.assignments(), title="different", request_key=key)

    def test_all_three_solvers_release_read_lease_and_allow_another_writer(self):
        for kind in ("create", "replace", "preparation"):
            with self.subTest(kind=kind):
                if kind == "create":
                    operation = lambda: self.comp.create_normal(self.token, self.assignments())
                elif kind == "replace":
                    eid = self.comp.create_normal(self.token, self.assignments(), balanced=False)
                    version = self.comp.get_event(eid)["roster_token"]
                    operation = lambda: self.comp.replace_normal_roster(self.token, eid, self.assignments(), "rebuild", version, balanced=True)
                else:
                    eid = self.preparing()
                    operation = lambda: self.prep.auto_balance(self.token, eid)
                owner, name = (tournament, "_balance_captains") if kind == "preparation" else (competition, "balance_teams")
                original = getattr(owner, name)
                entered, release = Event(), Event()
                leases = set()
                connect = self.core.connect
                class Lease:
                    def __init__(self, raw):
                        self.raw = raw
                        leases.add(id(self))
                    def __getattr__(self, field):
                        return getattr(self.raw, field)
                    def close(self):
                        leases.discard(id(self))
                        self.raw.close()
                def calculate(*args):
                    self.assertFalse(leases, "read connection must close before calculation")
                    entered.set()
                    self.assertTrue(release.wait(timeout=5))
                    return original(*args)
                with patch.object(self.core, "connect", side_effect=lambda: Lease(connect())), patch.object(owner, name, side_effect=calculate), ThreadPoolExecutor(max_workers=2) as pool:
                    job = pool.submit(operation)
                    try:
                        self.assertTrue(entered.wait(timeout=3))
                        writer = pool.submit(self.execute, "UPDATE members SET notes=? WHERE id=?", (kind, self.ids[-1]))
                        writer.result(timeout=1)
                        self.assertFalse(job.done(), "solver should still be paused")
                    finally:
                        release.set()
                    job.result(timeout=5)

    def test_replacement_detects_roster_change_and_keeps_competing_write(self):
        eid = self.comp.create_normal(self.token, self.assignments(), balanced=False)
        version = self.comp.get_event(eid)["roster_token"]
        original = competition.balance_teams
        def calculate(players):
            self.execute("UPDATE competition_players SET score=score+1 WHERE event_id=? AND member_id=?", (eid, self.ids[0]))
            return original(players)
        with patch.object(competition, "balance_teams", side_effect=calculate), self.assertRaisesRegex(ValueError, "다른 화면"):
            self.comp.replace_normal_roster(self.token, eid, self.assignments(), "race", version, balanced=True)
        self.assertEqual(self.scalar("SELECT score FROM competition_players WHERE event_id=? AND member_id=?", (eid, self.ids[0])), 101)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_audit WHERE action='NORMAL_ROSTER_REPLACE'"), 0)

    def test_preparation_detects_score_role_assignment_captain_and_aba_audit_changes(self):
        edits = ["UPDATE competition_players SET score=score+1 WHERE event_id=?",
                 "UPDATE competition_players SET role='TOP' WHERE event_id=?",
                 "UPDATE competition_players SET team_id=NULL WHERE event_id=?",
                 "UPDATE competition_teams SET budget=budget+1 WHERE event_id=?",
                 "UPDATE competition_teams SET captain_id=NULL WHERE event_id=?",
                 "INSERT INTO competition_audit(event_id,actor_id,action,detail,created_at) VALUES(?,1,'ABA','restored','2030-01-01')"]
        original = tournament._balance_captains
        for sql in edits:
            with self.subTest(sql=sql):
                eid = self.preparing()
                def calculate(*args):
                    self.execute(sql, (eid,))
                    return original(*args)
                with patch.object(tournament, "_balance_captains", side_effect=calculate), self.assertRaisesRegex(ValueError, "계산 중"):
                    self.prep.auto_balance(self.token, eid)
                self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_audit WHERE event_id=? AND action='AUTO_BALANCE'", (eid,)), 0)

    def test_preparation_preserves_confirmed_scores_despite_later_live_score_change(self):
        eid = self.preparing()
        original = tournament._balance_captains
        def calculate(*args):
            self.core.adjust_score(self.token, self.ids[0], 17, "after confirmation")
            return original(*args)
        with patch.object(tournament, "_balance_captains", side_effect=calculate):
            self.prep.auto_balance(self.token, eid)
        self.assertEqual(self.scalar("SELECT score FROM competition_players WHERE event_id=? AND member_id=?", (eid, self.ids[0])), 100)
        self.assertEqual(self.core.get_member(self.ids[0])["score"], 117)

    def test_preparation_rechecks_member_approval_after_compute(self):
        eid = self.preparing()
        original = tournament._balance_captains
        def calculate(*args):
            self.execute("UPDATE members SET status='KICKED' WHERE id=?", (self.ids[1],))
            return original(*args)
        with patch.object(tournament, "_balance_captains", side_effect=calculate), self.assertRaisesRegex(ValueError, "승인된 회원"):
            self.prep.auto_balance(self.token, eid)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_players WHERE event_id=? AND team_id IS NOT NULL", (eid,)), 2)

    def test_pg_batch_contract_partial_assignment_failure_rolls_back_all(self):
        eid = self.preparing(20)
        before = self.comp.get_event(eid)
        with closing(self.core.connect()) as db:
            db.execute(f"CREATE TRIGGER fail_assignment BEFORE UPDATE OF team_id ON competition_players WHEN NEW.member_id={self.ids[6]} AND NEW.team_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'deliberate batch failure'); END")
        connect, batches = self.core.connect, []
        with patch.object(self.core, "connect", side_effect=lambda: BatchConnection(connect(), batches)), self.assertRaises(sqlite3.IntegrityError):
            self.prep.auto_balance(self.token, eid)
        self.assertEqual(self.comp.get_event(eid), before)
        self.assertEqual(batches, [22])

    def test_normal_batch_failure_has_no_event_or_receipt(self):
        with closing(self.core.connect()) as db:
            db.execute(f"CREATE TRIGGER fail_insert BEFORE INSERT ON competition_players WHEN NEW.member_id={self.ids[6]} BEGIN SELECT RAISE(ABORT,'deliberate insert failure'); END")
        connect, batches = self.core.connect, []
        with patch.object(self.core, "connect", side_effect=lambda: BatchConnection(connect(), batches)), self.assertRaises(sqlite3.IntegrityError):
            self.comp.create_normal(self.token, self.assignments(20), request_key=str(uuid4()))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_events"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM competition_creation_requests"), 0)
        self.assertEqual(batches, [20])

    def test_equal_score_ties_preserve_first_team_order_and_captain_positions(self):
        normal = self.comp.create_normal(self.token, self.assignments(20))
        prep = self.preparing(20)
        self.prep.auto_balance(self.token, prep)
        for eid in (normal, prep):
            state = self.comp.get_event(eid)
            teams = sorted(state["teams"], key=lambda team: team["id"])
            self.assertEqual([sorted(player["member_id"] for player in team["players"]) for team in teams],
                             [self.ids[index:index + 5] for index in range(0, 20, 5)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
