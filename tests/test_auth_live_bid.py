"""Real bids must progress while personal-account password work is suspended."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from roly import auth
from roly.core import Core, ROLES
from roly.competition import Competition
from roly.live_auction import LiveAuction


class AuthLiveBidTests(unittest.TestCase):
    def test_personal_captain_is_event_scoped_and_bid_does_not_wait_for_password_work(self):
        with tempfile.TemporaryDirectory(prefix="roly-auth-live-") as directory:
            core = Core(Path(directory) / "isolated.sqlite3")
            core.setup_admin("admin", "admin-password-123")
            admin = core.login("admin", "admin-password-123")
            ids = []
            for i in range(20):
                result = core.register_member(f"member{i}", "member-password-123", f"Mixed{i}#KR1",
                    ROLES[i % 5], ROLES[(i + 1) % 5], request_key=str(uuid.uuid4()))
                core.approve_member(admin, result["member_id"], 100)
                ids.append(result["member_id"])
            first_captain = core.login("member0", "member-password-123")
            second_captain = core.login("member1", "member-password-123")
            actor_before = core.session(first_captain)
            comp = Competition(core)
            live = LiveAuction(core, comp, clock=lambda: 2_000_000_000.0)
            events = [comp.create_auction(admin, ids, ids[offset::5]) for offset in (0, 1)]
            with core.transaction() as db:
                for event in events:
                    db.execute("UPDATE competition_events SET status='AUCTION_READY' WHERE id=?", (event,))
            lots = []
            for event in events:
                live.configure(admin, event)
                lots.append(live.start(admin, event)["current_lot"])
            try:
                with self.assertRaises(PermissionError):
                    live.place_bid(first_captain, events[1], lots[1]["id"], 1, str(uuid.uuid4()))
                live.place_bid(second_captain, events[1], lots[1]["id"], 1, str(uuid.uuid4()))
                real_digest = auth.password_digest
                for amount, target, operation in (
                    (1, "roly.core.password_digest", lambda: core.login("member1", "member-password-123")),
                    (2, "roly.auth.password_digest", lambda: core.register_member("newmember", "new-member-password", "NewMixed#KR1", "TOP", "JG", request_key=str(uuid.uuid4()))),
                ):
                    entered, release = threading.Event(), threading.Event()
                    def suspended_digest(password, salt):
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError("password work was never released")
                        return real_digest(password, salt)
                    with self.subTest(target=target), patch(target, side_effect=suspended_digest), ThreadPoolExecutor(max_workers=2) as pool:
                        password_task = pool.submit(operation)
                        try:
                            self.assertTrue(entered.wait(10), "password work did not start")
                            bid_task = pool.submit(live.place_bid, first_captain, events[0], lots[0]["id"], amount, str(uuid.uuid4()))
                            # Timeouts only bound a deadlock. The assertion is
                            # causal: the bid commits before hashing is released.
                            receipt = bid_task.result(timeout=10)
                            self.assertEqual(receipt["amount"], amount)
                            self.assertFalse(release.is_set())
                            self.assertFalse(password_task.done())
                        finally:
                            release.set()
                        password_task.result(timeout=10)
                actor_after = core.session(first_captain)
                self.assertEqual((actor_after["id"], actor_after["member_id"], actor_after["role"]),
                                 (actor_before["id"], ids[0], "member"))
                self.assertEqual(len(live.get_state(events[0])["bids"]), 2)
                self.assertEqual(len(live.get_state(events[1])["bids"]), 1)
            finally:
                live.stop_worker()


if __name__ == "__main__":
    unittest.main()
