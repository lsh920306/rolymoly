"""Actual subprocess recovery, with no app process or operating data touched."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from scripts import rehearse_recovery as rehearsal


class RecoveryRehearsalTests(unittest.TestCase):
    def test_operating_targets_are_rejected_before_connecting(self):
        for target in ("supabase://rolymoly", "supabase://public", "supabase://rolymoly_qa_bad", ":memory:", None):
            with self.subTest(target=target), self.assertRaises((ValueError, FileNotFoundError)):
                rehearsal.validate_target(target)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.sqlite3"
            path.touch()
            with self.assertRaises(ValueError):
                rehearsal.validate_target(str(path))

    def test_actual_process_restart_pause_and_two_worker_settlement(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            output = Path(directory) / "recovery.json"
            code = rehearsal.main(["--sqlite", "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 0, {key: value for key, value in report.items()
                if key not in ("source_before_sha256", "source_sha256")})
        self.assertTrue(report["ok"])
        self.assertTrue(report["cleaned_up"])
        self.assertTrue(report["worker_processes_stopped"])
        self.assertTrue(report["source_unchanged"])
        self.assertFalse(report["mocked_clock"])
        self.assertFalse(report["operating_user_data_accessed"])
        self.assertEqual(report["counts"]["paid_sales"], 2)
        self.assertEqual(report["counts"]["duplicate_sales"], 0)
        self.assertEqual(report["counts"]["total_spent"], 30)
        self.assertEqual(report["counts"]["simultaneous_workers"], 2)
        self.assertEqual(report["counts"]["parent_settle_due_calls"], 0)
        self.assertGreaterEqual(report["counts"]["observed_transition_seconds"], 3)


if __name__ == "__main__":
    unittest.main()
