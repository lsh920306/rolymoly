"""Administrator-only, fixed three-query read-only DB probe."""
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import sys
from threading import Lock
from time import monotonic, perf_counter
from uuid import uuid4

from roly.auction_metrics import measure_operation


class ProbeGuard:
    """One active probe per process; bounded deployment cooldowns, no tokens."""
    def __init__(self):
        self.lock = Lock()
        self.deadlines = OrderedDict()
        self.active = None

    def reserve(self, deployment, now):
        with self.lock:
            if self.active is not None:
                return None, "busy", 0.0
            deadline = self.deadlines.get(deployment, 0.0)
            if deadline > now:
                return None, "rate_limited", (deadline - now) * 1000
            for key in tuple(self.deadlines):
                if self.deadlines[key] <= now:
                    del self.deadlines[key]
            if len(self.deadlines) >= 16:
                return None, "capacity_limited", 0.0
            ticket = object()
            self.active = ticket
            self.deadlines[deployment] = now + 60.0
            return ticket, "ok", 0.0

    def release(self, ticket):
        with self.lock:
            if self.active is ticket:
                self.active = None


_guard = ProbeGuard()


def _stage():
    return {"status": "not_attempted", "elapsed_ms": None, "exception_type": None}


def _exception_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else "Exception"


def _record(report, name, entry, operation):
    started = perf_counter()
    try:
        value = operation()
        entry["status"] = "ok"
        return value
    except Exception as error:
        entry.update(status="error", exception_type=_exception_type(error))
        report["errors"].append({"stage": name, "exception_type": entry["exception_type"]})
        raise
    finally:
        entry["elapsed_ms"] = round(max(0.0, perf_counter() - started) * 1000, 3)


def _state(raw):
    name = getattr(raw.info.transaction_status, "name", "UNKNOWN")
    return name if name in ("IDLE", "ACTIVE", "INTRANS", "INERROR", "UNKNOWN") else "UNKNOWN"


