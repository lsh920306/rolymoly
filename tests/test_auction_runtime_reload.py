"""An ASGI lifetime remains discoverable after Streamlit reimports UI modules."""
import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from roly import auction_http as http


class AuctionRuntimeReloadTests(unittest.TestCase):
    def setUp(self):
        self.registry = http._runtime_registry()
        self.assertIsNone(self.registry['runtime'])

    def tearDown(self):
        self.assertIsNone(self.registry['runtime'])

    def reimport(self):
        spec = importlib.util.spec_from_file_location(http.__name__, Path(http.__file__))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_reimport_keeps_server_epoch_and_retires_with_original_lifespan(self):
        async def exercise():
            with patch.object(http, '_target', return_value='supabase://rolymoly'), \
                 patch.object(http, '_services', side_effect=AssertionError('No DB during startup')):
                async with http.lifespan(None):
                    original = http.get_runtime()
                    refreshed = self.reimport()
                    self.assertIsNone(refreshed._runtime)
                    self.assertIs(refreshed.get_runtime(), original)
                    self.assertIs(refreshed.TransportError, http.TransportError)
                    with self.assertRaises(refreshed.TransportError) as rejected:
                        original.check_epoch('previous-process')
                    self.assertEqual(rejected.exception.status, 409)
                    self.assertTrue(rejected.exception.reload)
                    config = refreshed.transport_config('supabase://rolymoly', 'synthetic-session-token', 7)
                    self.assertEqual(config['epoch'], original.epoch)
                    self.assertEqual(config['ws_url'], '/api/auction/ws')
                    self.assertIsNone(refreshed.transport_config('supabase://other', 'synthetic-session-token', 7))
                self.assertIsNone(refreshed.get_runtime())
                self.assertIsNone(refreshed.transport_config('supabase://rolymoly', 'synthetic-session-token', 7))
        asyncio.run(exercise())

    def test_reimported_endpoint_reports_original_runtime_epoch_error(self):
        from starlette.applications import Starlette
        from tests.test_auction_http import call_asgi
        async def exercise():
            with patch.object(http, '_target', return_value='supabase://rolymoly'):
                async with http.lifespan(None):
                    refreshed = self.reimport()
                    app = Starlette(routes=refreshed.routes(base_url=''))
                    status, value, _ = await call_asgi(app, {'event_id': 7}, {
                        'host': 'auction.test', 'origin': 'https://auction.test',
                        'content-type': 'application/json', http.SESSION_HEADER: 'synthetic-session-token',
                        http.EPOCH_HEADER: 'previous-process'})
                    self.assertEqual(status, 409)
                    self.assertEqual(value['error']['code'], 'server_epoch_changed')
                    self.assertTrue(value['error']['reload_required'])
        asyncio.run(exercise())

    def test_reimport_cannot_register_a_second_lifespan(self):
        async def exercise():
            with patch.object(http, '_target', return_value='supabase://rolymoly'):
                async with http.lifespan(None):
                    original = http.get_runtime()
                    refreshed = self.reimport()
                    with patch.object(refreshed, '_target', return_value='supabase://rolymoly'):
                        with self.assertRaisesRegex(RuntimeError, 'already active'):
                            async with refreshed.lifespan(None):
                                self.fail('Duplicate lifespan must not start')
                    self.assertIs(refreshed.get_runtime(), original)
                    self.assertTrue(original.active)
        asyncio.run(exercise())

    def test_failed_or_cancelled_close_always_retires_runtime(self):
        async def exercise(error):
            with patch.object(http, '_target', return_value='supabase://rolymoly'):
                with self.assertRaises(type(error)):
                    async with http.lifespan(None):
                        runtime = http.get_runtime()
                        runtime.push.close = AsyncMock(side_effect=error)
                self.assertFalse(runtime.active)
                self.assertIsNone(http.get_runtime())
                self.assertIsNone(self.registry['runtime'])
        for error in (RuntimeError('synthetic close failure'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                asyncio.run(exercise(error))


if __name__ == '__main__':
    unittest.main()
