"""Dedicated LISTEN ownership/recovery with fake connections only."""
from collections import deque
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from roly.auction_notifications import NotificationListener, _connect


class FakeConnection:
    def __init__(self, notices=(), error=None):
        self.notices = deque(notices)
        self.error = error
        self.calls = []
        self.closed = threading.Event()
        self.listening = threading.Event()

    def execute(self, statement):
        self.calls.append(statement.as_string())
        self.listening.set()

    def notifies(self, *, timeout, stop_after):
        self.calls.append(("notifies", timeout, stop_after))
        if self.error:
            raise self.error
        if self.notices:
            yield SimpleNamespace(channel="auction_test", payload=self.notices.popleft())
        else:
            self.closed.wait(.01)

    def close(self):
        self.closed.set()


class AuctionNotificationsTests(unittest.TestCase):
    def setUp(self):
        channel = patch("roly.auction_state.notification_channel", return_value="auction_test")
        channel.start()
        self.addCleanup(channel.stop)
        blocked = patch("psycopg.connect", side_effect=AssertionError("No live PostgreSQL"))
        blocked.start()
        self.addCleanup(blocked.stop)

    def listener(self, *args, **kwargs):
        listener = NotificationListener("rolymoly", *args, **kwargs)
        self.addCleanup(listener.stop)
        return listener

    def test_start_is_idempotent_and_stop_closes_dedicated_connection(self):
        connection = FakeConnection(["23", "*", "-1", "9007199254740992"])
        seen = []
        done = threading.Event()
        def changed(value):
            seen.append(value)
            if len(seen) == 5:
                done.set()
        listener = self.listener(changed, factory=lambda: connection)
        self.assertTrue(listener.start())
        self.assertFalse(listener.start())
        self.assertTrue(done.wait(2))
        self.assertEqual(seen, [None, 23, None, None, None])
        self.assertTrue(listener.stop(2))
        self.assertTrue(connection.closed.is_set())
        self.assertEqual(connection.calls[0], 'LISTEN "auction_test"')
        self.assertFalse(listener.status()["connected"])

    def test_connection_failure_reconnects_and_callback_exception_is_contained(self):
        first = FakeConnection(error=RuntimeError("private-server-host"))
        second = FakeConnection(["17"])
        connections = deque([first, second])
        delivered = threading.Event()
        def changed(value):
            if value is None:
                raise RuntimeError("private-token")
            delivered.set()
        listener = self.listener(changed, factory=connections.popleft)
        listener.start()
        self.assertTrue(delivered.wait(3))
        self.assertTrue(listener.stop(2))
        status = listener.status()
        self.assertEqual(status["connections"], 2)
        self.assertEqual(status["failures"], 1)
        self.assertEqual(status["callback_errors"], 2)
        self.assertTrue(first.closed.is_set() and second.closed.is_set())
        self.assertNotIn("private", json.dumps(status))

    def test_stop_during_connection_retains_owner_until_factory_returns(self):
        entered, release = threading.Event(), threading.Event()
        connection = FakeConnection()
        def factory():
            entered.set()
            release.wait(2)
            return connection
        listener = self.listener(lambda event: None, factory=factory)
        listener.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertFalse(listener.stop(0))
            self.assertFalse(listener.start())
        finally:
            release.set()
        self.assertTrue(listener.stop(2))
        self.assertTrue(connection.closed.is_set())
        self.assertFalse(connection.listening.is_set())

    def test_default_connection_uses_session_settings_without_application_pool(self):
        connection = SimpleNamespace(close=lambda: None)
        with patch("roly.storage_config.postgres_kwargs", return_value={"host": "fixture", "port": 5432}), \
             patch("psycopg.connect", return_value=connection) as driver, \
             patch("roly.postgres._connection_pool", side_effect=AssertionError("LISTEN must own its connection")):
            self.assertIs(_connect(), connection)
        self.assertEqual(driver.call_args.kwargs, {"host": "fixture", "port": 5432, "autocommit": True, "prepare_threshold": None})
        self.assertTrue(connection.read_only)


if __name__ == "__main__":
    unittest.main()
