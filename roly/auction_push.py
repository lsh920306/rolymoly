"""Shared, revocable auction subscriptions; PostgreSQL remains authoritative.

One reader per auction serves all viewers. Version checks also recover missed
notifications. Queues hold at most one complete current state, never bids/ACKs.
"""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
import json
from time import monotonic
from uuid import uuid4

from anyio import to_thread
from starlette.websockets import WebSocketDisconnect


def presentation(state, actor, context, *, request=None, elapsed_ms=0, views=None):
    from .auction_live_panel import live_panel_data
    display = state
    lot = state.get("current_lot") if state else None
    if state and state["status"] == "READY" and lot is None:
        lot = next((item for item in state.get("lots", ()) if item["status"] == "QUEUED"), None)
        display = dict(state, current_lot=lot)
    own = next((team for team in state["teams"] if team["captain_id"] == actor.get("member_id")), None) if state else None
    control = None
    if own and lot and state["status"] != "COMPLETED":
        control = {"team_name": own["name"], "remaining": own["remaining"], "context": context,
                   "can_bid": state["event"]["status"] == "AUCTION" and state["status"] == "RUNNING"
                   and lot["status"] == "OPEN" and float(lot.get("remaining_seconds") or 0) > 0
                   and len(own["players"]) < 5}
    panel = live_panel_data(display, control=control, transport={"context": context,
        "frame_id": None, "request": request, "server_elapsed_ms": elapsed_ms,
        "view_elapsed_ms": elapsed_ms, "snapshot_elapsed_ms": 0, "callback_elapsed_ms": 0, "ack": None})
    if state is None:
        panel["views"] = None
    elif views is not None:
        panel["views"] = views
    return panel


@dataclass(eq=False, repr=False)
class Subscription:
    token: str
    context: str
    request: dict
    key: str = field(default_factory=lambda: uuid4().hex)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    actor: dict | None = None
    initialized: bool = False
    last_revision: int = -1
    last_detail: int = -1
    joined: float = field(default_factory=monotonic)

    def offer(self, frame):
        if self.queue.full():
            previous = self.queue.get_nowait()
            if "panel" in previous and "panel" in frame:
                # Keep detail sections if a faster new hot state replaces them
                # before a slow socket has sent the preceding snapshot.
                before, after = previous["panel"], frame["panel"]
                views = (None if "views" in after and after["views"] is None else
                         {**(before.get("views") or {}), **(after.get("views") or {})})
                frame = {**frame, "panel": {**after, "views": views}}
                if previous.get("type") == "snapshot":
                    frame["type"] = "snapshot"
                    frame["panel"]["transport"] = {**after["transport"],
                        "request": before["transport"].get("request"),
                        "server_elapsed_ms": max(0, (monotonic()-self.joined)*1000)}
        self.queue.put_nowait(frame)


@dataclass(repr=False)
class Room:
    event_id: int
    live: object
    members: dict = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    change_pending: bool = False
    probe_pending: bool = False
    reading: bool = False
    task: object = None
    state: object = None
    version: object = None
    revision: int = -1
    detail_revision: int = -1
    display_revision: int | None = None
    views: dict = field(default_factory=dict)
    clock: float | None = None
    clock_at: float = 0
    verified_at: float = 0


