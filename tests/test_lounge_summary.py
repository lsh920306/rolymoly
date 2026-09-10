"""Bounded home reads retain month boundaries, corrected labels and current state."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roly.core import Core
from roly.competition import Competition
from roly.lounge import Lounge


class LoungeSummaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='roly-home-summary-')
        self.addCleanup(folder.cleanup)
        self.core = Core(Path(folder.name) / 'test.sqlite3')
        self.core.setup_admin('summary-admin', 'local-summary-password')
        self.competition = Competition(self.core)
        self.lounge = Lounge(self.core)
        self.clock = datetime(2026, 9, 10, tzinfo=timezone.utc)

    def game(self, identifier, stamp, status='CONFIRMED'):
        with self.core.transaction() as db:
            db.execute("""INSERT INTO games(id,request_key,fingerprint,kind,played_at,
                created_at,policy_id,winner,status,actor_id) VALUES(?,?,?,'NORMAL',?,?,1,'A',?,1)""",
                (identifier, str(identifier), str(identifier), stamp, stamp, status))

    def event(self, identifier, status='PREPARING'):
        with self.core.transaction() as db:
            db.execute("""INSERT INTO competition_events(id,title,kind,format,status,created_by,
                created_at,policy_snapshot) VALUES(?,?,'NORMAL','KNOCKOUT',?,1,'2026-09-01','{}')""",
                (identifier, f'Event {identifier}', status))

    def test_korean_month_bounds_and_void_games(self):
        for i, stamp in enumerate(('2026-08-31T14:59:59.999999+00:00',
                                   '2026-08-31T15:00:00.000000+00:00',
                                   '2026-09-30T14:59:59.999999+00:00',
                                   '2026-09-30T15:00:00.000000+00:00'), 1):
            self.game(i, stamp)
        self.game(5, '2026-09-05T00:00:00.000000+00:00', 'VOID')
        summary = self.lounge.home_summary(self.clock)
        self.assertEqual(summary['month_game_count'], 2)
        self.assertEqual([g['id'] for g in summary['recent_games']], [4, 3, 2, 1])
        with self.assertRaises(ValueError):
            self.lounge.home_summary(datetime(2026, 9, 1))

    def test_year_transition_and_empty_history(self):
        self.assertEqual(self.lounge.home_summary(self.clock)['recent_games'], [])
        self.game(1, '2026-11-30T15:00:00.000000+00:00')
        self.game(2, '2026-12-31T14:59:59.999999+00:00')
        self.game(3, '2026-12-31T15:00:00.000000+00:00')
        summary = self.lounge.home_summary(datetime(2026, 12, 31, tzinfo=timezone.utc))
        self.assertEqual(summary['month_game_count'], 2)

    def test_only_five_recent_rows_and_current_event_status(self):
        for i in range(1, 31):
            self.game(i, '2026-09-05T00:00:00.000000+00:00')
        self.event(1)
        self.event(2, 'COMPLETED')
        self.event(3, 'CANCELLED')
        summary = self.lounge.home_summary(self.clock)
        self.assertEqual([g['id'] for g in summary['recent_games']], [30, 29, 28, 27, 26])
        self.assertEqual(summary['month_game_count'], 30)
        self.assertEqual([e['id'] for e in summary['active_events']], [1])
        with self.core.transaction() as db:
            db.execute("UPDATE competition_events SET status='COMPLETED' WHERE id=1")
            db.execute("UPDATE games SET status='VOID' WHERE id=30")
        fresh = self.lounge.home_summary(self.clock)
        self.assertEqual(fresh['active_events'], [])
        self.assertEqual(fresh['recent_games'][0]['id'], 29)
        self.assertEqual(fresh['month_game_count'], 29)

    def test_archive_selection_and_live_labels_match_history(self):
        self.event(1)
        for i in range(1, 8):
            self.game(i, f'2026-09-0{i}T00:00:00.000000+00:00')
        with self.core.transaction() as db:
            # Only the columns used by both existing history and summary reads.
            db.execute('CREATE TABLE competition_game_archives(id INTEGER PRIMARY KEY,event_id INTEGER,core_game_id INTEGER,snapshot TEXT)')
            for i in range(1, 8):
                db.execute('INSERT INTO competition_game_archives VALUES(?,?,?,?)',
                           (i, 1, i, json.dumps({'team_a_name': f'Old A{i}', 'team_b_name': 'Old B', 'winner_name': f'Old A{i}'})))
            db.execute("INSERT INTO competition_teams(id,event_id,name) VALUES(1,1,'Current A'),(2,1,'Current B')")
            db.execute("INSERT INTO competition_games(event_id,round,position,team_a,team_b,winner_team_id,core_game_id) VALUES(1,1,1,1,2,1,7)")
        summary = self.lounge.home_summary(self.clock)
        expected = {key: value for key, value in self.competition.game_labels().items() if key >= 3}
        self.assertEqual(summary['game_labels'], expected)
        # An unrelated legacy archive is not parsed when displaying recent five.
        with self.core.transaction() as db:
            db.execute("UPDATE competition_game_archives SET snapshot='invalid old json' WHERE core_game_id=1")
        self.assertEqual(self.lounge.home_summary(self.clock)['game_labels'], expected)

    def test_one_read_connection_without_writer_or_whole_history_helpers(self):
        self.game(1, '2026-09-05T00:00:00.000000+00:00')
        original = self.core.connect
        statements = []
        def connect():
            db = original()
            db.set_trace_callback(statements.append)
            return db
        with patch.object(self.core, 'connect', side_effect=connect) as connections, \
             patch.object(self.core, 'transaction', side_effect=AssertionError('No writer')), \
             patch.object(self.core, 'list_games', side_effect=AssertionError('No whole history')):
            self.lounge.home_summary(self.clock)
        self.assertEqual(connections.call_count, 1)
        self.assertTrue(any('LIMIT 5' in sql for sql in statements))
        self.assertFalse(any(sql.startswith('BEGIN IMMEDIATE') for sql in statements))


if __name__ == '__main__':
    unittest.main()
