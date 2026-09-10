"""Bounded timing counters for an explicitly measured operation.

Context variables follow AnyIO's copied context into request worker threads.
Only fixed stage names, durations and counts are retained: no SQL, credentials,
request payloads or exception text. Background pool work has separate process
counters and must not be attributed to the request observing those counters.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from math import isfinite
from threading import Lock
from time import perf_counter


STAGES = frozenset({"dispatch", "pool_checkout", "writer_begin", "read_begin",
                    "read_pipeline", "write_pipeline", "bid_commit_pipeline", "statement", "commit", "rollback"})
_BACKGROUND_STAGES = frozenset({"pool_reset", "pool_check"})
_current = ContextVar("roly_auction_metrics", default=None)


class OperationMetrics:
    def __init__(self, allowed=STAGES):
        self._allowed = allowed
        self._values = {}
        self._lock = Lock()
        self._closed = False

    def record(self, stage, seconds, *, failed=False):
        """Add a duration in seconds; stage names are deliberately bounded."""
        if stage not in self._allowed:
            raise ValueError("Unknown timing stage")
        if not isfinite(seconds):
            return
        with self._lock:
            if self._closed:
                return
            row = self._values.setdefault(stage, [0, 0.0, 0])
            row[0] += 1
            row[1] += max(0.0, seconds) * 1000
            row[2] += bool(failed)

    def as_dict(self):
        with self._lock:
            return {stage: {"count": row[0], "elapsed_ms": round(row[1], 3), "errors": row[2]}
                    for stage, row in sorted(self._values.items())}

    def _close(self):
        with self._lock:
            self._closed = True


_background = OperationMetrics(_BACKGROUND_STAGES)


@contextmanager
def measure_operation():
    """Start a fresh request scope, restoring any enclosing scope on exit."""
    metrics = OperationMetrics()
    token = _current.set(metrics)
    try:
        yield metrics
    finally:
        metrics._close()
        _current.reset(token)


@contextmanager
def _duration(metrics, stage):
    started = perf_counter()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        metrics.record(stage, perf_counter() - started, failed=failed)


def measure_stage(stage):
    """Time a foreground stage only while a request scope is active."""
    metrics = _current.get()
    return _duration(metrics, stage) if metrics is not None else nullcontext()


def measure_background(stage):
    return _duration(_background, stage)


def pool_background_stats():
    """Return process totals; these include work for other concurrent requests."""
    return _background.as_dict()
