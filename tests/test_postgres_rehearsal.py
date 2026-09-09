"""The remote rehearsal cannot target production or reveal driver details."""
from contextlib import redirect_stdout
import io
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import rehearse_postgres


class PostgresRehearsalTests(unittest.TestCase):
    def test_generated_schema_is_private_and_unsafe_targets_are_rejected(self):
        first, second = rehearse_postgres.new_qa_schema(), rehearse_postgres.new_qa_schema()
        self.assertNotEqual(first, second)
        rehearse_postgres.require_qa_schema(first)
        for schema in ("rolymoly", "public", "rolymoly_qa_", "rolymoly_qa_other",
                       "rolymoly_qa_" + "a" * 32 + "; DROP SCHEMA public", None):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                rehearse_postgres.require_qa_schema(schema)

    def test_exercise_rejects_operational_schema_before_service_calls(self):
        core = SimpleNamespace(is_postgres=True, schema="rolymoly")
        with patch.object(rehearse_postgres, "Competition") as competition:
            with self.assertRaises(ValueError):
                rehearse_postgres.exercise(core, rehearse_postgres.Report("postgresql"))
        competition.assert_not_called()

    def test_remote_driver_failure_is_sanitized_and_only_owned_schema_is_cleaned(self):
        schema = "rolymoly_qa_" + "a" * 32
        cleanup = Mock()
        backend = SimpleNamespace(drop_qa_schema=cleanup)
        output = io.StringIO()
        with patch.dict(sys.modules, {"roly.postgres": backend}):
            with patch.object(rehearse_postgres, "new_qa_schema", return_value=schema):
                with patch.object(rehearse_postgres, "Core",
                                  side_effect=RuntimeError("synthetic-private-driver-secret")) as core:
                    with redirect_stdout(output):
                        status = rehearse_postgres.main(["--remote"])
        self.assertEqual(status, 1)
        self.assertNotIn("synthetic-private-driver-secret", output.getvalue())
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertTrue(report["cleaned_up"])
        self.assertEqual(report["failed_check"], "initialize")
        core.assert_called_once_with("supabase://" + schema)
        cleanup.assert_called_once_with(schema)

    def test_invalid_generated_target_never_connects_or_drops_schema(self):
        cleanup = Mock()
        output = io.StringIO()
        with patch.dict(sys.modules, {"roly.postgres": SimpleNamespace(drop_qa_schema=cleanup)}):
            with patch.object(rehearse_postgres, "new_qa_schema", return_value="rolymoly"):
                with patch.object(rehearse_postgres, "Core") as core:
                    with redirect_stdout(output):
                        status = rehearse_postgres.main(["--remote"])
        self.assertEqual(status, 1)
        core.assert_not_called()
        cleanup.assert_not_called()

    def test_complete_workflow_runs_on_disposable_sqlite(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = rehearse_postgres.main(["--sqlite"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0, report)
        self.assertTrue(report["cleaned_up"])
        self.assertEqual(report["counts"]["accounts"], 21)
        self.assertEqual(report["counts"]["pending_logins"], 20)
        self.assertEqual(report["counts"]["approved_personal_accounts"], 20)
        self.assertEqual(report["counts"]["distinct_member_hosts"], 2)
        self.assertEqual(report["counts"]["noncaptain_hosts"], 2)
        self.assertEqual(report["counts"]["persisted_random_lots"], 16)
        self.assertEqual(report["counts"]["concurrent_bid_accepts"], 1)
        self.assertEqual(report["counts"]["auction_sales"], 16)
        self.assertEqual(report["counts"]["total_games"], 4)
        self.assertEqual(report["counts"]["award_recipients"], 5)
        self.assertIn("normal_result_correction_preserves_ledger", report["passed"])
        self.assertIn("pending_permissions_and_approval_reuse_personal_accounts", report["passed"])


if __name__ == "__main__":
    unittest.main()
