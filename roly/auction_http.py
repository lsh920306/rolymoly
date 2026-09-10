"""Same-origin auction transport independent of Streamlit script reruns.

Authentication remains a revocable Core session. A process epoch prevents an
old request from becoming a new command after its terminal ledger was lost.
"""
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
import os
import logging
from pathlib import Path
import threading
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from anyio import CapacityLimiter, to_thread
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from .auction_commands import clean_envelope, context_id, execute_command

MAX_BODY = 4096
MAX_CONTEXTS = 128
MAX_COMMANDS = 16384
MAX_CONTEXT_COMMANDS = 8192
EPOCH_HEADER = "X-Rolymoly-Server-Epoch"
SESSION_HEADER = "X-Rolymoly-Session"
_runtime = None
_runtime_lock = threading.RLock()
_logger = logging.getLogger(__name__)


class TransportDiagnostics:
    """Log startup/first delivery without recording credentials or payloads."""
    def __init__(self, app):
        self.app = app
        self.seen = set()

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        endpoint = next((name for name in ("live", "bid") if path.endswith("/api/auction/" + name)), None)
        first = scope["type"] == "http" and endpoint and endpoint not in self.seen
        if first:
            self.seen.add(endpoint)
            _logger.warning("Auction HTTP first delivery: endpoint=%s method=%s path=%r root_path=%r",
                            endpoint, scope.get("method"), path[:128], str(scope.get("root_path", ""))[:128])
        async def report_status(message):
            if first and message["type"] == "http.response.start":
                _logger.warning("Auction HTTP first response: endpoint=%s status=%s", endpoint, message["status"])
            await send(message)
        await self.app(scope, receive, report_status if first else send)


class TransportError(Exception):
    def __init__(self, code, message, status=400, *, reload=False):
        self.code, self.message, self.status, self.reload = code, message, status, reload


@dataclass
class Commands:
    lock: threading.RLock = field(default_factory=threading.RLock)
    terminal: dict = field(default_factory=dict)
    pending: dict | None = None


def _services(path):
    from .service_resources import service_bundle
    return service_bundle(path)


