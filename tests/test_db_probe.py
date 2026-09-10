import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Event, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.auction_metrics import measure_stage
from streamlit.testing.v1 import AppTest
from tests.test_native_blocks import native_blocks
from roly import db_probe as probe


class Cursor:
    def __init__(self, raw):
        self.raw = raw

    def fetchone(self):
        if self.raw.in_pipeline:
            raise AssertionError("Fetch happened before pipeline exit")
        self.raw.fetches += 1
        if self.raw.fail_fetch == self.raw.fetches:
            raise RuntimeError("private-dsn password member-value")
        return (1,)


class Raw:
    autocommit = True
    prepare_threshold = None

    def __init__(self):
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        self.log = []
        self.in_pipeline = False
        self.pending = []
        self.pipeline_number = self.fetches = self.selects = 0
        self.fail_exit = self.fail_select = self.fail_fetch = None
        self.fail_setup = False
        self.fail_rollback = self.fail_close = False
        self.rollback_count = 0
        self.sticky_rollback_at = None
        self.rollback_hook = None
        self.block = None
        self.closed = False

    def _apply(self, query):
        if query.startswith("BEGIN"):
            if "READ ONLY" not in query:
                raise AssertionError("Writable transaction")
            self.info.transaction_status.name = "INTRANS"
        elif query == "ROLLBACK":
            self.info.transaction_status.name = "IDLE"

    def execute(self, query):
        allowed = ("BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY", "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
                   "SET LOCAL statement_timeout = '2s'", "SET LOCAL lock_timeout = '15s'", "SELECT 1", "ROLLBACK")
        if query not in allowed:
            raise AssertionError("Unexpected SQL")
        self.log.append(query)
        if self.fail_setup and query == "SET LOCAL statement_timeout = '2s'":
            raise RuntimeError("private-dsn password setup")
        if query == "SELECT 1":
            self.selects += 1
            if self.block and self.selects == 1:
                self.block[0].set()
                if not self.block[1].wait(3):
                    raise RuntimeError("Timed out")
            if self.fail_select == self.selects:
                raise RuntimeError("private-dsn password token")
        if self.in_pipeline:
            self.pending.append(query)
        else:
            self._apply(query)
        return Cursor(self)

    def pipeline(self):
        raw = self
        class Pipeline:
            def __enter__(self):
                raw.pipeline_number += 1
                raw.in_pipeline = True
                return self
            def __exit__(self, *args):
                raw.in_pipeline = False
                for query in raw.pending:
                    raw._apply(query)
                raw.pending.clear()
                if raw.fail_exit == raw.pipeline_number:
                    raise RuntimeError("private-dsn password pipeline")
        return Pipeline()

    def rollback(self):
        self.log.append("ROLLBACK")
        self.rollback_count += 1
        if self.rollback_hook:
            self.rollback_hook()
        if self.fail_rollback:
            raise RuntimeError("private-dsn password rollback")
        if self.rollback_count != self.sticky_rollback_at:
            self.info.transaction_status.name = "IDLE"

    def close(self):
        self.closed = True
        self.info.transaction_status.name = "UNKNOWN"


class Lease:
    def __init__(self, raw):
        self._raw = raw
        self.returned = 0
    def execute(self, query):
        if query == "BEGIN":
            query = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        return self._raw.execute(query)
    def rollback(self):
        self._raw.rollback()
    def close(self):
        self.returned += 1
        self._raw.closed = True
        if self._raw.fail_close:
            raise RuntimeError("private-dsn password close")


class Core:
    is_postgres = True
    db_path = "supabase://synthetic-deployment"
    def __init__(self):
        self.admin = True
        self.connects = self.auths = 0
        self.raw = Raw()
        self.lease = Lease(self.raw)
        self.fail_connect = False
    def connect(self):
        self.connects += 1
        with measure_stage("pool_checkout"):
            if self.fail_connect:
                raise RuntimeError("private-dsn password checkout")
            return self.lease
    def require_admin(self, conn, token):
        self.auths += 1
        if conn is not self.lease or self.raw.info.transaction_status.name != "INTRANS":
            raise AssertionError("Authorization outside the read snapshot")
        self.raw.log.append("AUTH")
        if not self.admin or token != "synthetic-admin":
            raise PermissionError("private-token denied")
        return {"role": "admin"}


class CandidateProbeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(probe, "_guard", probe.ProbeGuard()))

    def test_one_lease_fixed_queries_and_ab_ba_exit_states(self):
        core = Core()
        report = probe.measure_db(core, "synthetic-admin")
        self.assertTrue(report["ok"], report)
        self.assertEqual((core.connects, core.auths, core.lease.returned), (1, 1, 1))
        self.assertEqual(core.raw.selects, 15)
        self.assertEqual(core.raw.fetches, 15)
        self.assertEqual(core.raw.log[:6], ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY", "SET LOCAL statement_timeout = '2s'", "AUTH", "SELECT 1", "SELECT 1", "SELECT 1"])
        self.assertEqual([x["mode"] for x in report["pipeline_controls"]], ["closed", "open", "open", "closed"])
        self.assertEqual([x["exit_state"] for x in report["pipeline_controls"]], ["IDLE", "INTRANS", "INTRANS", "IDLE"])
        self.assertTrue(all(x["verified_rows"] == 3 and x["final_state"] == "IDLE" for x in report["pipeline_controls"]))
        self.assertEqual(report["pool_checkout"]["count"], 1)
        self.assertEqual(report["completed_samples"], 3)
        self.assertTrue(all(x["elapsed_ms"] is not None for x in report["samples"]))
        self.assertEqual(report["driver_settings"]["diagnostic_version"], "db-response-compare-v1")
        self.assertRegex(report["driver_settings"]["helper_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(report["driver_settings"]["autocommit"])

    def test_unauthorized_and_revoked_auth_do_not_consume_cooldown(self):
        for token, active in (("not-admin", True), ("synthetic-admin", False)):
            core = Core(); core.admin = active
            report = probe.measure_db(core, token)
            self.assertEqual(report["status"], "auth_denied")
            self.assertEqual(core.raw.selects, 0)
            self.assertEqual(core.lease.returned, 1)
            self.assertFalse(probe._guard.deadlines)
        self.assertTrue(probe.measure_db(Core(), "synthetic-admin")["ok"])

    def test_cooldown_still_checks_current_auth_and_does_not_probe(self):
        self.assertTrue(probe.measure_db(Core(), "synthetic-admin")["ok"])
        core = Core()
        report = probe.measure_db(core, "synthetic-admin")
        self.assertEqual(report["status"], "rate_limited")
        self.assertEqual((core.auths, core.raw.selects, core.lease.returned), (1, 0, 1))
        core = Core(); core.admin = False
        self.assertEqual(probe.measure_db(core, "synthetic-admin")["status"], "auth_denied")

    def test_concurrent_sessions_allow_only_one_probe(self):
        entered, resume = Event(), Event()
        core = Core(); core.raw.block = (entered, resume)
        results = []
        worker = Thread(target=lambda: results.append(probe.measure_db(core, "synthetic-admin")))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            other = Core()
            result = probe.measure_db(other, "synthetic-admin")
            self.assertEqual(result["status"], "busy")
            self.assertEqual((other.auths, other.raw.selects, other.lease.returned), (1, 0, 1))
        finally:
            resume.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["ok"])

    def test_guard_is_bounded_by_deployment_and_releases_on_expiry(self):
        guard = probe.ProbeGuard()
        for index in range(16):
            ticket, status, _ = guard.reserve(bytes([index]), 100)
            self.assertEqual(status, "ok"); guard.release(ticket)
        self.assertEqual(guard.reserve(b"new", 100)[1], "capacity_limited")
        self.assertEqual(guard.reserve(b"new", 160)[1], "ok")
        self.assertEqual(len(guard.deadlines), 1)

    def test_select_failure_stops_remaining_samples_and_keeps_cleanup_errors(self):
        core = Core(); core.raw.fail_select = 2; core.raw.fail_rollback = core.raw.fail_close = True
        report = probe.measure_db(core, "synthetic-admin")
        self.assertEqual(report["completed_samples"], 1)
        self.assertEqual([x["status"] for x in report["samples"]], ["ok", "error", "not_attempted"])
        self.assertTrue(all(x["status"] == "not_attempted" for x in report["pipeline_controls"]))
        self.assertEqual([x["stage"] for x in report["errors"]], ["select_2", "rollback", "lease_return"])
        self.assertEqual(core.lease.returned, 1)
        self.assertEqual(report["stages"]["lease_discard"]["status"], "ok")
        self.assertIsNone(probe._guard.active)
        self.assertNotIn("private", json.dumps(report))
        self.assertNotIn("password", json.dumps(report))

    def test_pipeline_exit_or_fetch_failure_stops_later_cases(self):
        for option, value, expected in (("fail_exit", 2, "pipeline_2.exit"), ("fail_fetch", 7, "pipeline_2.fetch")):
            with self.subTest(option=option), patch.object(probe, "_guard", probe.ProbeGuard()):
                core = Core(); setattr(core.raw, option, value)
                report = probe.measure_db(core, "synthetic-admin")
                self.assertFalse(report["ok"])
                self.assertEqual(report["pipeline_controls"][1]["status"], "error")
                self.assertEqual(report["pipeline_controls"][2]["status"], "not_attempted")
                self.assertIn(expected, [x["stage"] for x in report["errors"]])
                self.assertEqual((core.lease.returned, core.raw.info.transaction_status.name), (1, "IDLE"))

    def test_connect_failure_and_non_postgres_never_run_sql(self):
        core = Core(); core.fail_connect = True
        report = probe.measure_db(core, "synthetic-admin")
        self.assertEqual(report["stages"]["connection_acquire"]["status"], "error")
        self.assertEqual(report["pool_checkout"]["errors"], 1)
        self.assertEqual(core.raw.log, [])
        core = Core(); core.is_postgres = False
        self.assertEqual(probe.measure_db(core, "synthetic-admin")["status"], "unsupported_storage")
        self.assertEqual(core.connects, 0)

    def test_read_setup_failure_prevents_auth_cooldown_and_probes(self):
        core = Core(); core.raw.fail_setup = True
        report = probe.measure_db(core, "synthetic-admin")
        self.assertEqual([x["stage"] for x in report["errors"]], ["read_setup"])
        self.assertEqual((core.auths, core.raw.selects, core.lease.returned), (0, 0, 1))
        self.assertFalse(probe._guard.deadlines)

    def test_queue_and_exit_errors_are_both_preserved_before_cleanup(self):
        core = Core(); core.raw.fail_select = 5; core.raw.fail_exit = 1
        report = probe.measure_db(core, "synthetic-admin")
        self.assertFalse(report["ok"])
        self.assertEqual([x["stage"] for x in report["errors"]], ["pipeline_1.queue", "pipeline_1.exit"])
        self.assertEqual(report["pipeline_controls"][1]["status"], "not_attempted")
        self.assertEqual((core.lease.returned, core.raw.info.transaction_status.name), (1, "IDLE"))

    def test_non_idle_cleanup_stops_next_pair_and_never_reports_success(self):
        core = Core(); core.raw.sticky_rollback_at = 2
        report = probe.measure_db(core, "synthetic-admin")
        self.assertFalse(report["ok"])
        self.assertEqual(report["pipeline_controls"][1]["final_state"], "INTRANS")
        self.assertEqual(report["pipeline_controls"][2]["status"], "not_attempted")
        self.assertIn("pipeline_2.final_state", [x["stage"] for x in report["errors"]])
        self.assertEqual(core.lease.returned, 1)

    def test_changed_driver_mode_stops_controlled_pairs(self):
        for field, value in (("autocommit", False), ("prepare_threshold", 5)):
            with self.subTest(field=field), patch.object(probe, "_guard", probe.ProbeGuard()):
                core = Core(); setattr(core.raw, field, value)
                report = probe.measure_db(core, "synthetic-admin")
                self.assertEqual(report["completed_samples"], 3)
                self.assertFalse(report["ok"])
                self.assertEqual(report["stages"]["pipeline_settings"]["status"], "error")
                self.assertEqual(core.raw.pipeline_number, 0)

    def test_pipeline_comparison_excludes_separate_open_cleanup(self):
        core = Core()
        clock = [0.0]
        core.raw.rollback_hook = lambda: clock.__setitem__(0, clock[0] + 0.025)
        with patch.object(probe, "perf_counter", side_effect=lambda: clock[0]):
            report = probe.measure_db(core, "synthetic-admin")
        self.assertTrue(report["ok"])
        for case in report["pipeline_controls"]:
            self.assertEqual(case["pipeline_elapsed_ms"], 0.0)
            self.assertEqual(case["total_with_cleanup_ms"], 25.0 if case["mode"] == "open" else 0.0)

    def test_ui_never_probes_on_render_open_or_unrelated_rerun(self):
        core = Core()
        script = '''
import streamlit as st
from roly.db_probe_ui import render_db_probe
render_db_probe(st.session_state.core, "synthetic-admin")
'''
        with native_blocks(), patch("roly.db_probe_ui.measure_db", return_value=probe.measure_db(core, "synthetic-admin")) as execute:
            app = AppTest.from_string(script, default_timeout=10)
            app.session_state.core = core
            app.run(); self.assertFalse(app.exception); execute.assert_not_called()
            app.session_state.admin_db_probe_panel = True
            app.run(); self.assertFalse(app.exception); execute.assert_not_called()
            app.run(); execute.assert_not_called()
            app.button(key="admin_db_probe_run").click().run()
            self.assertFalse(app.exception); execute.assert_called_once()
            self.assertTrue(app.code)
            displayed = json.loads(app.code[0].value)
            self.assertEqual(displayed["requested_samples"], 3)
            self.assertEqual(len(displayed["pipeline_controls"]), 4)
            app.run(); execute.assert_called_once()


class AdminProbePageTests(unittest.TestCase):
    def setUp(self):
        from roly.core import Core as LocalCore
        from roly.competition import Competition
        self.enterContext(native_blocks())
        temporary = TemporaryDirectory(prefix="roly-admin-db-probe-")
        self.addCleanup(temporary.cleanup)
        self.core = LocalCore(Path(temporary.name) / "local.sqlite3")
        self.comp = Competition(self.core)
        self.core.setup_admin("admin", "synthetic-page-password")
        self.admin = self.core.login("admin", "synthetic-page-password")
        outer = self
        class DisplayCore:
            # Enable the PostgreSQL-only action while actual page data stays
            # in disposable SQLite. Its measurement callback is patched below.
            is_postgres = True
            def __getattr__(self, name):
                return getattr(outer.core, name)
        self.display_core = DisplayCore()

    def app(self, token):
        self.enterContext(patch("roly.ui.context", side_effect=lambda: (
            self.display_core, self.comp, token, self.core.session(token))))
        path = Path(__file__).resolve().parents[1] / "app_pages/admin.py"
        app = AppTest.from_file(str(path), default_timeout=15)
        app.session_state.admin_active_tab = "운영 계정"
        return app

    def test_actual_admin_page_only_runs_on_click_and_revocation_hides_action(self):
        with patch.object(probe, "_guard", probe.ProbeGuard()):
            fixed_report = probe.measure_db(Core(), "synthetic-admin")
        with patch("roly.db_probe_ui.measure_db", return_value=fixed_report) as execute:
            app = self.app(self.admin).run()
            self.assertFalse(app.exception); execute.assert_not_called()
            self.assertFalse(any(button.key == "admin_db_probe_run" for button in app.button))
            app.session_state.admin_db_probe_panel = True
            app.run(); self.assertFalse(app.exception); execute.assert_not_called()
            app.run(); execute.assert_not_called()
            app.button(key="admin_db_probe_run").click().run()
            self.assertFalse(app.exception); execute.assert_called_once_with(self.display_core, self.admin)
            self.assertEqual(json.loads(app.code[0].value)["requested_samples"], 3)
            app.run(); execute.assert_called_once()
            self.core.logout(self.admin)
            app.run(); self.assertFalse(app.exception); execute.assert_called_once()
            self.assertFalse(any(button.key == "admin_db_probe_run" for button in app.button))
            self.assertFalse(app.code)

    def test_actual_unauthenticated_page_never_calls_probe(self):
        with patch("roly.db_probe_ui.measure_db") as execute:
            app = self.app(None)
            app.session_state.admin_db_probe_panel = True
            app.run()
            self.assertFalse(app.exception); execute.assert_not_called()
            self.assertFalse(any(button.key == "admin_db_probe_run" for button in app.button))

    def test_actual_member_page_never_calls_probe(self):
        receipt = self.core.register_member("member", "synthetic-member-password", "Member#QA", "TOP", "JG", request_key=str(uuid4()))
        self.core.approve_member(self.admin, receipt["member_id"], 100)
        token = self.core.login("member", "synthetic-member-password")
        with patch("roly.db_probe_ui.measure_db") as execute:
            app = self.app(token)
            app.session_state.admin_db_probe_panel = True
            app.run()
            self.assertFalse(app.exception); execute.assert_not_called()
            self.assertFalse(any(button.key == "admin_db_probe_run" for button in app.button))


if __name__ == "__main__":
    unittest.main()
