"""Exercise the full disposable workflow and reject unsafe CLI targets."""
from contextlib import redirect_stdout
import io
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import rehearse_member_normal as rehearsal


class MemberNormalRehearsalTests(unittest.TestCase):
    def test_operational_schema_is_rejected_before_service_initialization(self):
        core = SimpleNamespace(is_postgres=True, schema="rolymoly")
        with patch.object(rehearsal, "Competition") as competition:
            with self.assertRaises(ValueError):
                rehearsal.exercise(core, rehearsal.Report("postgresql"))
        competition.assert_not_called()

    def test_invalid_generated_target_never_connects_or_attempts_cleanup(self):
        cleanup, output = Mock(), io.StringIO()
        with patch.dict(sys.modules, {"roly.postgres": SimpleNamespace(drop_qa_schema=cleanup)}):
            with patch.object(rehearsal, "new_qa_schema", return_value="rolymoly"):
                with patch.object(rehearsal, "Core") as core, redirect_stdout(output):
                    code = rehearsal.main(["--remote"])
        self.assertEqual(code, 1)
        core.assert_not_called()
        cleanup.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_driver_failure_reports_only_safe_identifiers_and_cleans_owned_schema(self):
        schema, cleanup, output = "rolymoly_qa_" + "b" * 32, Mock(), io.StringIO()
        with patch.dict(sys.modules, {"roly.postgres": SimpleNamespace(drop_qa_schema=cleanup)}):
            with patch.object(rehearsal, "new_qa_schema", return_value=schema):
                with patch.object(rehearsal, "Core", side_effect=RuntimeError("synthetic-private-password-driver-detail")) as core:
                    with redirect_stdout(output):
                        code = rehearsal.main(["--remote"])
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-private", output.getvalue())
        core.assert_called_once_with("supabase://" + schema)
        cleanup.assert_called_once_with(schema)
        document = json.loads(output.getvalue())
        self.assertEqual(document["error_type"], "RuntimeError")
        self.assertTrue(document["cleaned_up"])
        self.assertTrue(all(set(frame) == {"file", "line", "function"} for frame in document["failure_location"]))

    def test_full_member_and_normal_workflow_uses_only_disposable_sqlite(self):
        output = io.StringIO()
        with patch("roly.storage_config._runtime_document", side_effect=AssertionError("Real settings must not be read")):
            with redirect_stdout(output):
                code = rehearsal.main(["--sqlite"])
        document = json.loads(output.getvalue())
        self.assertEqual(code, 0, document)
        self.assertTrue(document["ok"] and document["cleaned_up"])
        counts = document["counts"]
        for key, expected in {"accounts": 24, "members": 22, "pending_members": 1,
                              "replaced_rosters": 2, "profile_race_winners": 1,
                              "manual_adjustment": 7, "concurrent_adjustment_requests": 2,
                              "manual_ledger_entries": 1, "adjustment_payload_rejections": 4,
                              "corrected_event_games": 1, "completed_normal_events": 3,
                              "total_games": 10, "confirmed_games": 9, "normal_awards": 0,
                              "voided_standalone_games": 1, "rerecorded_standalone_games": 1,
                              "restored_member_ids": 1, "reset_consumptions": 1}.items():
            self.assertEqual(counts[key], expected, key)
        self.assertIn("final_shared_ledger_consistency", document["passed"])
        self.assertIn("profile_rank_edit_identity_and_historical_snapshots", document["passed"])


if __name__ == "__main__":
    unittest.main()
