"""Explicit demo snapshot preparation; every HTTP request is synthetic."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from roly.core import Core, identity
from roly.riot_api import RiotConfig
from scripts.prepare_demo_riot import main, prepare_profiles, publish_snapshot, write_json
from scripts.rehearse_postgres import Clock
from scripts.rehearse_riot import FakeHTTP


class DemoRiotSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="roly-demo-snapshot-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.rate = Core(self.path / "shared-rate.sqlite3")
        self.work = self.path / "temporary-jobs"
        self.work.mkdir()
        self.clock = Clock()
        self.http = FakeHTTP()
        self.config = RiotConfig("synthetic-demo-snapshot-key", allow_demo=True)
        for item in (
            patch("roly.riot_api._http_get", side_effect=AssertionError("Unexpected outbound HTTP")),
            patch("roly.riot_sync._static_data", return_value=("16.18.1", {
                "22": {"name": "애쉬", "image": "https://ddragon.leagueoflegends.com/cdn/16.18.1/img/champion/Ashe.png"}})),
        ):
            item.start()
            self.addCleanup(item.stop)

    def prepare(self, names, **kwargs):
        def sleep(seconds):
            self.clock.value += seconds
        return prepare_profiles(names, self.config, self.rate, client_factory=self.http.factory,
            clock=self.clock, monotonic=self.clock, sleep=sleep, temporary_parent=self.work, **kwargs)

    def test_twenty_two_public_profiles_use_only_shared_rate_rows_and_delete_private_database(self):
        names = [f"SyntheticDemo{index}#QA" for index in range(22)]
        progress = []
        snapshot, report = self.prepare(names, progress=progress.append)
        self.assertTrue(report["ok"] and report["temporary_database_deleted"])
        self.assertEqual((report["succeeded"], report["failed"], report["processed_stages"] >= 88), (22, 0, True))
        self.assertEqual(set(snapshot["profiles"]), {identity(name)[1] for name in names})
        self.assertEqual(snapshot["errors"], {})
        self.assertEqual(len(list(self.work.iterdir())), 0)
        with self.rate.read_snapshot() as db:
            for table in ("members", "accounts", "sessions", "riot_jobs", "riot_profiles", "score_ledger"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM riot_rate_hits").fetchone()[0], 88)
        self.assertEqual(self.http.stage_count("account"), 22)
        public_text = json.dumps(snapshot)
        for private in ("puuid", "partial_payload", "key_hash", self.config.api_key, "lease_id"):
            self.assertNotIn(private, public_text)
        for profile in snapshot["profiles"].values():
            self.assertEqual((profile["current_tier"], profile["lp"]), ("골드 2", 37))
            self.assertEqual((profile["flex_current_tier"], profile["flex_lp"], profile["flex_rank_wins"], profile["flex_rank_losses"]),
                             ("플래티넘 3", 64, 12, 7))
            self.assertEqual(profile["champions"][0]["points"], 123456)
            self.assertEqual([champion["points"] for champion in profile["champions"]], [123456, 54321, 12000, 9000, 7000])
        self.assertEqual(self.http.stage_count("rank"), 22)
        for message in progress:
            self.assertEqual(set(message), {"code", "total", "succeeded", "failed", "pending"})

    def test_not_found_does_not_stop_other_members_and_429_preserves_work_until_retry(self):
        self.http.failures = {"account": [(404, None)], "rank": [(429, 2)]}
        started = self.clock.value
        snapshot, report = self.prepare(["Missing#QA", "PresentOne#QA", "PresentTwo#QA"])
        self.assertEqual(snapshot["errors"], {"missing#qa": "not_found"})
        self.assertEqual(len(snapshot["profiles"]), 2)
        self.assertEqual(self.http.stage_count("account"), 3)
        self.assertEqual(self.http.stage_count("rank"), 3)
        self.assertGreaterEqual(self.clock.value - started, 2)
        self.assertEqual(report["error_counts"], {"not_found": 1})
        self.assertTrue(report["temporary_database_deleted"])

    def test_long_retry_after_stops_at_time_limit_without_ignoring_shared_cooldown(self):
        self.http.failures = {"rank": [(429, 600)]}
        started = self.clock.value
        snapshot, report = self.prepare(["Limited#QA"], max_seconds=3)
        self.assertEqual(snapshot["errors"], {"limited#qa": "time_limit"})
        self.assertEqual(self.clock.value - started, 3)
        self.assertEqual(self.http.stage_count("rank"), 1)
        with self.rate.read_snapshot() as db:
            row = db.execute("SELECT until_at FROM riot_rate_cooldowns").fetchone()
            self.assertEqual(row[0], started + 600)
        self.assertTrue(report["temporary_database_deleted"])

    def test_bad_key_finishes_all_pending_with_static_auth_code_without_retry_loop(self):
        self.http.failures = {"account": [(403, None)]}
        snapshot, report = self.prepare(["First#QA", "Second#QA"])
        self.assertEqual(snapshot["errors"], {"first#qa": "auth", "second#qa": "auth"})
        self.assertEqual(sum(self.http.calls.values()), 1)
        self.assertEqual(report["error_counts"], {"auth": 2})
        self.assertTrue(report["temporary_database_deleted"])

    def test_default_opt_out_rejects_before_operating_database_or_temporary_files(self):
        with patch("scripts.prepare_demo_riot.load_riot_config", return_value=RiotConfig("synthetic-not-allowed")), \
             patch("scripts.prepare_demo_riot.Core") as core, \
             patch("scripts.prepare_demo_riot.write_json"), redirect_stdout(StringIO()) as output:
            self.assertEqual(main([]), 2)
        core.assert_not_called()
        self.assertNotIn("synthetic-not-allowed", output.getvalue())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_all_auth_failed_refresh_keeps_original_snapshot_bytes(self):
        path = self.path / "snapshot.json"
        old = {"generated_at": "2026-09-08T00:00:00+00:00", "profiles": {
            "first#qa": {"current_tier": "실버 2", "lp": 10, "champions": [], "updated_at": "2026-09-08T00:00:00+00:00"}}, "errors": {}}
        write_json(path, old)
        before = path.read_bytes()
        self.http.failures = {"account": [(401, None)]}
        fresh, preparation = self.prepare(["First#QA"])
        self.assertEqual(preparation["error_counts"], {"auth": 1})
        publication = publish_snapshot(path, fresh, ["First#QA"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(publication["snapshot_written"])
        self.assertEqual((publication["retained_profiles"], publication["updated_profiles"]), (1, 0))
        empty_path = self.path / "never-published.json"
        self.assertFalse(publish_snapshot(empty_path, fresh, ["First#QA"])["snapshot_written"])
        self.assertFalse(empty_path.exists())

    def test_partial_failure_keeps_previous_profile_and_replaces_only_new_success(self):
        path = self.path / "snapshot.json"
        original_time = "2026-09-08T00:00:00+00:00"
        old_profile = {"current_tier": "실버 2", "lp": 10, "champions": [], "updated_at": original_time}
        write_json(path, {"generated_at": original_time, "profiles": {
            "missing#qa": old_profile, "present#qa": old_profile, "outside#qa": old_profile},
            "errors": {"present#qa": "not_found", "outside#qa": "not_found"}})
        self.http.failures = {"account": [(404, None)]}
        fresh, _ = self.prepare(["Missing#QA", "Present#QA"])
        publication = publish_snapshot(path, fresh, ["Missing#QA", "Present#QA"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["profiles"]), {"missing#qa", "present#qa"})
        self.assertEqual(stored["profiles"]["missing#qa"]["current_tier"], "실버 2")
        self.assertEqual(stored["profiles"]["missing#qa"]["updated_at"], original_time)
        self.assertEqual(stored["profiles"]["present#qa"]["current_tier"], "골드 2")
        self.assertEqual(stored["errors"], {"missing#qa": "not_found"})
        self.assertEqual((publication["retained_profiles"], publication["updated_profiles"]), (1, 1))
        first_path = self.path / "first-partial.json"
        first = publish_snapshot(first_path, fresh, ["Missing#QA", "Present#QA"])
        self.assertEqual((first["retained_profiles"], first["updated_profiles"]), (0, 1))
        self.assertEqual(set(json.loads(first_path.read_text(encoding="utf-8"))["profiles"]), {"present#qa"})


if __name__ == "__main__":
    unittest.main()