class AuctionHTTP:
    """One ASGI lifetime, a fixed store, and bounded non-evicting receipts."""
    def __init__(self, db_path, *, factory=_services, epoch=None, max_contexts=MAX_CONTEXTS,
                 max_commands=MAX_COMMANDS, max_context_commands=MAX_CONTEXT_COMMANDS):
        self.db_path = str(db_path)
        self.factory = factory
        self.epoch = epoch or uuid4().hex
        self.active = True
        self.lock = threading.RLock()
        self.contexts = {}
        self.command_count = 0
        self.max_contexts, self.max_commands = max_contexts, max_commands
        self.max_context_commands = max_context_commands
        self.read_limiter, self.bid_limiter = CapacityLimiter(2), CapacityLimiter(2)
        self.push = None

    def check_epoch(self, value):
        if not self.active or value != self.epoch:
            raise TransportError("server_epoch_changed", "서버가 갱신되었습니다. 화면을 새로 열고 로그인 상태를 확인해 주세요.", 409, reload=True)

    def authorize(self, token, event_id, envelope):
        self.check_epoch(self.epoch)
        if type(event_id) is not int or not 0 < event_id <= 2**53 - 1:
            raise TransportError("invalid_event", "경매 번호를 확인해 주세요.")
        bundle = self.factory(self.db_path)
        expected = context_id(self.db_path, token, event_id)
        try:
            request = clean_envelope(envelope, expected)
        except ValueError:
            raise TransportError("invalid_envelope", "입찰 연결 정보가 변경되었습니다. 화면을 다시 열어 주세요.", 409, reload=True) from None
        return bundle, expected, request

    def _actor(self, core, token):
        if core.is_postgres:
            from .core import session_query
            with closing(core.connect()) as db:
                rows = db.fetch_snapshot_batches([session_query(token)])[0]
                actor = dict(rows[0]) if rows else None
        else:
            actor = core.session(token)
        if not actor:
            raise TransportError("session_expired", "로그인이 만료되었습니다. 다시 로그인해 주세요.", 401, reload=True)
        return actor

    def _bucket(self, context):
        with self.lock:
            bucket = self.contexts.get(context)
            if bucket is None:
                if len(self.contexts) >= self.max_contexts:
                    raise TransportError("command_capacity", "입찰 확인 기록이 가득 찼습니다. 진행자에게 문의해 주세요.", 503)
                bucket = self.contexts[context] = Commands()
            return bucket

    def _reserve(self, bucket):
        with self.lock:
            if len(bucket.terminal) >= self.max_context_commands or self.command_count >= self.max_commands:
                raise TransportError("command_capacity", "입찰 확인 기록이 가득 찼습니다. 진행자에게 문의해 주세요.", 503)
            self.command_count += 1

    def bid(self, token, event_id, envelope, *, confirm_only=False):
        bundle, context, request = self.authorize(token, event_id, envelope)
        command = request.get("command")
        if not command:
            raise TransportError("command_required", "확인할 입찰 정보가 없습니다.")
        started = monotonic()
        with self.lock:
            bucket = self.contexts.get(context)
        if bucket is None:
            # Authenticate before allocating memory for a new context. Once it
            # exists, writes/resolutions use their own authoritative transaction
            # check; only cached ACKs need a separate current-session read.
            self._actor(bundle["core"], token)
            bucket = self._bucket(context)
        with bucket.lock:
            # Pending always has priority; a newer UUID cannot get past an
            # uncertain COMMIT even if its sender discarded the old command.
            if bucket.pending and bucket.pending != command:
                original = bucket.pending
                ack = execute_command(bundle["live"], token, event_id, original, confirm_only=True)
                if ack["status"] != "pending":
                    bucket.terminal[str(UUID(original["request_id"]))] = dict(ack)
                    bucket.pending = None
                return {"context": context, "epoch": self.epoch, "request": request, "ack": ack,
                        "callback_elapsed_ms": max(0, (monotonic() - started) * 1000)}
            key = str(UUID(command["request_id"]))
            previous = bucket.terminal.get(key)
            if previous is not None:
                self._actor(bundle["core"], token)
                if (previous["lot_id"], previous["amount"]) != (command["lot_id"], command["amount"]):
                    ack = {**command, "status": "rejected", "message": "같은 요청 번호로 다른 입찰을 전송할 수 없습니다."}
                else:
                    ack = {**previous, "request_id": command["request_id"]}
            else:
                if bucket.pending is None:
                    self._reserve(bucket)
                    bucket.pending = dict(command)
                    resolve = confirm_only
                else:
                    resolve = True
                # Keep pending before entering a write. Any storage/transport
                # uncertainty only permits resolve_bid on later attempts.
                ack = execute_command(bundle["live"], token, event_id, command, confirm_only=resolve)
                if ack["status"] in ("accepted", "rejected"):
                    bucket.terminal[key] = dict(ack)
                    bucket.pending = None
            return {"context": context, "epoch": self.epoch, "request": request, "ack": ack,
                    "callback_elapsed_ms": max(0, (monotonic() - started) * 1000)}

    def live(self, token, event_id, envelope, *, request_started=None):
        bundle, context, request = self.authorize(token, event_id, envelope)
        if request.get("command") is not None:
            raise TransportError("view_cannot_bid", "입찰은 입찰 전용 경로로 전송해 주세요.")
        started = monotonic()
        request_started = started if request_started is None else request_started
        actor, state = bundle["live"].get_view(token, event_id)
        received = monotonic()
        if actor is None:
            raise TransportError("session_expired", "로그인이 만료되었습니다. 다시 로그인해 주세요.", 401, reload=True)
        if state and state["status"] in ("RUNNING", "WAITING", "PAUSED"):
            bundle["live"].ensure_worker()
        from .auction_live_panel import live_panel_data
        from .auction_components import live_companion_data
        display = state
        lot = state.get("current_lot") if state else None
        if state and state["status"] == "READY" and lot is None:
            lot = next((item for item in state.get("lots", ()) if item["status"] == "QUEUED"), None)
            display = dict(state, current_lot=lot)
        own = next((team for team in state["teams"] if team["captain_id"] == actor.get("member_id")), None) if state else None
        control = None
        if own and lot and state["status"] != "COMPLETED":
            can_bid = (state["event"]["status"] == "AUCTION" and state["status"] == "RUNNING" and lot["status"] == "OPEN"
                       and float(lot.get("remaining_seconds") or 0) > 0 and len(own["players"]) < 5)
            control = {"team_name": own["name"], "remaining": own["remaining"], "can_bid": can_bid, "context": context}
        panel = live_panel_data(display, control=control, transport={"context": context, "frame_id": None,
            "request": request, "server_elapsed_ms": max(0, (received-request_started)*1000), "view_elapsed_ms": max(0, (received-started)*1000),
            "snapshot_elapsed_ms": max(0, (monotonic()-received)*1000), "callback_elapsed_ms": 0, "ack": None})
        panel["views"] = live_companion_data(state, actor) if state else None
        panel["transport"]["snapshot_elapsed_ms"] = max(0, (monotonic()-received)*1000)
        return {"context": context, "epoch": self.epoch, "panel": panel}


