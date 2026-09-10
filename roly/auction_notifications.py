"""Dedicated session connection for committed PostgreSQL auction wake-ups.

Notifications are hints, not replayable events. Consumers must read the durable
revision/state again, including after every reconnect, and keep a fallback scan.
This connection never enters the application's transaction connection pool.
"""
import threading

from .postgres import validate_schema


def _connect():
    import psycopg
    from .storage_config import postgres_kwargs

    kwargs = dict(postgres_kwargs())
    kwargs.update(autocommit=True, prepare_threshold=None)
    connection = psycopg.connect(**kwargs)
    try:
        connection.read_only = True
    except BaseException:
        connection.close()
        raise
    return connection


class NotificationListener:
    def __init__(self, schema, on_change, factory=None):
        self.schema = validate_schema(schema)
        self.on_change = on_change
        self.factory = factory or _connect
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._connected = False
        self._connections = 0
        self._failures = 0
        self._callback_errors = 0

    def start(self):
        """Start at most one listener; performs no connection work on the caller."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name="roly-auction-notifications", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=11.0):
        """Request shutdown and report whether the owned thread actually stopped.

        The configured connection timeout is ten seconds; notification waits are
        at most one second. A failed join retains the thread to prevent overlap.
        """
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def status(self):
        """Only bounded counters; never expose connection or exception details."""
        with self._lock:
            return {"connected": self._connected, "connections": self._connections,
                    "failures": self._failures, "callback_errors": self._callback_errors}

    def _changed(self, event_id):
        try:
            self.on_change(event_id)
        except Exception:
            # A stopped event loop/callback cannot kill the connection owner.
            with self._lock:
                self._callback_errors += 1

    def _run(self, stop):
        from psycopg import sql
        from .auction_state import notification_channel

        channel = notification_channel(self.schema)
        backoff = .5
        while not stop.is_set():
            connection = None
            try:
                connection = self.factory()
                if stop.is_set():
                    break
                connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
                with self._lock:
                    self._connected = True
                    self._connections += 1
                # LISTEN becomes active before the caller's refresh is requested.
                self._changed(None)
                while not stop.is_set():
                    notices = connection.notifies(timeout=1.0, stop_after=64)
                    try:
                        for notice in notices:
                            if stop.is_set():
                                break
                            if getattr(notice, "channel", channel) != channel:
                                continue
                            payload = getattr(notice, "payload", "")
                            event_id = int(payload) if isinstance(payload, str) and payload.isascii() and payload.isdecimal() and len(payload) <= 16 else None
                            if event_id is not None and not 0 < event_id <= 2**53 - 1:
                                event_id = None
                            self._changed(event_id)
                    finally:
                        # Release the generator's connection lock before closing.
                        close = getattr(notices, "close", None)
                        if close is not None:
                            close()
                    backoff = .5
            except Exception:
                with self._lock:
                    self._failures += 1
            finally:
                with self._lock:
                    self._connected = False
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if stop.wait(backoff):
                break
            backoff = min(10.0, backoff * 2)
