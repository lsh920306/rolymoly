"""Operational configuration errors never create a local operating database."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from roly import storage_config, ui
from scripts import create_admin


ROOT = Path(__file__).resolve().parents[1]


class StorageSelectionUITests(unittest.TestCase):
    def test_unconfigured_operation_requires_explicit_demo_selection(self):
        with tempfile.TemporaryDirectory(prefix='roly-storage-ui-') as temporary:
            self.addCleanup(ui.services.clear)
            self.addCleanup(ui.lounge_service.clear)
            with patch.dict(os.environ, {'ROLYMOLY_DATA_DIR': temporary}), patch.object(
                ui, 'operating_database', side_effect=storage_config.ConfigError('운영 Supabase 설정이 필요합니다.')
            ), patch.object(ui, 'services', wraps=ui.services) as services, patch(
                'roly.storage_config._runtime_document', side_effect=AssertionError('No real settings')
            ):
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state['space'], '운영 공간')
                self.assertTrue(app.error)
                self.assertNotIn('db_path', app.session_state)
                services.assert_not_called()
                self.assertEqual(list(Path(temporary).iterdir()), [])
                app.selectbox(key='space').select('체험 공간').run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                demo_path = Path(app.session_state['db_path'])
                self.assertEqual(demo_path.parent, Path(temporary))
                self.assertTrue(demo_path.name.startswith('demo-'))
                self.assertTrue(demo_path.exists())
                self.assertTrue(all(Path(call.args[0]) == demo_path for call in services.call_args_list))
                previous_calls = services.call_count
                app.selectbox(key='space').select('운영 공간').run()
                self.assertFalse(app.exception)
                self.assertTrue(app.error)
                self.assertEqual(services.call_count, previous_calls)
                self.assertFalse((Path(temporary) / 'rolymoly.sqlite3').exists())

    def test_private_admin_setup_without_settings_never_connects_or_prompts(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            storage_config, '_runtime_document', return_value={}
        ), patch.object(create_admin, 'Core') as core, patch('builtins.input') as prompt, redirect_stdout(output):
            result = create_admin.main()
        self.assertEqual(result, 2)
        core.assert_not_called()
        prompt.assert_not_called()
        self.assertIn('[supabase]', output.getvalue())


if __name__ == '__main__':
    unittest.main()
