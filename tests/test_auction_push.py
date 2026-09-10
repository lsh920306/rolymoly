"""Shared push/HTTP behavior with isolated SQLite and revocable real sessions."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from roly import auction_http as http
from roly.auction_push import AuctionHub, Subscription
from roly.auction_state import read_snapshot
from tests import test_auction_http as http_fixtures

call_asgi = http_fixtures.call_asgi


def add_viewer_sessions(core, source_token, count):
    """Clone only synthetic fixture rows; create distinct approved members/accounts.

    Password material is reused from the test fixture, avoiding 80 login/hash
    operations. This measures already authenticated viewing, not login capacity.
    """
    source_actor = core.session(source_token)
    tokens = []
    with core.transaction() as db:
        member = dict(db.execute("SELECT * FROM members WHERE id=?", (source_actor["member_id"],)).fetchone())
        account = dict(db.execute("SELECT * FROM accounts WHERE id=?", (source_actor["id"],)).fetchone())
        member.pop("id")
        account.pop("id")
        for index in range(count):
            suffix = uuid4().hex
            person = {**member, "riot_id": f"PushViewer{index}{suffix[:8]}#KR1", "canonical_id": f"pushviewer{index}{suffix[:8]}#kr1"}
            keys = list(person)
            member_id = db.execute("INSERT INTO members(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(person.values())).lastrowid
            actor = {**account, "username": "push-" + suffix, "display_name": f"Push viewer {index}", "member_id": member_id}
            keys = list(actor)
            account_id = db.execute("INSERT INTO accounts(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(actor.values())).lastrowid
            token = "synthetic-viewer-" + uuid4().hex
            now = datetime.now(timezone.utc)
            db.execute("INSERT INTO sessions(token_hash,account_id,expires_at,created_at) VALUES(?,?,?,?)",
                       (sha256(token.encode()).hexdigest(), account_id, (now+timedelta(hours=1)).isoformat(), now.isoformat()))
            tokens.append(token)
    return tokens


class WebSocketProbe:
    def __init__(self, app, headers):
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()
        self.scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "wss",
            "path": "/api/auction/ws", "raw_path": b"/api/auction/ws", "query_string": b"",
            "root_path": "", "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "server": ("auction.test", 443), "client": ("127.0.0.1", 1), "subprotocols": []}
        self.app = app
        self.task = None

    async def open(self, body):
        self.task = asyncio.create_task(self.app(self.scope, self.input.get, self.output.put))
        await self.input.put({"type": "websocket.connect"})
        accepted = await asyncio.wait_for(self.output.get(), 2)
        if accepted["type"] != "websocket.accept":
            return accepted
        await self.input.put({"type": "websocket.receive", "text": json.dumps(body)})
        return accepted

    async def packet(self):
        message = await asyncio.wait_for(self.output.get(), 2)
        return json.loads(message["text"]) if message["type"] == "websocket.send" else message

    async def close(self):
        await self.input.put({"type": "websocket.disconnect", "code": 1000})
        if self.task:
            await asyncio.wait_for(self.task, 2)


class AuctionPushTests(unittest.TestCase):
    setUpClass = classmethod(http_fixtures.AuctionHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(http_fixtures.AuctionHTTPTests.tearDownClass.__func__)
    setUp = http_fixtures.AuctionHTTPTests.setUp
    start = http_fixtures.AuctionHTTPTests.start
    envelope = http_fixtures.AuctionHTTPTests.envelope
    headers = http_fixtures.AuctionHTTPTests.headers

    def run_hub(self, exercise, **options):
        options.setdefault("scan_interval", .04)
        async def run():
            self.runtime.push = hub = AuctionHub(self.runtime, listen=False, **options)
            try:
                await exercise(hub)
            finally:
                await hub.close()
        asyncio.run(run())

    @staticmethod
    async def frame(member):
        return await asyncio.wait_for(member.queue.get(), 2)

    def test_shared_stage_formats_once_per_batch_without_duplicate_wire_profile(self):
        from roly.auction_live_panel import stage_data
        lot = self.start()

        async def run():
            gate = asyncio.Event()

            class GatedHub(AuctionHub):
                async def _run(self, room):
                    await gate.wait()
                    await super()._run(room)

            self.runtime.push = hub = GatedHub(self.runtime, scan_interval=60, listen=False)
            try:
                tokens = (self.tokens[0], self.tokens[1], self.player_token)
                pairs = [await hub.subscribe(token, self.event, self.envelope(token=token)) for token in tokens]
                with patch("roly.auction_live_panel.stage_data", wraps=stage_data) as formatted:
                    gate.set()
                    frames = await asyncio.gather(*(self.frame(member) for _, member in pairs))
                    self.assertEqual(formatted.call_count, 1)
                    first_stage = frames[0]["panel"]["stage"]
                    self.assertTrue(all(frame["panel"]["stage"] == first_stage for frame in frames))
                    for frame, (_, member) in zip(frames, pairs):
                        panel = frame["panel"]
                        self.assertNotIn("player", panel)
                        self.assertEqual(panel["stage"]["player"], first_stage["player"])
                        self.assertEqual(panel["transport"]["context"], member.context)
                        if panel["control"]:
                            self.assertEqual(panel["control"]["context"], member.context)
                        compact = len(json.dumps(panel, ensure_ascii=False).encode())
                        duplicated = len(json.dumps({**panel["stage"], **panel}, ensure_ascii=False).encode())
                        self.assertLess(compact, duplicated)
                    self.assertIsNone(frames[2]["panel"]["control"])
                    self.assertNotEqual(frames[0]["panel"]["control"]["team_name"], frames[1]["panel"]["control"]["team_name"])
                    self.assertIsNot(frames[0]["panel"]["transport"], frames[1]["panel"]["transport"])

                    self.live.place_bid(self.tokens[0], self.event, lot["id"], 10, str(uuid4()))
                    hub.notify(self.event)
                    updates = await asyncio.gather(*(self.frame(member) for _, member in pairs))
                    self.assertEqual(formatted.call_count, 2)
                    for old, update in zip(frames, updates):
                        self.assertGreater(update["revision"], old["revision"])
                        self.assertEqual(update["panel"]["lot"]["highest_bid"], 10)
                        self.assertEqual(update["panel"]["stage"]["price"], "최고 입찰 10 P")
                        self.assertEqual(update["panel"]["stage"]["player"], first_stage["player"])
            finally:
                gate.set()
                await hub.close()

        asyncio.run(run())

    def test_unchanged_probe_and_revocation_do_not_copy_or_format_display(self):
        self.start()

        async def exercise(hub):
            room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            await self.frame(member)
            with patch.object(hub, "_fresh_state", side_effect=AssertionError("unchanged/auth-only read copied display")), \
                    patch("roly.auction_live_panel.stage_data", side_effect=AssertionError("unchanged/auth-only read formatted stage")):
                prior_reads = hub.stats["reads"]
                hub.notify(self.event)

                async def wait_for_read():
                    while hub.stats["reads"] == prior_reads:
                        await asyncio.sleep(.001)

                await asyncio.wait_for(wait_for_read(), 2)
                self.assertTrue(member.queue.empty())
                self.core.logout(self.player_token)
                hub.notify(self.event)
                frame = await self.frame(member)
                self.assertEqual(frame["error"]["code"], "session_expired")
                self.assertNotIn("panel", frame)
                self.assertIsNone(member.actor)

        self.run_hub(exercise, scan_interval=60)

    def test_eighty_distinct_viewers_and_http_snapshot_share_one_initial_read(self):
        self.start()
        tokens = add_viewer_sessions(self.core, self.player_token, 80)
        async def run():
            gate = asyncio.Event()
            class GatedHub(AuctionHub):
                async def _run(self, room):
                    await gate.wait()
                    await super()._run(room)
            self.runtime.push = hub = GatedHub(self.runtime, scan_interval=60, listen=False)
            try:
                pairs = await asyncio.gather(*(hub.subscribe(token, self.event, self.envelope(token=token)) for token in tokens))
                http_task = asyncio.create_task(call_asgi(self.app, self.envelope(token=self.tokens[0]), self.headers(), "/api/auction/live"))
                while len(hub.rooms[self.event].members) < 81:
                    await asyncio.sleep(.001)
                gate.set()
                frames = await asyncio.gather(*(self.frame(member) for _, member in pairs))
                status, response, _ = await http_task
                self.assertEqual(status, 200)
                self.assertEqual(hub.stats["reads"], 1)
                self.assertEqual(hub.stats["full_states"], 1)
                self.assertEqual(len({member.actor["id"] for _, member in pairs}), 80)
                self.assertTrue(all(frame["type"] == "snapshot" and frame["panel"].get("control") is None for frame in frames))
                self.assertTrue(response["panel"]["control"]["can_bid"])
                serialized = json.dumps(frames)
                self.assertTrue(all(token not in serialized for token in tokens))
            finally:
                gate.set()
                await hub.close()
        asyncio.run(run())

    def test_revocation_and_expiry_revoke_subscriptions_on_next_read(self):
        self.start()
        tokens = add_viewer_sessions(self.core, self.player_token, 2)
        async def exercise(hub):
            pairs = [await hub.subscribe(token, self.event, self.envelope(token=token)) for token in tokens]
            await asyncio.gather(*(self.frame(member) for _, member in pairs))
            self.core.logout(tokens[0])
            with self.core.transaction() as db:
                db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", ("2000-01-01T00:00:00+00:00", sha256(tokens[1].encode()).hexdigest()))
            hub.notify(self.event)
            frames = await asyncio.gather(*(self.frame(member) for _, member in pairs))
            self.assertTrue(all(frame["error"]["code"] == "session_expired" for frame in frames))
            self.assertTrue(all(hub.pong(room, member, 1) is None for room, member in pairs))
        self.run_hub(exercise)

    def test_invalid_session_never_receives_a_snapshot(self):
        self.start()
        invalid = "synthetic-nonexistent-session-token"
        async def exercise(hub):
            room, member = await hub.subscribe(invalid, self.event, self.envelope(token=invalid))
            frame = await self.frame(member)
            self.assertEqual(frame["error"]["code"], "session_expired")
            self.assertNotIn("panel", frame)
        self.run_hub(exercise)

    def test_reconnect_gets_full_snapshot_after_bid_and_unnotified_worker_settlement(self):
        self.start()
        async def exercise(hub):
            room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            first = await self.frame(member)
            self.live.place_bid(self.tokens[0], self.event, self.live.get_state(self.event)["current_lot"]["id"], 10, str(uuid4()))
            hub.notify(self.event)
            update = await self.frame(member)
            self.assertGreater(update["revision"], first["revision"])
            hub.unsubscribe(room, member)
            _, resumed = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            restored = await self.frame(resumed)
            self.assertEqual(restored["type"], "snapshot")
            self.assertIn("teams", restored["panel"]["views"])
            self.clock.value = self.live.get_state(self.event)["current_lot"]["closes_at"]
            self.live.settle_due()  # Independent writer: no hub.notify call.
            settled = await self.frame(resumed)
            self.assertEqual(settled["panel"]["lot"]["status"], "SOLD")
            self.assertGreater(settled["revision"], restored["revision"])
        self.run_hub(exercise)

    def test_committed_bid_ack_does_not_wait_for_blocked_shared_reader(self):
        lot = self.start()
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Synthetic reader gate timed out")
            return read_snapshot(*args, **kwargs)
        async def exercise(hub):
            room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            while not entered.is_set():
                await asyncio.sleep(.001)
            try:
                status, response, _ = await asyncio.wait_for(call_asgi(self.app, self.envelope(lot), self.headers()), 1)
                self.assertEqual(status, 200)
                self.assertEqual(response["ack"]["status"], "accepted")
                self.assertFalse(release.is_set())
                with closing(self.core.connect()) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM live_bids").fetchone()[0], 1)
            finally:
                release.set()
            await self.frame(member)
        self.run_hub(exercise, reader=blocked)

    def test_notification_during_inflight_snapshot_survives_for_the_next_read(self):
        lot = self.start()
        entered, release = threading.Event(), threading.Event()
        hints = []
        def delayed(*args, **kwargs):
            result = read_snapshot(*args, **kwargs)
            hints.append(kwargs["changed_hint"])
            if len(hints) == 2:
                # The first bid snapshot is already fixed when the next bid
                # commits and notifies. Clearing a hint after await loses it.
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("Synthetic snapshot gate timed out")
            return result

        async def exercise(hub):
            room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            await self.frame(member)
            self.live.place_bid(self.tokens[0], self.event, lot["id"], 5, str(uuid4()))
            hub.notify(self.event)
            try:
                async def wait_for_snapshot():
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                await asyncio.wait_for(wait_for_snapshot(), 2)
                self.live.place_bid(self.tokens[1], self.event, lot["id"], 10, str(uuid4()))
                hub.notify(self.event)
                self.assertTrue(room.change_pending)
            finally:
                release.set()
            frame = await self.frame(member)
            if frame["panel"]["lot"]["highest_bid"] == 5:
                frame = await self.frame(member)
            self.assertEqual(frame["panel"]["lot"]["highest_bid"], 10)
            # The in-flight wake is preserved; its following read probes first
            # to distinguish a new bid from the same commit's delayed NOTIFY.
            self.assertEqual(hints, [False, True, False])
            self.assertEqual(hub.stats["reads"], 3)
        self.run_hub(exercise, reader=delayed, scan_interval=60)

    def test_delayed_duplicate_notify_only_probes_and_wakes_existing_deadline_worker(self):
        from roly import auction_state
        lot = self.start()
        entered, release = threading.Event(), threading.Event()
        hints, statement_counts = [], []
        batches = auction_state._batches
        def counted(live, event, tokens, state_statements=()):
            statement_counts.append(len(state_statements))
            return batches(live, event, tokens, state_statements)
        def delayed(*args, **kwargs):
            result = read_snapshot(*args, **kwargs)
            hints.append(kwargs["changed_hint"])
            if len(hints) == 2:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("duplicate notification gate timed out")
            return result
        async def exercise(hub):
            room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            await self.frame(member)
            self.live.place_bid(self.tokens[0], self.event, lot["id"], 5, str(uuid4()))
            with patch.object(self.live, "wake_worker") as wake:
                hub.notify(self.event)
                try:
                    async def wait_for_read():
                        while not entered.is_set():
                            await asyncio.sleep(.001)
                    await asyncio.wait_for(wait_for_read(), 2)
                    hub.notify(self.event)  # Same commit, delayed delivery; no new write.
                    self.assertTrue(room.probe_pending)
                finally:
                    release.set()
                await self.frame(member)
                async def wait_for_probe():
                    while hub.stats["reads"] < 3:
                        await asyncio.sleep(.001)
                await asyncio.wait_for(wait_for_probe(), 2)
                self.assertEqual(wake.call_count, 2)
            self.assertTrue(member.queue.empty())
            self.assertEqual(hints, [False, True, False])
            self.assertEqual(statement_counts, [7, 4, 0])
        with patch.object(auction_state, "_batches", side_effect=counted):
            self.run_hub(exercise, reader=delayed, scan_interval=60)

    def test_duplicate_notifications_coalesce_without_frames_and_joins_keep_the_probe(self):
        self.start()
        hints = []
        def observed(*args, **kwargs):
            hints.append(kwargs["changed_hint"])
            return read_snapshot(*args, **kwargs)

        async def exercise(hub):
            room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            first = await self.frame(member)
            hub.notify(self.event + 10000)
            self.assertFalse(room.change_pending)
            for event_id in (self.event, self.event, 0, None):
                hub.notify(event_id)
            async def wait_for_read():
                while hub.stats["reads"] < 2:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(wait_for_read(), 2)
            self.assertTrue(member.queue.empty())
            self.assertFalse(room.change_pending)
            self.assertFalse(room.wake.is_set())
            _, joined = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            restored = await self.frame(joined)
            self.assertEqual(restored["type"], "snapshot")
            self.assertEqual(restored["revision"], first["revision"])
            self.assertTrue(member.queue.empty())
            self.assertEqual(hints, [False, True, False])
        self.run_hub(exercise, reader=observed, scan_interval=60)

    def test_failed_notification_read_recovers_on_default_periodic_probe_without_another_wake(self):
        lot = self.start()
        hints = []
        def fail_once(*args, **kwargs):
            hints.append(kwargs["changed_hint"])
            if len(hints) == 2:
                raise RuntimeError("Synthetic read failure after notification")
            return read_snapshot(*args, **kwargs)

        async def run():
            self.runtime.push = hub = AuctionHub(self.runtime, reader=fail_once, listen=False)
            try:
                self.assertEqual(hub.scan_interval, 1.0)
                room, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
                first = await self.frame(member)
                self.live.place_bid(self.tokens[0], self.event, lot["id"], 5, str(uuid4()))
                hub.notify(self.event)
                failed = await self.frame(member)
                self.assertEqual(failed["error"]["code"], "transport_uncertain")
                self.assertIsNone(hub.pong(room, member, 1))
                repaired = await self.frame(member)  # No next NOTIFY; default 1s scan repairs the missed read.
                self.assertEqual(repaired["panel"]["lot"]["highest_bid"], 5)
                self.assertGreater(repaired["revision"], first["revision"])
                self.assertEqual(hints, [False, True, False])
                self.assertIsNotNone(hub.pong(room, member, 2))
            finally:
                await hub.close()
        asyncio.run(run())

    def test_capacity_and_shutdown_reject_new_subscriptions(self):
        self.start()
        async def exercise(hub):
            await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            with self.assertRaises(http.TransportError) as full:
                await hub.subscribe(self.tokens[0], self.event, self.envelope())
            self.assertEqual(full.exception.code, "connection_capacity")
            await hub.close()
            with self.assertRaises(http.TransportError) as closed:
                await hub.subscribe(self.tokens[0], self.event, self.envelope())
            self.assertEqual(closed.exception.code, "server_epoch_changed")
        self.run_hub(exercise, max_subscribers=1)

    def test_shutdown_during_authorization_cannot_register_a_late_subscription(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.authorize
        def waiting(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Synthetic authorization gate timed out")
            return original(*args)
        async def exercise(hub):
            with patch.object(self.runtime, "authorize", side_effect=waiting):
                arrival = asyncio.create_task(hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token)))
                while not entered.is_set():
                    await asyncio.sleep(.001)
                try:
                    await hub.close()
                finally:
                    release.set()
                with self.assertRaises(http.TransportError) as closed:
                    await arrival
                self.assertEqual(closed.exception.code, "server_epoch_changed")
                self.assertEqual(hub.rooms, {})
        self.run_hub(exercise)

    def test_removed_auction_clears_state_and_companion_cards(self):
        self.start()
        async def exercise(hub):
            _, member = await hub.subscribe(self.player_token, self.event, self.envelope(token=self.player_token))
            initial = await self.frame(member)
            self.assertTrue(initial["panel"]["views"]["teams"])
            with self.core.transaction() as db:
                for table in ("live_bids", "live_events", "live_lots"):
                    db.execute(f"DELETE FROM {table} WHERE event_id=?", (self.event,))
                db.execute("DELETE FROM live_sessions WHERE event_id=?", (self.event,))
            hub.notify(self.event)
            removed = await self.frame(member)
            self.assertGreater(removed["revision"], initial["revision"])
            self.assertFalse(removed["panel"]["available"])
            self.assertIsNone(removed["panel"]["views"])
        self.run_hub(exercise)

    def test_websocket_auth_epoch_reconnect_and_bid_channel_rejection(self):
        lot = self.start()
        async def exercise(hub):
            headers = {"host": "auction.test", "origin": "https://auction.test"}
            def body(**changes):
                return {**self.envelope(token=self.player_token), "type": "subscribe", "session_token": self.player_token,
                        "server_epoch": self.runtime.epoch, **changes}
            for changes, code in (({"server_epoch": "retired-server"}, "server_epoch_changed"),
                                  ({"command": self.envelope(lot)["command"]}, "view_cannot_bid")):
                client = WebSocketProbe(self.app, headers)
                await client.open(body(**changes))
                error = await client.packet()
                self.assertEqual(error["error"]["code"], code)
                await client.close()
            for _ in range(2):
                client = WebSocketProbe(self.app, headers)
                await client.open(body())
                snapshot = await client.packet()
                self.assertEqual(snapshot["type"], "snapshot")
                self.assertEqual(snapshot["epoch"], self.runtime.epoch)
                await client.close()
        self.run_hub(exercise)


class SubscriptionQueueTests(unittest.TestCase):
    def test_removal_cannot_resurrect_unsent_cards_when_queue_coalesces(self):
        member = Subscription("private-token", "context", {"seq": 1})
        member.offer({"type": "snapshot", "panel": {"transport": {"request": {"seq": 1}},
                      "views": {"teams": {"1": "old card"}}}})
        member.offer({"type": "state", "panel": {"transport": {"request": None}, "views": None}})
        frame = member.queue.get_nowait()
        self.assertEqual(frame["type"], "snapshot")
        self.assertIsNone(frame["panel"]["views"])
        self.assertEqual(frame["panel"]["transport"]["request"], {"seq": 1})

    def test_slow_consumer_keeps_latest_hot_state_and_unsent_full_details(self):
        async def run():
            member = Subscription("private-token", "context", {"seq": 1})
            member.offer({"type": "snapshot", "revision": 1, "panel": {
                "lot": {"highest_bid": 10}, "transport": {"request": {"seq": 1}}, "views": {"teams": {"1": "cards"}, "history": "old"}}})
            for revision in range(2, 30):
                member.offer({"type": "state", "revision": revision, "panel": {
                    "lot": {"highest_bid": revision}, "transport": {"request": None}, "views": {"history": revision}}})
            self.assertEqual(member.queue.qsize(), 1)
            frame = member.queue.get_nowait()
            self.assertEqual(frame["type"], "snapshot")
            self.assertEqual(frame["revision"], 29)
            self.assertEqual(frame["panel"]["views"], {"teams": {"1": "cards"}, "history": 29})
            self.assertEqual(frame["panel"]["transport"]["request"], {"seq": 1})
            member.offer(frame)
            member.offer({"type": "error", "error": {"code": "session_expired"}})
            self.assertNotIn("panel", member.queue.get_nowait())
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
