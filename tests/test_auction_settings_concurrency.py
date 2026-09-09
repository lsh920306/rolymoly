"""Settings dialogs cannot overwrite a competing save, even at the same clock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction


class AuctionSettingsConcurrencyTests(unittest.TestCase):
    def test_two_dialogs_save_once_and_stale_form_cannot_reset_time(self):
        with tempfile.TemporaryDirectory(prefix="roly-setup-cas-") as folder:
            core = Core(Path(folder) / "test.sqlite3")
            core.setup_admin("admin", "synthetic-admin-password")
            token = core.login("admin", "synthetic-admin-password")
            competition = Competition(core)
            ids = []
            for index in range(20):
                mid = core.join_member(f"SetupCAS{index}#QA", ROLES[index % 5], ROLES[(index + 1) % 5])
                core.approve_member(token, mid, 100)
                ids.append(mid)
            event_id = competition.create_auction(token, ids, ids[::5])
            with core.transaction() as db:
                db.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (event_id,))
            live = LiveAuction(core, competition, clock=lambda: 2_000_000_000.0)
            version = (competition.get_event(event_id)["roster_token"], None)
            gate = Barrier(2)
            def save(seconds):
                gate.wait(timeout=10)
                try:
                    live.configure(token, event_id, bid_seconds=seconds, expected_settings=version)
                    return "saved"
                except ValueError:
                    return "stale"
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(save, (10, 30)))
            self.assertCountEqual(results, ["saved", "stale"])
            before = live.get_state(event_id)
            saved_order = [lot["member_id"] for lot in before["lots"]]
            version = (competition.get_event(event_id)["roster_token"], before["updated_at"])
            live.configure(token, event_id, bid_seconds=5, expected_settings=version)
            with self.assertRaisesRegex(ValueError, "설정이 변경"):
                live.configure(token, event_id, bid_seconds=20, expected_settings=version)
            after = live.get_state(event_id)
            self.assertEqual(after["bid_seconds"], 5)
            self.assertGreater(after["updated_at"], before["updated_at"])
            self.assertEqual([lot["member_id"] for lot in after["lots"]], saved_order)