class AuctionHub:
    def __init__(self, runtime, *, reader=None, scan_interval=1.0, max_rooms=32,
                 max_subscribers=256, listen=True):
        self.runtime = runtime
        self.reader = reader
        self.scan_interval = scan_interval
        self.max_rooms = max_rooms
        self.max_subscribers = max_subscribers
        self.rooms = {}
        self.closed = False
        self.listener = None
        self.listen = listen
        self.stats = {"reads": 0, "full_states": 0, "allocation_states": 0, "hot_states": 0, "frames": 0}

    async def subscribe(self, token, event_id, envelope):
        from .auction_http import TransportError
        joined = monotonic()
        if self.closed or not self.runtime.active:
            raise TransportError("server_epoch_changed", "화면을 새로고침해 주세요.", 409, reload=True)
        if sum(len(room.members) for room in self.rooms.values()) >= self.max_subscribers:
            raise TransportError("connection_capacity", "접속이 많습니다. 잠시 후 다시 연결해 주세요.", 503)
        bundle, context, request = await to_thread.run_sync(
            partial(self.runtime.authorize, token, event_id, envelope), limiter=self.runtime.read_limiter)
        if self.closed or not self.runtime.active:
            raise TransportError("server_epoch_changed", "화면을 새로고침해 주세요.", 409, reload=True)
        if request.get("command") is not None:
            raise TransportError("view_cannot_bid", "입찰은 입찰 전용 경로로 전송해 주세요.")
        room = self.rooms.get(event_id)
        if room is None:
            if len(self.rooms) >= self.max_rooms:
                raise TransportError("connection_capacity", "열린 경매가 많습니다. 잠시 후 다시 연결해 주세요.", 503)
            room = self.rooms[event_id] = Room(event_id, bundle["live"])
        # Recheck after the await: concurrent arrivals also share the cap.
        if sum(len(item.members) for item in self.rooms.values()) >= self.max_subscribers:
            raise TransportError("connection_capacity", "접속이 많습니다. 잠시 후 다시 연결해 주세요.", 503)
        subscription = Subscription(token, context, request, joined=joined)
        room.members[subscription.key] = subscription
        room.wake.set()
        if room.task is None or room.task.done():
            room.task = asyncio.create_task(self._run(room))
        if self.listener is None and self.listen and room.live.core.is_postgres:
            from .auction_notifications import NotificationListener
            loop = asyncio.get_running_loop()
            self.listener = NotificationListener(room.live.core.schema,
                lambda event_id=None: loop.call_soon_threadsafe(self.notify, event_id))
            self.listener.start()
        return room, subscription

    def unsubscribe(self, room, subscription):
        room.members.pop(subscription.key, None)
        subscription.token = ""
        subscription.actor = None
        room.wake.set()

    def notify(self, event_id=None):
        if self.closed:
            return
        for room in self.rooms.values():
            if event_id in (None, 0, room.event_id):
                room.change_pending = True
                # A local wake and its committed PG NOTIFY may straddle the
                # same read. Keep the following pass, but start with a cheap
                # version/auth probe instead of fetching duplicate hot rows.
                room.probe_pending |= room.reading
                room.wake.set()
                wake_worker = getattr(room.live, "wake_worker", None)
                if wake_worker:
                    wake_worker()

    async def snapshot(self, token, event_id, envelope):
        room, subscriber = await self.subscribe(token, event_id, envelope)
        try:
            frame = await asyncio.wait_for(subscriber.queue.get(), 10)
            if "error" in frame:
                from .auction_http import TransportError
                error = frame["error"]
                raise TransportError(error["code"], error["message"],
                                     401 if error["reload_required"] else 503, reload=error["reload_required"])
            return {"context": subscriber.context, "epoch": self.runtime.epoch, "panel": frame["panel"]}
        finally:
            self.unsubscribe(room, subscriber)

    def _read(self, room, tokens, changed_hint):
        from .auction_shared import shared_snapshot
        return (self.reader or shared_snapshot)(room.live, room.event_id, tokens,
            base_state=room.state, base_version=room.version, changed_hint=changed_hint)

    @staticmethod
    def _fresh_state(room):
        if room.state is None:
            return None
        from copy import deepcopy
        from .auction_state import age_state
        return age_state(deepcopy(room.state), room.clock, room.clock_at)

    @staticmethod
    def _personal_views(views, state, actor):
        if "teams" not in views or not state:
            return views
        mine = {str(team["id"]) for team in state["teams"]
                if any(player["member_id"] == actor.get("member_id") for player in team["players"])}
        teams = {key: {**card, "mine": key in mine} for key, card in views["teams"].items()}
        return {**views, "teams": teams, "overview": {"kind": "overview", "teams": list(teams.values())}}

    async def _run(self, room):
        try:
            while not self.closed:
                room.wake.clear()
                # Coalesce joins/notifications into one query batch. This is
                # independent of the bid POST/commit/acknowledgment path.
                await asyncio.sleep(0.01)
                if not room.members:
                    try:
                        await asyncio.wait_for(room.wake.wait(), 10)
                        continue
                    except asyncio.TimeoutError:
                        break
                members = list(room.members.values())
                tokens = tuple(dict.fromkeys(member.token for member in members))
                # Consume only the notifications coalesced before this read.
                # All three operations run on the event loop without an await;
                # a notification during the read must wake the following pass.
                changed_hint = room.change_pending and not room.probe_pending
                room.change_pending = False
                room.probe_pending = False
                room.wake.clear()
                try:
                    room.reading = True
                    try:
                        result = await to_thread.run_sync(partial(self._read, room, tokens, changed_hint), limiter=self.runtime.read_limiter)
                    finally:
                        room.reading = False
                    self.stats["reads"] += 1
                    room.verified_at = monotonic()
                    room.clock_at = result.get("sampled_at", room.verified_at)
                    room.clock = float(result["server_now"])
                    changed = result["revision"] != room.revision
                    details = result["detail_revision"] != room.detail_revision
                    display = result.get("display_revision") != room.display_revision
                    room.revision, room.detail_revision = result["revision"], result["detail_revision"]
                    room.display_revision = result.get("display_revision")
                    room.version = {"revision": room.revision, "detail_revision": room.detail_revision,
                                    "display_revision": room.display_revision}
                    if result.get("state") is not None:
                        room.state = result["state"]
                    elif result.get("changed") and result.get("state") is None:
                        room.state = None
                        room.views = {}
                    changed_views = {}
                    if room.state and (details or not room.views):
                        from .auction_components import live_companion_data
                        views = live_companion_data(room.state, {})
                        changed_views = {key: value for key, value in views.items() if room.views.get(key) != value}
                        self.stats["full_states" if display or not room.views else "allocation_states"] += 1
                        room.views = views
                    elif changed and room.state:
                        # Metadata is shared; only bid history and sound change
                        # on an ordinary bid. Formatting happens once per room.
                        from .auction_components import bid_history_data
                        changed_views = bid_history_data(room.state)
                        room.views.update(changed_views)
                        self.stats["hot_states"] += 1
                    if room.state and room.state["status"] in ("RUNNING", "WAITING", "PAUSED"):
                        room.live.ensure_worker()
                    state = self._fresh_state(room)
                    for member in members:
                        if member.key not in room.members:
                            continue
                        actor = result["actors"].get(member.token)
                        if actor is None:
                            self._error(member, "session_expired", "로그인이 만료되었습니다. 다시 로그인해 주세요.", True)
                            member.actor = None
                            continue
                        actor_changed = member.actor != actor
                        member.actor = actor
                        first = not member.initialized
                        initial = first or (actor_changed and not changed)
                        if not (first or changed or actor_changed):
                            continue
                        views = room.views if first or actor_changed else changed_views
                        views = self._personal_views(views, state, actor)
                        panel = presentation(state, actor, member.context, request=member.request if initial else None,
                            elapsed_ms=max(0, (monotonic()-member.joined)*1000) if initial else 0, views=views)
                        panel["transport"].update(revision=room.revision, detail_revision=room.detail_revision)
                        frame = {"type": "snapshot" if initial else "state", "epoch": self.runtime.epoch,
                            "context": member.context, "revision": room.revision, "detail_revision": room.detail_revision,
                            "panel": panel}
                        member.offer(frame)
                        member.initialized = True
                        member.last_revision, member.last_detail = room.revision, room.detail_revision
                        self.stats["frames"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    room.verified_at = 0
                    for member in members:
                        self._error(member, "transport_uncertain", "경매 연결을 확인하고 있습니다.", False)
                if room.wake.is_set():
                    continue
                try:
                    await asyncio.wait_for(room.wake.wait(), self.scan_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self.rooms.get(room.event_id) is room:
                self.rooms.pop(room.event_id, None)

    @staticmethod
    def _error(member, code, message, reload):
        member.offer({"type": "error", "error": {"code": code, "message": message, "reload_required": reload}})

    def pong(self, room, member, sent_ms):
        if not member.actor or not member.initialized or monotonic()-room.verified_at > max(3, self.scan_interval*2):
            return None
        return {"type": "pong", "epoch": self.runtime.epoch, "context": member.context,
                "sent_ms": sent_ms, "server_now": room.clock + max(0, monotonic()-room.clock_at)}

    async def close(self):
        self.closed = True
        tasks = [room.task for room in self.rooms.values() if room.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.listener:
            await to_thread.run_sync(self.listener.stop)
        self.rooms.clear()


async def websocket_endpoint(socket):
    from . import auction_http as http
    room = member = hub = None
    sender = receiver = None
    accepted = False
    try:
        http._check_origin(socket)
        if socket.scope.get("query_string"):
            raise http.TransportError("invalid_connection", "경매 연결 주소를 확인해 주세요.")
        runtime = http.get_runtime()
        if runtime is None or getattr(runtime, "push", None) is None:
            await socket.close(code=1013)
            return
        hub = runtime.push
        await socket.accept()
        accepted = True
        raw = await asyncio.wait_for(socket.receive_text(), 5)
        if len(raw.encode()) > http.MAX_BODY:
            raise http.TransportError("request_too_large", "경매 연결 정보를 확인해 주세요.")
        body = json.loads(raw)
        allowed = {"type", "event_id", "context", "epoch", "seq", "sent_ms", "command", "session_token", "server_epoch"}
        if not isinstance(body, dict) or set(body)-allowed or body.pop("type", None) != "subscribe":
            raise http.TransportError("invalid_connection", "경매 연결 정보를 확인해 주세요.")
        token = http._credentials(socket, body)
        runtime.check_epoch(body.pop("server_epoch", None))
        body.pop("session_token", None)
        room, member = await hub.subscribe(token, body.get("event_id"), body)

        async def send_frames():
            while runtime.active:
                frame = await member.queue.get()
                await asyncio.wait_for(socket.send_json(frame), 3)
                if "error" in frame:
                    return

        async def receive_pings():
            import math
            while runtime.active:
                raw = await asyncio.wait_for(socket.receive_text(), 20)
                if len(raw.encode()) > 256:
                    return
                packet = json.loads(raw)
                if not isinstance(packet, dict) or set(packet)-{"type", "sent_ms"} or packet.get("type") != "ping":
                    return
                sent = packet.get("sent_ms")
                if type(sent) not in (int, float) or not math.isfinite(sent) or sent < 0:
                    return
                pong = hub.pong(room, member, sent)
                if pong:
                    # A single writer avoids interleaving socket sends. Pongs
                    # cannot replace a pending state or terminal auth error.
                    if member.queue.empty():
                        member.queue.put_nowait(pong)

        sender = asyncio.create_task(send_frames())
        receiver = asyncio.create_task(receive_pings())
        await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
    except http.TransportError as error:
        if accepted:
            with suppress(Exception):
                await socket.send_json({"type": "error", "error": {"code": error.code,
                    "message": error.message, "reload_required": error.reload}})
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, TypeError):
        pass
    except Exception:
        # Credentials are intentionally absent from exceptions and logs.
        pass
    finally:
        for task in (sender, receiver):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (sender, receiver) if task), return_exceptions=True)
        if room and member:
            hub.unsubscribe(room, member)
        with suppress(Exception):
            await socket.close(code=1000 if accepted else 1008)
