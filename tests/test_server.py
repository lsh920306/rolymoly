"""Launcher recovery before the Streamlit child or any browser is started."""
from contextlib import chdir, closing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.tournament import TournamentService
from roly import server


class ServerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roly-server-test-")
        self.addCleanup(self.temp.cleanup)
        self.core = Core(Path(self.temp.name) / "rolymoly.sqlite3")
        self.comp = Competition(self.core)
        self.core.setup_admin("admin", "server-test-password")
        self.admin = self.core.login("admin", "server-test-password")
        ids = []
        for index in range(20):
            member_id = self.core.join_member(f"Recovery{index}#KR1", ROLES[index % 5], ROLES[(index + 1) % 5])
            self.core.approve_member(self.admin, member_id, 100)
            ids.append(member_id)
        self.event_id = self.comp.create_auction(self.admin, ids, ids[::5])
        TournamentService(self.core, self.comp).upgrade_auction(self.admin, self.event_id)
        self.core.create_account(self.admin, "captain", "captain-test-password", role="member", member_id=ids[0])
        token = self.core.login("captain", "captain-test-password")
        # Persist a valid bid and an elapsed deadline, as if the process stopped
        # before closing. No browser/AppTest or worker has run in this fixture.
        earlier = time.time() - 10
        self.live = LiveAuction(self.core, self.comp, clock=lambda: earlier)
        self.addCleanup(self.live.stop_worker)
        self.live.configure(self.admin, self.event_id, bid_seconds=5)
        lot = self.live.start(self.admin, self.event_id)["current_lot"]
        self.lot_id = lot["id"]
        receipt = self.live.place_bid(token, self.event_id, self.lot_id, 5, str(uuid4()))
        self.assertEqual(receipt["closes_at"], min(lot["closes_at"] + 5, earlier + 5))
        # The persisted deadline is elapsed even after the mandatory extension.
        self.assertLess(receipt["closes_at"], time.time())
        self.assertEqual(self.live.get_state(self.event_id)["current_lot"]["status"], "OPEN")
        environment = patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        argv = patch("sys.argv", ["roly.server", "--server.headless=true"])
        argv.start()
        self.addCleanup(argv.stop)

    def assert_worker_stopped(self):
        worker = LiveAuction._workers.get(self.core.db_path)
        self.assertTrue(worker is None or not worker["thread"].is_alive())

    def test_restart_settles_persisted_bid_before_any_browser_and_returns_child_code(self):
        def fake_streamlit_process(command, cwd, env=None):
            self.assertEqual(command[1:4], ["-m", "streamlit", "run"])
            self.assertEqual(Path(command[4]).name, "streamlit_app.py")
            self.assertEqual(command[5:], ["--server.headless=true"])
            self.assertEqual(Path(cwd), Path(__file__).resolve().parents[1])
            worker = LiveAuction._workers[self.core.db_path]["thread"]
            self.assertTrue(worker.is_alive())
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                with closing(self.core.connect()) as db:
                    state = db.execute("SELECT status FROM live_lots WHERE id=?", (self.lot_id,)).fetchone()[0]
                if state == "SOLD":
                    break
                time.sleep(.01)
            self.assertEqual(state, "SOLD")
            with closing(self.core.connect()) as db:
                sold = db.execute("SELECT COUNT(*) FROM live_events WHERE event_id=? AND type='SOLD'", (self.event_id,)).fetchone()[0]
                price = db.execute("SELECT price FROM competition_players WHERE event_id=? AND member_id=(SELECT member_id FROM live_lots WHERE id=?)", (self.event_id, self.lot_id)).fetchone()[0]
            self.assertEqual((sold, price), (1, 5))
            return 37

        with patch.object(server.subprocess, "call", side_effect=fake_streamlit_process) as child:
            self.assertEqual(server.main(), 37)
            child.assert_called_once()
        self.assert_worker_stopped()
        self.assertEqual(self.core.list_games(), [])

    def test_keyboard_interrupt_stops_background_worker(self):
        with patch.object(server.subprocess, "call", side_effect=KeyboardInterrupt):
            self.assertEqual(server.main(), 0)
        self.assert_worker_stopped()

    def test_child_start_failure_stops_worker_and_is_not_hidden(self):
        with patch.object(server.subprocess, "call", side_effect=OSError("child could not start")):
            with self.assertRaisesRegex(OSError, "child could not start"):
                server.main()
        self.assert_worker_stopped()


class ServerDataPathTests(unittest.TestCase):
    def test_relative_data_directory_is_shared_by_launcher_and_streamlit_child(self):
        # A service/shortcut may launch from a different working directory.
        # Streamlit itself always starts in the project directory.
        with tempfile.TemporaryDirectory(prefix="roly-server-path-") as directory:
            with chdir(directory), patch.dict(os.environ, {"ROLYMOLY_DATA_DIR": "storage"}):
                expected = Path(directory) / "storage" / "rolymoly.sqlite3"

                def child(command, cwd, env=None):
                    child_environment = env if env is not None else os.environ
                    child_root = Path(cwd) / child_environment["ROLYMOLY_DATA_DIR"]
                    self.assertEqual((child_root / "rolymoly.sqlite3").resolve(), expected.resolve())
                    self.assertTrue(expected.is_file())
                    self.assertIn(str(expected.resolve()), LiveAuction._workers)
                    return 0

                with patch.object(server.subprocess, "call", side_effect=child):
                    self.assertEqual(server.main(), 0)
                self.assertEqual(os.environ["ROLYMOLY_DATA_DIR"], "storage")


if __name__ == "__main__":
    unittest.main()
