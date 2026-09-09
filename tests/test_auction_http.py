"""The real ASGI routing and domain rules using isolated SQLite only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from starlette.applications import Starlette

from roly import auction_http as http
from roly.auction_commands import context_id
from tests import test_live_auction as fixtures


async def call_asgi(app, body, headers, path="/api/auction/bid", method="POST"):
    encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "server": ("auction.test", 443), "client": ("127.0.0.1", 12345)}
    sent = []
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    response = next(m for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw.decode()
    return response["status"], value, dict(response.get("headers", []))


class AuctionHTTPTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.LiveAuctionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.LiveAuctionTests.tearDownClass.__func__)
    start = fixtures.LiveAuctionTests.start

    def setUp(self):
        fixtures.LiveAuctionTests.setUp(self)
        self.addCleanup(fixtures.LiveAuctionTests.tearDown, self)
        self.runtime = http.AuctionHTTP(self.core.db_path, factory=lambda path: {"core": self.core, "live": self.live})
        self.app = Starlette(routes=http.routes())
        runtime = patch.object(http, "_runtime", self.runtime)
        runtime.start(); self.addCleanup(runtime.stop)
        for target in ("psycopg.connect", "roly.storage_config._runtime_document", "urllib.request.urlopen"):
            blocked = patch(target, side_effect=AssertionError("No operating settings, DB or network in HTTP tests"))
            blocked.start(); self.addCleanup(blocked.stop)
        worker = patch.object(self.live, "ensure_worker")
        worker.start(); self.addCleanup(worker.stop)

    def envelope(self, lot=None, amount=10, *, token=None, request_id=None):
        token = token or self.tokens[0]
        return {"event_id": self.event, "context": context_id(self.core.db_path, token, self.event),
                "epoch": uuid4().hex, "seq": 1, "sent_ms": 123.0,
                **({"command": {"lot_id": lot["id"], "amount": amount, "request_id": request_id or str(uuid4())}} if lot else {})}

    def headers(self, token=None):
        return {"host": "auction.test", "origin": "https://auction.test", "content-type": "application/json",
                "authorization": "Bearer " + (token or self.tokens[0]), http.EPOCH_HEADER: self.runtime.epoch}

    def request(self, body, *, token=None, headers=None, path="/api/auction/bid", method="POST"):
        return asyncio.run(call_asgi(self.app, body, headers or self.headers(token), path, method))

    def count_bids(self):
        with closing(self.core.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM live_bids WHERE event_id=?", (self.event,)).fetchone()[0]

    def test_bid_commits_once_without_view_and_returns_small_sanitized_ack(self):
        lot = self.start(bid_seconds=10)
        body = self.envelope(lot)
        with patch.object(self.live, "get_view", side_effect=AssertionError("ACK must not wait for a view")):
            status, value, headers = self.request(body)
        self.assertEqual((status, value["ack"]["status"], self.count_bids()), (200, "accepted", 1))
        self.assertGreaterEqual(value["server_elapsed_ms"], value["callback_elapsed_ms"])
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(value["request"]["command"], body["command"])
        encoded = json.dumps(value)
        for secret in (self.tokens[0], self.core.db_path, "fingerprint", "token_hash"):
            self.assertNotIn(secret, encoded)

    def test_same_uuid_concurrency_reuses_terminal_and_does_not_extend_twice(self):
        lot = self.start(bid_seconds=10)
        self.clock.advance(9)
        body = self.envelope(lot)
        barrier = threading.Barrier(4)
        def submit():
            barrier.wait()
            return self.runtime.bid(self.tokens[0], self.event, body)
        with ThreadPoolExecutor(max_workers=4) as executor:
            replies = list(executor.map(lambda _: submit(), range(4)))
        self.assertEqual([r["ack"]["status"] for r in replies], ["accepted"]*4)
        state = self.live.get_state(self.event)
        self.assertEqual(self.count_bids(), 1)
        self.assertEqual(state["current_lot"]["closes_at"], lot["closes_at"]+5)
        self.assertEqual(self.runtime.command_count, 1)

    def test_four_teams_same_amount_have_one_receipt_and_three_terminal_rejections(self):
        lot = self.start()
        bodies = [self.envelope(lot, token=token) for token in self.tokens]
        barrier = threading.Barrier(4)
        def submit(i):
            barrier.wait()
            return self.runtime.bid(self.tokens[i], self.event, bodies[i])["ack"]["status"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(submit, range(4)))
        self.assertEqual(statuses.count("accepted"), 1)
        self.assertEqual(statuses.count("rejected"), 3)
        self.assertEqual(self.count_bids(), 1)

    def test_paused_rejection_cannot_become_bid_after_resume(self):
        lot = self.start()
        self.live.pause(self.admin, self.event)
        body = self.envelope(lot)
        self.assertEqual(self.request(body)[1]["ack"]["status"], "rejected")
        self.live.resume(self.admin, self.event)
        self.assertEqual(self.request(body)[1]["ack"]["status"], "rejected")
        self.assertEqual(self.count_bids(), 0)

    def test_uncertain_commit_is_confirmed_without_repeating_write(self):
        lot = self.start()
        body = self.envelope(lot)
        original = self.live.place_bid
        def lost(*args):
            original(*args)
            raise sqlite3.OperationalError("private database host should not escape")
        with patch.object(self.live, "place_bid", side_effect=lost) as write:
            first = self.request(body)[1]
            second = self.request({**body, "confirm_only": True})[1]
            self.assertEqual(write.call_count, 1)
        self.assertEqual((first["ack"]["status"], second["ack"]["status"], self.count_bids()), ("pending", "accepted", 1))
        self.assertNotIn("private database", json.dumps(first))

    def test_confirm_absent_is_terminal_and_cannot_later_submit(self):
        lot = self.start()
        body = self.envelope(lot)
        self.assertEqual(self.request({**body, "confirm_only": True})[1]["ack"]["status"], "rejected")
        self.assertEqual(self.request(body)[1]["ack"]["status"], "rejected")
        self.assertEqual(self.count_bids(), 0)

    def test_epoch_change_blocks_old_request_before_any_service_access(self):
        lot = self.start()
        body = self.envelope(lot)
        old_headers = self.headers()
        self.runtime.epoch = uuid4().hex
        with patch.object(self.runtime, "factory", side_effect=AssertionError("old epoch reached DB")):
            status, value, _ = self.request(body, headers=old_headers)
        self.assertEqual(status, 409)
        self.assertTrue(value["error"]["reload_required"])
        self.assertEqual(self.count_bids(), 0)

    def test_logout_and_non_captain_cannot_use_saved_or_new_bid(self):
        lot = self.start()
        body = self.envelope(lot)
        self.assertEqual(self.request(body)[1]["ack"]["status"], "accepted")
        self.core.logout(self.tokens[0])
        self.assertEqual(self.request(body)[0], 401)
        general = self.envelope(lot, 20, token=self.player_token)
        self.assertEqual(self.request(general, token=self.player_token)[1]["ack"]["status"], "rejected")
        self.assertEqual(self.count_bids(), 1)

    def test_same_uuid_different_payload_preserves_original_result(self):
        lot = self.start()
        body = self.envelope(lot)
        self.request(body)
        changed = {**body, "command": {**body["command"], "amount": 20}}
        self.assertEqual(self.request(changed)[1]["ack"]["status"], "rejected")
        self.assertEqual(self.request(body)[1]["ack"]["status"], "accepted")
        self.assertEqual(self.count_bids(), 1)

    def test_origin_epoch_token_body_limits_and_methods_fail_before_database(self):
        good = self.envelope()
        cases = [({**self.headers(), "origin": "https://evil.test"}, good, "POST", 403),
                 ({k:v for k,v in self.headers().items() if k != "authorization"}, good, "POST", 401),
                 ({**self.headers(), "authorization": "Bearer short"}, good, "POST", 401),
                 ({**self.headers(), http.EPOCH_HEADER: "wrong"}, good, "POST", 409),
                 (self.headers(), b" "*(http.MAX_BODY+1), "POST", 413),
                 (self.headers(), b"{invalid", "POST", 400),
                 ({**self.headers(), "content-type": "text/plain"}, good, "POST", 415),
                 (self.headers(), good, "GET", 405)]
        with patch.object(self.runtime, "factory", side_effect=AssertionError("invalid input reached DB")):
            for headers, body, method, expected in cases:
                with self.subTest(expected=expected, method=method):
                    self.assertEqual(self.request(body, headers=headers, method=method)[0], expected)

    def test_ledger_capacity_never_evicts_a_rejection(self):
        lot = self.start()
        self.runtime.max_commands = 1
        self.live.pause(self.admin, self.event)
        rejected = self.envelope(lot)
        self.assertEqual(self.request(rejected)[1]["ack"]["status"], "rejected")
        self.live.resume(self.admin, self.event)
        self.assertEqual(self.request(self.envelope(lot))[0], 503)
        self.assertEqual(self.request(rejected)[1]["ack"]["status"], "rejected")
        self.assertEqual(self.count_bids(), 0)

    def test_pending_old_uuid_blocks_a_new_command_until_resolved(self):
        lot = self.start()
        first, other = self.envelope(lot), self.envelope(lot, 20)
        with patch.object(self.live, "place_bid", side_effect=sqlite3.OperationalError("offline")) as write:
            self.assertEqual(self.request(first)[1]["ack"]["status"], "pending")
            response = self.request(other)[1]
            self.assertEqual(response["ack"]["request_id"], first["command"]["request_id"])
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.count_bids(), 0)

    def test_live_view_uses_saved_profile_projection_and_no_write(self):
        self.start()
        with patch.object(self.live, "place_bid", side_effect=AssertionError("view wrote bid")):
            status, value, _ = self.request(self.envelope(), path="/api/auction/live")
        self.assertEqual(status, 200)
        panel = value["panel"]
        self.assertTrue(panel["control"]["can_bid"])
        self.assertIsNone(panel["transport"]["frame_id"])
        self.assertIsNone(panel["transport"]["ack"])
        self.assertEqual(panel["transport"]["request"]["seq"], 1)
        self.assertEqual(len(panel["views"]["teams"]), 4)
        self.assertIn("queue", panel["views"])
        for secret in (self.tokens[0], self.core.db_path, "token_hash", "registration_status"):
            self.assertNotIn(secret, json.dumps(value))

    def test_ready_view_previews_first_queued_player_and_disables_bidding(self):
        state = self.live.configure(self.admin, self.event, order=self.pool, bid_seconds=30)
        status, value, _ = self.request(self.envelope(), path="/api/auction/live")
        self.assertEqual(status, 200)
        self.assertEqual(value["panel"]["lot"]["id"], state["lots"][0]["id"])
        self.assertFalse(value["panel"]["control"]["can_bid"])
        self.assertEqual(self.count_bids(), 0)

    def test_live_clock_metrics_separate_queued_request_view_and_serialization(self):
        self.start()
        actor, state = self.live.get_view(self.tokens[0], self.event)
        from roly import auction_components
        original = auction_components.live_companion_data
        virtual = [100.0]
        def view(*args):
            virtual[0] += 2
            return actor, state
        def companions(*args):
            virtual[0] += 3
            return original(*args)
        with patch.object(http, "monotonic", side_effect=lambda: virtual[0]), patch.object(self.live, "get_view", side_effect=view), patch.object(auction_components, "live_companion_data", side_effect=companions):
            response = self.runtime.live(self.tokens[0], self.event, self.envelope(), request_started=90.0)
        metrics = response["panel"]["transport"]
        self.assertEqual(metrics["server_elapsed_ms"], 12000)
        self.assertEqual(metrics["view_elapsed_ms"], 2000)
        self.assertEqual(metrics["snapshot_elapsed_ms"], 3000)

    def test_existing_context_new_bid_has_no_duplicate_session_read_but_cached_ack_does(self):
        lot = self.start()
        first = self.envelope(lot, 0)
        self.request(first)
        with patch.object(self.runtime, "_actor", wraps=self.runtime._actor) as authorization:
            second = self.envelope(lot, 10)
            self.assertEqual(self.request(second)[1]["ack"]["status"], "accepted")
            authorization.assert_not_called()
            self.assertEqual(self.request(second)[1]["ack"]["status"], "accepted")
            authorization.assert_called_once()
        self.assertEqual(self.count_bids(), 2)

    def test_db_deadline_rejects_late_request_without_extension(self):
        lot = self.start(bid_seconds=10)
        self.clock.value = lot["closes_at"]
        result = self.request(self.envelope(lot))[1]
        self.assertEqual(result["ack"]["status"], "rejected")
        self.assertEqual(self.count_bids(), 0)
        self.assertEqual(self.live.get_state(self.event)["current_lot"]["closes_at"], lot["closes_at"])

    def test_cached_ack_after_sale_preserves_single_settlement_and_charge(self):
        lot = self.start()
        body = self.envelope(lot)
        self.request(body)
        self.clock.value = self.live.get_state(self.event)["current_lot"]["closes_at"]
        self.live.settle_due()
        self.assertEqual(self.request(body)[1]["ack"]["status"], "accepted")
        with closing(self.core.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_events WHERE event_id=? AND type='SOLD'", (self.event,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT price FROM competition_players WHERE event_id=? AND member_id=?", (self.event,lot["member_id"])).fetchone()[0], 10)


class AuctionHTTPConfigurationTests(unittest.TestCase):
    def test_pg_cached_ack_auth_uses_the_canonical_session_query_in_one_snapshot(self):
        from roly.core import SESSION_SELECT
        db = Mock()
        db.fetch_snapshot_batches.return_value = [[{"id": 4, "member_id": 3}]]
        core = SimpleNamespace(is_postgres=True, connect=lambda: db)
        runtime = http.AuctionHTTP("synthetic-local-store")
        actor = runtime._actor(core, "synthetic-local-session")
        self.assertEqual(actor["id"], 4)
        statements = db.fetch_snapshot_batches.call_args.args[0]
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0][0], SESSION_SELECT)
        db.execute.assert_not_called()
        db.close.assert_called_once()

    def test_local_and_signed_out_config_never_load_settings(self):
        active = http.AuctionHTTP("supabase://rolymoly")
        with patch.object(http, "_runtime", active), patch("roly.storage_config._runtime_document", side_effect=AssertionError("private settings")):
            self.assertIsNone(http.transport_config("local.sqlite3", "synthetic-local-session", 1))
            self.assertIsNone(http.transport_config("supabase://rolymoly", None, 1))
            self.assertIsNone(http.transport_config("supabase://other", "synthetic-local-session", 1))
            config = http.transport_config("supabase://rolymoly", "synthetic-local-session", 1)
            self.assertEqual(config["epoch"], active.epoch)
            self.assertNotIn("synthetic-local-session", json.dumps(config))
            self.assertNotIn("supabase://", json.dumps(config))

    def test_lifespan_registers_new_epoch_then_disables_without_services(self):
        async def exercise():
            with patch.object(http, "_runtime", None), patch.object(http, "_target", return_value="supabase://rolymoly"), patch.object(http, "_services", side_effect=AssertionError("startup connected DB")):
                async with http.lifespan(None):
                    first = http.transport_config("supabase://rolymoly", "synthetic-local-session", 1)
                    self.assertIsNotNone(first)
                self.assertIsNone(http.transport_config("supabase://rolymoly", "synthetic-local-session", 1))
                async with http.lifespan(None):
                    second = http.transport_config("supabase://rolymoly", "synthetic-local-session", 1)
                    self.assertNotEqual(first["epoch"], second["epoch"])
        asyncio.run(exercise())

    def test_official_cli_discovers_existing_cloud_entry_without_executing_it(self):
        from streamlit.web.server.app_discovery import discover_asgi_app
        result = discover_asgi_app(Path(__file__).resolve().parents[1] / "streamlit_app.py")
        self.assertTrue(result.is_asgi_app)
        self.assertEqual(result.app_name, "app")
        self.assertTrue(result.import_string.endswith(":app"))


if __name__ == "__main__":
    unittest.main()
