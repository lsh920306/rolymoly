import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import time
from threading import Barrier

from roly.core import Core
from roly.lounge import Lounge


class LoungeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='roly-lounge-')
        self.addCleanup(self.temp.cleanup)
        self.core = Core(Path(self.temp.name) / 'test.sqlite3')
        self.core.setup_admin('admin', 'test-only-password')
        self.admin = self.core.login('admin', 'test-only-password')
        self.core.create_account(self.admin, 'host', 'test-only-password', role='organizer')
        self.host = self.core.login('host', 'test-only-password')
        self.lounge = Lounge(self.core)

    def test_profile_permissions_validation_and_persistence(self):
        with self.assertRaises(PermissionError):
            self.lounge.update_profile(self.host, name='forbidden')
        for url in ('javascript:alert(1)', 'http://example.com', 'https://name:password@example.com', 'https://example.com\nother'):
            with self.assertRaises(ValueError):
                self.lounge.update_profile(self.admin, name='forbidden', contact_url=url)
        self.assertEqual(self.lounge.profile()['name'], '롤리몰리')
        self.lounge.update_profile(self.admin, name='롤리몰리 클랜', description='소개', capacity=50, founded_on='2025-01-02')
        saved = Lounge(Core(self.core.db_path)).profile()
        self.assertEqual((saved['name'], saved['capacity'], saved['poster']), ('롤리몰리 클랜', 50, 'rolymoly'))
        with self.core.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='CLAN_PROFILE_UPDATE'").fetchone()[0], 1)

    def test_profile_changes_preserve_retired_schedule_data(self):
        # Simulate the previous schema. Retiring its UI and service must leave
        # existing records intact when an old database is opened again.
        with self.core.transaction() as db:
            db.execute("""CREATE TABLE clan_meetings(
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at TEXT NOT NULL,
                place TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('SCHEDULED','COMPLETED','CANCELLED')),
                created_by INTEGER NOT NULL REFERENCES accounts(id), created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            db.execute("INSERT INTO clan_meetings VALUES(1,'기존 모임','2026-10-01T11:00:00+00:00','채널','기존 설명','CANCELLED',1,'2026-09-01','2026-09-02')")
            before = tuple(db.execute('SELECT * FROM clan_meetings').fetchone())
        reopened = Lounge(Core(self.core.db_path))
        reopened.update_profile(self.admin, name='새 클랜 소개')
        with self.core.transaction() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM clan_meetings').fetchone()), before)
        self.assertEqual(reopened.profile()['name'], '새 클랜 소개')

    def test_two_reviewed_admin_edits_commit_only_one_change(self):
        self.core.create_account(self.admin, 'second-admin', 'test-only-password', role='admin')
        second = self.core.login('second-admin', 'test-only-password')
        version = self.lounge.profile()['updated_at']
        barrier = Barrier(2)

        def save(actor, name):
            barrier.wait(timeout=10)
            try:
                self.lounge.update_profile(actor, name=name, expected_updated_at=version)
                return name
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(save, actor, name) for actor, name in ((self.admin, 'First draft'), (second, 'Second draft'))]
            saved = [job.result(timeout=15) for job in jobs]
        winner = [name for name in saved if name is not None]
        self.assertEqual(len(winner), 1)
        self.assertEqual(self.lounge.profile()['name'], winner[0])
        with self.core.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='CLAN_PROFILE_UPDATE'").fetchone()[0], 1)

    def test_committed_retry_is_noop_but_newer_different_change_is_preserved(self):
        version = self.lounge.profile()['updated_at']
        first = self.lounge.update_profile(self.admin, name='Saved draft', description='Original request', expected_updated_at=version)
        replay = self.lounge.update_profile(self.admin, name='Saved draft', description='Original request', expected_updated_at=version)
        self.assertEqual(replay, first)
        with self.assertRaises(PermissionError):
            self.lounge.update_profile(self.host, name='Saved draft', description='Original request', expected_updated_at=version)
        with self.core.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='CLAN_PROFILE_UPDATE'").fetchone()[0], 1)
        self.lounge.update_profile(self.admin, name='Newer value', expected_updated_at=first['updated_at'])
        with self.assertRaises(ValueError):
            self.lounge.update_profile(self.admin, name='Saved draft', description='Original request', expected_updated_at=version)
        self.assertEqual(self.lounge.profile()['name'], 'Newer value')

    def test_profile_version_increases_even_when_clock_does_not_advance(self):
        original = self.lounge.profile()['updated_at']
        with patch('roly.lounge.now', return_value=original):
            first = self.lounge.update_profile(self.admin, name='First', expected_updated_at=original)
            second = self.lounge.update_profile(self.admin, name='Second', expected_updated_at=first['updated_at'])
        self.assertGreater(first['updated_at'], original)
        self.assertGreater(second['updated_at'], first['updated_at'])

    def test_concurrent_old_profile_migration_preserves_saved_fields(self):
        with self.core.transaction() as db:
            db.execute('DROP TABLE clan_profile')
            db.execute("""CREATE TABLE clan_profile(
                id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',founded_on TEXT NOT NULL DEFAULT '',
                capacity INTEGER,contact_url TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL)""")
            db.execute("INSERT INTO clan_profile VALUES(1,'기존 클랜','저장된 소개','2025-01-02',40,'https://example.com','2026-01-01')")
        original_connect = self.core.connect

        class SlowAlterConnection:
            # Allow another initializer to overlap a realistically slow DDL call.
            # The migrated initializer must serialize the schema check and change.
            def __init__(self):
                self.db = original_connect()

            def execute(self, sql, *args):
                if sql.startswith('ALTER TABLE clan_profile'):
                    time.sleep(.1)
                return self.db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.db, name)

        with patch.object(self.core, 'connect', side_effect=SlowAlterConnection):
            with ThreadPoolExecutor(max_workers=2) as pool:
                profiles = list(pool.map(lambda _: Lounge(self.core).profile(), range(2)))
        self.assertEqual(profiles[0], profiles[1])
        self.assertEqual((profiles[0]['name'], profiles[0]['description'], profiles[0]['founded_on'],
                          profiles[0]['capacity'], profiles[0]['contact_url'], profiles[0]['poster']),
                         ('기존 클랜', '저장된 소개', '2025-01-02', 40, 'https://example.com', 'rolymoly'))