def _metadata(raw):
    value = {"diagnostic_version": "db-response-compare-v1", "helper_source_sha256": None,
             "psycopg_version": None, "autocommit": bool(raw.autocommit),
             "prepare_threshold": raw.prepare_threshold if type(raw.prepare_threshold) is int else None,
             "capabilities_pipeline": None, "metadata_errors": []}
    try:
        value["helper_source_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as error:
        value["metadata_errors"].append({"field": "helper_source_sha256", "exception_type": _exception_type(error)})
    try:
        driver_version = version("psycopg")
        if re.fullmatch(r"[A-Za-z0-9.+_-]{1,40}", driver_version):
            value["psycopg_version"] = driver_version
    except PackageNotFoundError as error:
        value["metadata_errors"].append({"field": "psycopg_version", "exception_type": _exception_type(error)})
    # A real Core.connect() has already loaded its driver. No import or private
    # driver hook is needed; fake-only tests intentionally have no driver here.
    capability = getattr(getattr(sys.modules.get("psycopg"), "capabilities", None), "has_pipeline", None)
    if callable(capability):
        try:
            value["capabilities_pipeline"] = bool(capability())
        except Exception as error:
            value["metadata_errors"].append({"field": "capabilities_pipeline", "exception_type": _exception_type(error)})
    return value


def _control(report, raw, case):
    """Fixed public driver calls only; no driver monkeypatch or private hook."""
    prefix = f"pipeline_{case['index']}"
    started = perf_counter()
    cursors = []
    primary = None
    manager = None
    entered = False
    try:
        case["initial_state"] = _state(raw)
        if case["initial_state"] != "IDLE":
            raise RuntimeError("Diagnostic requires an idle lease")
        manager = raw.pipeline()
        _record(report, prefix + ".enter", case["enter"], manager.__enter__)
        entered = True

        def queue():
            raw.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY")
            raw.execute("SET LOCAL lock_timeout = '15s'")
            raw.execute("SET LOCAL statement_timeout = '2s'")
            for _ in range(3):
                cursors.append(raw.execute("SELECT 1"))
            if case["mode"] == "closed":
                raw.execute("ROLLBACK")

        try:
            _record(report, prefix + ".queue", case["queue"], queue)
        except Exception as error:
            primary = error
        try:
            _record(report, prefix + ".exit", case["pipeline_exit"],
                    lambda: manager.__exit__(type(primary) if primary else None, primary,
                                             primary.__traceback__ if primary else None))
        except Exception as error:
            primary = primary or error
        entered = False
        if primary is not None:
            raise primary
        case["exit_state"] = _state(raw)

        def fetch():
            expected = "IDLE" if case["mode"] == "closed" else "INTRANS"
            if case["exit_state"] != expected:
                raise RuntimeError("Unexpected diagnostic transaction state")
            for cursor in cursors:
                row = cursor.fetchone()
                if row is None or len(row) != 1 or type(row[0]) is not int or row[0] != 1:
                    raise ValueError("Unexpected diagnostic result")
                case["verified_rows"] += 1

        _record(report, prefix + ".fetch", case["materialization"], fetch)
        case["status"] = "ok"
    except Exception as error:
        case["status"] = "error"
        if not any(entry["stage"].startswith(prefix + ".") for entry in report["errors"]):
            report["errors"].append({"stage": prefix + ".precondition", "exception_type": _exception_type(error)})
        raise
    finally:
        # If entering a pipeline succeeded, public __exit__ is still required
        # even when local queue code failed before its ordinary exit block.
        if entered:
            try:
                _record(report, prefix + ".exit", case["pipeline_exit"], lambda: manager.__exit__(None, None, None))
            except Exception:
                case["status"] = "error"
        case["pipeline_elapsed_ms"] = round(max(0.0, perf_counter() - started) * 1000, 3)
        try:
            if _state(raw) != "IDLE":
                _record(report, prefix + ".rollback", case["rollback"], raw.rollback)
            else:
                case["rollback"].update(status="not_needed", elapsed_ms=0.0)
        except Exception:
            case["status"] = "error"
        case["final_state"] = _state(raw)
        if case["final_state"] != "IDLE":
            case["status"] = "error"
            report["errors"].append({"stage": prefix + ".final_state", "exception_type": "RuntimeError"})
        case["total_with_cleanup_ms"] = round(max(0.0, perf_counter() - started) * 1000, 3)
    if case["status"] != "ok":
        raise RuntimeError("Diagnostic cleanup failed")


def measure_db(core, token):
    """No SQL/count/target arguments. Uses the existing Core, never initializes."""
    started = perf_counter()
    report = {
        "schema": 1, "run_id": str(uuid4()),
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "deployed_server_to_db_same_readonly_lease",
        "status": "not_started", "ok": False, "requested_samples": 3,
        "completed_samples": 0, "retry_after_ms": 0.0,
        "stages": {name: _stage() for name in ("connection_acquire", "read_setup", "admin_auth", "sequential_rollback", "pipeline_settings", "rollback", "lease_discard", "lease_return")},
        "pool_checkout": {"status": "not_attempted", "count": 0, "elapsed_ms": None, "errors": 0},
        "samples": [{"index": index, **_stage()} for index in range(1, 4)],
        "pipeline_controls": [{"index": index, "mode": mode, "status": "not_attempted", "verified_rows": 0,
                               "initial_state": None, "exit_state": None, "final_state": None, "pipeline_elapsed_ms": None, "total_with_cleanup_ms": None,
                               **{name: _stage() for name in ("enter", "queue", "pipeline_exit", "materialization", "rollback")}}
                              for index, mode in enumerate(("closed", "open", "open", "closed"), 1)],
        "driver_settings": None,
        "errors": [], "total_ms": None,
    }
    if getattr(core, "is_postgres", False) is not True:
        report["status"] = "unsupported_storage"
        report["total_ms"] = round(max(0.0, perf_counter() - started) * 1000, 3)
        return report
    # This marker chooses the existing deployment's cooldown only. It is not
    # returned or derived from a login token or connection credentials.
    deployment = sha256(str(core.db_path).encode()).digest()
    conn = None
    ticket = None
    with measure_operation() as metrics:
        try:
            conn = _record(report, "connection_acquire", report["stages"]["connection_acquire"], core.connect)

            def setup():
                conn.execute("BEGIN")  # PostgreSQL adapter: REPEATABLE READ READ ONLY.
                conn.execute("SET LOCAL statement_timeout = '2s'")

            _record(report, "read_setup", report["stages"]["read_setup"], setup)
            _record(report, "admin_auth", report["stages"]["admin_auth"], lambda: core.require_admin(conn, token))
            # Fresh current-admin verification precedes cooldown reservation.
            ticket, report["status"], retry = _guard.reserve(deployment, monotonic())
            report["retry_after_ms"] = round(retry, 3)
            if ticket is not None:
                for sample in report["samples"]:
                    def select_one():
                        row = conn.execute("SELECT 1").fetchone()
                        if row is None or len(row) != 1 or type(row[0]) is not int or row[0] != 1:
                            raise ValueError("Unexpected diagnostic result")
                    _record(report, f"select_{sample['index']}", sample, select_one)
                    report["completed_samples"] += 1
                _record(report, "sequential_rollback", report["stages"]["sequential_rollback"], conn.rollback)
                # The lease adapter currently exposes no public raw property.
                # Read this one adapter-owned reference; all driver operations
                # below use public APIs and never modify the driver globally.
                raw = conn._raw
                report["driver_settings"] = _metadata(raw)

                def check_settings():
                    if raw.autocommit is not True or raw.prepare_threshold is not None:
                        raise RuntimeError("Uncontrolled driver settings")
                    if report["driver_settings"]["capabilities_pipeline"] is False:
                        raise RuntimeError("Pipeline capability unavailable")

                _record(report, "pipeline_settings", report["stages"]["pipeline_settings"], check_settings)
                for case in report["pipeline_controls"]:
                    _control(report, raw, case)
        except Exception as error:
            if not report["errors"]:
                report["errors"].append({"stage": "probe", "exception_type": _exception_type(error)})
            report["status"] = "auth_denied" if isinstance(error, PermissionError) and report["stages"]["admin_auth"]["status"] == "error" else "error"
        finally:
            if conn is not None:
                try:
                    _record(report, "rollback", report["stages"]["rollback"], conn.rollback)
                except Exception:
                    pass  # Preserve the failed rollback and still return/discard the lease.
                raw = getattr(conn, "_raw", None)
                if raw is not None and _state(raw) != "IDLE":
                    try:
                        _record(report, "lease_discard", report["stages"]["lease_discard"], raw.close)
                    except Exception:
                        pass
                try:
                    _record(report, "lease_return", report["stages"]["lease_return"], conn.close)
                except Exception:
                    pass
            if ticket is not None:
                _guard.release(ticket)
        checkout = metrics.as_dict().get("pool_checkout")
        if checkout is not None:
            report["pool_checkout"] = {"status": "error" if checkout["errors"] else "ok", **checkout}
        elif conn is not None:
            report["pool_checkout"]["status"] = "unavailable"
    if report["errors"] and report["status"] != "auth_denied":
        report["status"] = "error"
    report["ok"] = (report["status"] == "ok" and report["completed_samples"] == 3 and not report["errors"]
                    and all(case["status"] == "ok" for case in report["pipeline_controls"]))
    report["total_ms"] = round(max(0.0, perf_counter() - started) * 1000, 3)
    return report