def transport_config(db_path, token, event_id):
    # Local/demo rendering must not read Secrets or activate a network service.
    if not str(db_path).startswith("supabase://") or not isinstance(token, str) or not token:
        return None
    with _runtime_lock:
        active = _runtime
        if active is None or not active.active or active.db_path != str(db_path):
            return None
        return {"live_url": "/api/auction/live", "bid_url": "/api/auction/bid",
                **({"ws_url": "/api/auction/ws"} if active.push is not None else {}), "epoch": active.epoch,
                "storage_key": "roly-login-" + sha256(str(db_path).encode()).hexdigest()[:24], "event_id": event_id}


def _response(value, status=200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"})


def _check_origin(request):
    origin = urlsplit(request.headers.get("origin", ""))
    host = request.headers.get("host", "").lower()
    local = origin.hostname in ("localhost", "127.0.0.1", "::1")
    if (origin.netloc.lower() != host or origin.username or origin.password or origin.path or origin.query or origin.fragment
            or origin.scheme not in (("https", "http") if local else ("https",))):
        raise TransportError("origin_rejected", "같은 사이트에서 다시 요청해 주세요.", 403)
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        raise TransportError("origin_rejected", "같은 사이트에서 다시 요청해 주세요.", 403)


def _credentials(request, body):
    # Hosting gateways may consume Authorization for their own authentication.
    # Use our app-specific header while keeping the same revocable Core session.
    value = request.headers.get(SESSION_HEADER)
    if value is None:
        legacy = request.headers.get("authorization", "")
        value = legacy[7:] if legacy.startswith("Bearer ") else body.get("session_token", "")
    if not isinstance(value, str) or not 20 <= len(value) <= 256 or any(c.isspace() for c in value):
        raise TransportError("login_required", "본인 계정으로 로그인해 주세요.", 401, reload=True)
    return value


async def _body(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise TransportError("json_required", "JSON 형식의 요청만 받을 수 있습니다.", 415)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BODY:
            raise TransportError("request_too_large", "입찰 요청이 너무 큽니다.", 413)
        content.extend(chunk)
    import json
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise TransportError("invalid_json", "요청 형식을 확인해 주세요.") from None
    if not isinstance(value, dict) or set(value) - {"event_id", "context", "epoch", "seq", "sent_ms", "command", "confirm_only", "session_token", "server_epoch"}:
        raise TransportError("invalid_json", "요청 형식을 확인해 주세요.")
    if type(value.get("confirm_only", False)) is not bool:
        raise TransportError("invalid_confirmation", "입찰 확인 형식을 확인해 주세요.")
    return value


async def handle(request, *, bidding):
    started = monotonic()
    try:
        _check_origin(request)
        body = await _body(request)
        token = _credentials(request, body)
        server_epoch = request.headers.get(EPOCH_HEADER, body.get("server_epoch"))
        # Keep transport secrets out of command ledgers, echoed envelopes and views.
        body.pop("session_token", None)
        body.pop("server_epoch", None)
        with _runtime_lock:
            runtime = _runtime
        if runtime is None:
            raise TransportError("transport_unavailable", "입찰 연결을 준비하고 있습니다. 화면을 다시 열어 주세요.", 503, reload=True)
        runtime.check_epoch(server_epoch)
        event_id = body.get("event_id")
        if not bidding and body.get("confirm_only"):
            raise TransportError("view_cannot_confirm", "입찰 확인은 입찰 전용 경로로 요청해 주세요.")
        from .auction_metrics import measure_operation
        with measure_operation() as metrics:
            if not bidding and runtime.push is not None:
                value = await runtime.push.snapshot(token, event_id, body)
            else:
                function = partial(runtime.bid, token, event_id, body, confirm_only=body.get("confirm_only", False)) if bidding else partial(runtime.live, token, event_id, body, request_started=started)
                queued = monotonic()
                def dispatched():
                    metrics.record("dispatch", monotonic()-queued)
                    return function()
                value = await to_thread.run_sync(dispatched, limiter=runtime.bid_limiter if bidding else runtime.read_limiter)
            value["timings"] = metrics.as_dict()
        runtime.check_epoch(server_epoch)
        elapsed = max(0, (monotonic()-started)*1000)
        if bidding:
            value["server_elapsed_ms"] = elapsed
            if runtime.push is not None and value.get("ack", {}).get("status") == "accepted":
                # Wake the shared reader without delaying the committed ACK.
                runtime.push.notify(event_id)
        return _response(value)
    except TransportError as error:
        return _response({"error": {"code": error.code, "message": error.message, "reload_required": error.reload}}, error.status)
    except Exception:
        # A timeout/unknown exception never declares an attempted bid rejected.
        return _response({"error": {"code": "transport_uncertain", "message": "연결을 확인하고 있습니다. 같은 입찰의 접수 여부를 다시 확인해 주세요.", "reload_required": False}}, 503)


async def live_endpoint(request):
    return await handle(request, bidding=False)


async def bid_endpoint(request):
    return await handle(request, bidding=True)


def routes(*, base_url=None):
    """Register APIs below the same configured path as Streamlit's own routes."""
    if base_url is None:
        import streamlit as st

        # The Streamlit CLI loads file/environment/flag options before importing
        # this ASGI entry point. App does not prefix user-supplied routes itself.
        base_url = st.get_option("server.baseUrlPath")
    if not isinstance(base_url, str):
        raise ValueError("The auction API base path must be a string")
    base_url = base_url.strip("/")
    if base_url and (any(part in ("", ".", "..") for part in base_url.split("/"))
                     or any(char in "?#{}\\" or char.isspace() or ord(char) < 32 for char in base_url)):
        raise ValueError("The auction API base path must be a literal URL path")
    prefix = "/" + base_url if base_url else ""
    _logger.warning("Auction HTTP registered routes: base_path=%r", prefix)
    from .auction_push import websocket_endpoint
    return [Route(prefix + "/api/auction/live", live_endpoint, methods=["POST"]),
            Route(prefix + "/api/auction/bid", bid_endpoint, methods=["POST"]),
            WebSocketRoute(prefix + "/api/auction/ws", websocket_endpoint)]


def _target():
    from .deployment_config import load_deployment_config, require_operating_target
    from .storage_config import operating_database
    deployment = load_deployment_config()
    if deployment.allow_demo:
        return None
    root = Path(__file__).resolve().parents[1]
    data_root = Path(os.environ.get("ROLYMOLY_DATA_DIR", root / ".data")).expanduser().resolve()
    return require_operating_target(deployment, operating_database(data_root))


@asynccontextmanager
async def lifespan(app):
    global _runtime
    target = await to_thread.run_sync(_target)
    runtime = AuctionHTTP(target) if target is not None else None
    if runtime is not None:
        from .auction_push import AuctionHub
        runtime.push = AuctionHub(runtime)
    with _runtime_lock:
        if _runtime is not None:
            raise RuntimeError("Auction HTTP lifetime already active")
        _runtime = runtime
    _logger.warning("Auction HTTP startup ready: enabled=%s", runtime is not None)
    try:
        yield
    finally:
        if runtime is not None and runtime.push is not None:
            await runtime.push.close()
        with _runtime_lock:
            if runtime is not None:
                runtime.active = False
            if _runtime is runtime:
                _runtime = None
