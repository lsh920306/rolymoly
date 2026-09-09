"""Real entrypoint and native form payloads against a disposable local database."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly.core import Core
from roly.lounge import Lounge
from roly.ui import services, lounge_service


ROOT = Path(__file__).resolve().parents[1]


class LoungeProfileUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='roly-lounge-form-')
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'test.sqlite3'
        environment = patch.dict(os.environ, {'ROLYMOLY_DATABASE_TARGET': str(path), 'ROLYMOLY_DATA_DIR': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        settings = patch('roly.storage_config._runtime_document', side_effect=AssertionError('No real settings'))
        settings.start()
        self.addCleanup(settings.stop)
        self.addCleanup(services.clear)
        self.addCleanup(lounge_service.clear)
        self.core = Core(path)
        self.core.setup_admin('admin', 'synthetic-admin-password')
        self.token = self.core.login('admin', 'synthetic-admin-password')
        self.lounge = Lounge(self.core)
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30)
        for key, value in {'space': '운영 공간', 'db_path': str(path), 'token': self.token}.items():
            self.app.session_state[key] = value
        self.app.run()
        self.widget('button', '소개 수정').click().run()
        self.healthy()

    def widget(self, kind, label):
        return next(w for w in getattr(self.app, kind) if w.label == label)

    def healthy(self):
        self.assertFalse(self.app.exception, [e.message for e in self.app.exception])

    def audit_count(self):
        with self.core.transaction() as db:
            return db.execute("SELECT count(*) FROM audit WHERE action='CLAN_PROFILE_UPDATE'").fetchone()[0]

    def test_native_stale_form_preserves_input_and_requires_explicit_reload(self):
        self.lounge.update_profile(self.token, name='Other title', description='Other description', capacity=77)
        self.widget('text_area', '소개').set_value('My unsaved draft')
        self.widget('button', '소개 저장').click()
        wire = self.app._tree.get_widget_states()
        self.assertTrue(any(w.WhichOneof('value') == 'string_value' and w.string_value == 'My unsaved draft' for w in wire.widgets))
        self.app._run(wire)
        self.healthy()
        self.assertEqual(self.widget('text_area', '소개').value, 'My unsaved draft')
        self.assertTrue(self.app.warning)
        self.assertTrue(self.app.error)
        self.assertFalse(self.app.success)
        self.assertEqual(self.lounge.profile()['description'], 'Other description')
        self.assertEqual(self.audit_count(), 1)
        self.widget('button', '최신 소개 불러오기').click().run()
        self.healthy()
        self.assertEqual(self.widget('text_area', '소개').value, 'Other description')
        self.assertEqual(self.widget('text_input', '클랜명').value, 'Other title')
        self.widget('text_area', '소개').set_value('Reviewed fresh draft')
        self.widget('button', '소개 저장').click().run()
        self.healthy()
        self.assertEqual(self.lounge.profile()['description'], 'Reviewed fresh draft')
        self.assertEqual(self.lounge.profile()['capacity'], 77)
        self.assertNotIn('_clan_profile_editor', self.app.session_state)

    def test_committed_response_loss_retry_preserves_draft_and_writes_once(self):
        self.widget('text_area', '소개').set_value('Committed draft')
        original = Lounge.update_profile

        def lost_response(service, *args, **kwargs):
            original(service, *args, **kwargs)
            raise sqlite3.OperationalError('SYNTHETIC_PRIVATE_DRIVER_ERROR')

        with patch.object(Lounge, 'update_profile', lost_response):
            self.widget('button', '소개 저장').click().run()
        self.healthy()
        self.assertTrue(self.app.error)
        self.assertFalse(any('SYNTHETIC_PRIVATE_DRIVER_ERROR' in e.value for e in self.app.error))
        self.assertEqual(self.widget('text_area', '소개').value, 'Committed draft')
        self.assertEqual(self.audit_count(), 1)
        self.widget('button', '소개 저장').click().run()
        self.healthy()
        self.assertTrue(self.app.success)
        self.assertEqual(self.audit_count(), 1)
        self.assertEqual(self.lounge.profile()['description'], 'Committed draft')
        self.assertNotIn('_clan_profile_editor', self.app.session_state)

    def test_change_after_render_is_rechecked_inside_save_transaction(self):
        self.widget('text_area', '소개').set_value('Late draft')
        original = Lounge.update_profile
        called = False

        def intervening_change(service, token, **kwargs):
            nonlocal called
            if not called:
                called = True
                original(service, token, name='Intervening change', description='Keep latest')
            return original(service, token, **kwargs)

        with patch.object(Lounge, 'update_profile', intervening_change):
            self.widget('button', '소개 저장').click().run()
        self.healthy()
        self.assertTrue(self.app.error)
        self.assertFalse(self.app.success)
        self.assertEqual(self.widget('text_area', '소개').value, 'Late draft')
        self.assertEqual(self.lounge.profile()['description'], 'Keep latest')
        self.assertEqual(self.audit_count(), 1)


if __name__ == '__main__':
    unittest.main()
