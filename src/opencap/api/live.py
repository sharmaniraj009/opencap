"""Live values over a WebSocket: `GET /api/v1/ws`, the Quick Client feed (#31, 03 §3.6).

Protocol `opencap.v1`, JSON text frames (schemas `Ws*` in api/openapi.json, `x-websocket`):

    client  {"subscribe": {"devices": ["<channel>/<device>", ...],
                           "tags": [{"device": "<channel>/<device>", "tag": "<name>"}, ...]}}
            Each subscribe replaces the previous one; an empty one stops everything.
    server  {"type": "subscribed", "devices", "tags", "conflate_ms"}     the ack, then per device:
            {"type": "snapshot", "device", "version", "changes": [{tag, value, quality, source_ts}]}
            {"type": "changes",  "device", "version", "changes": [...]}  only what changed since
            {"type": "unavailable", "device", "detail"}                  not polled; stays subscribed
            {"type": "error", "detail"}                                  a message it could not use

Auth: the REST API's tokens and roles, checked before the upgrade is accepted (401/403, no
socket). `Authorization: Bearer <token>`, or, for browsers, which cannot set headers on a
WebSocket, the subprotocol `opencap.bearer.<base64url(token)>` offered next to `opencap.v1`.
The server answers with `opencap.v1` and never echoes the token. Subscribing is a read, so
viewers may. A browser's `Origin` must be the API's own origin or one of `api.cors_origins`:
CORS does not cover WebSockets, so without this check a foreign page could read live values
through a token-less loopback API (cross-site WebSocket hijacking, D-066).

Fan-out and conflation (ADR-0005): one `LiveHub` per app. Every `TICK_S` it reads the change
counter (`poller.live_version`) of each subscribed device. A device whose values changed and
whose last message left at least `CONFLATE_S` ago is diffed once against the state last sent,
and the batch is encoded once for all its subscribers. Each connection has a bounded queue;
a client that falls behind loses its queue and gets fresh snapshots instead of a backlog.
The hub only reads the poller (cache snapshots); nothing here waits on PLC I/O.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

import structlog
from pydantic import ValidationError
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from opencap.api.auth import Auth, AuthError, Principal
from opencap.api.schemas import WsSubscribe

log = structlog.get_logger("opencap.api.live")

PROTOCOL = "opencap.v1"
BEARER_PREFIX = "opencap.bearer."
CONFLATE_S = 0.25            # at most one message per device per 250 ms (and connection)
TICK_S = 0.05                # how often the hub looks at the change counters
MAX_CONNECTIONS = 256
MAX_MESSAGE = 1024 * 1024    # client text frame (uvicorn's ws_max_size is set to the same)
MAX_QUEUE_BYTES = 4 * 1024 * 1024
MAX_SUBSCRIBE_ITEMS = 100_000
# uvicorn settings for the API server (runtime._start_api). permessage-deflate is off: it compresses
# per connection on the poll loop, and with 50 clients x 5,000 changing tags it stalled the loop
# 40-150 ms every second; without it the load test shows no stall (D-066).
UVICORN_WS = {"ws_max_size": MAX_MESSAGE, "ws_per_message_deflate": False}
NOT_POLLED = ("not polled: unknown device, its channel is disabled or not selected, "
              "or the change is not reloaded yet")
SUBSCRIBE_HELP = '{"subscribe": {"devices": ["<channel>/<device>"], "tags": [{"device": "...", "tag": "..."}]}}'


# ------------------------------------------------------------------------------ encoding

def _finite(v: Any) -> Any:
    """JSON has no NaN/Infinity: send them as strings (the proto3 JSON mapping does the same)."""
    if isinstance(v, float) and not math.isfinite(v):
        return "NaN" if math.isnan(v) else ("Infinity" if v > 0 else "-Infinity")
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


def encode(msg: dict[str, Any]) -> str:
    try:
        return json.dumps(msg, separators=(",", ":"), allow_nan=False, default=str)
    except ValueError:
        return json.dumps(_finite(msg), separators=(",", ":"), allow_nan=False, default=str)


# ------------------------------------------------------------------------------ auth

def offered_protocols(ws: WebSocket) -> list[str]:
    raw = ws.headers.get("sec-websocket-protocol", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def bearer_from_protocols(protocols: list[str]) -> str | None:
    """The token of an `opencap.bearer.<base64url>` subprotocol; "" if it does not decode."""
    for p in protocols:
        if p.startswith(BEARER_PREFIX):
            raw = p[len(BEARER_PREFIX):]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return ""
    return None


def origin_allowed(origin: str | None, host: str | None, cors_origins: list[str]) -> bool:
    """No Origin (not a browser), the API's own origin, or a configured CORS origin."""
    if origin is None:
        return True
    origin = origin.strip().rstrip("/")
    if origin in {o.rstrip("/") for o in cors_origins}:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.netloc)
            and parts.netloc.lower() == (host or "").strip().lower())


def authenticate(ws: WebSocket, auth: Auth, cors_origins: list[str]) -> Principal:
    """The caller of a WebSocket handshake, with the REST API's rules. Raises AuthError."""
    headers = ws.headers
    if not origin_allowed(headers.get("origin"), headers.get("host"), cors_origins):
        raise AuthError(403, "this page's origin may not open the live feed (api.cors_origins)")
    protocols = offered_protocols(ws)
    if protocols and PROTOCOL not in protocols:
        raise AuthError(400, f"offer the subprotocol {PROTOCOL}")
    given: dict[str, str] = {}
    if headers.get("authorization"):
        given["authorization"] = headers["authorization"]
    else:
        token = bearer_from_protocols(protocols)
        if token is not None:
            given["authorization"] = "Bearer " + token
    if protocols:
        given["x-opencap-client"] = "ui"     # a browser: the web UI or the desktop shell
    return auth.principal("GET", given, headers.get("host"))


async def deny(ws: WebSocket, status: int, detail: str) -> None:
    """Refuse the handshake: an HTTP answer where the server can send one, else close (403)."""
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    if "websocket.http.response" in ws.scope.get("extensions", {}):
        await ws.send_denial_response(JSONResponse({"detail": detail}, status_code=status, headers=headers))
    else:
        await ws.close(code=1008, reason=detail[:120])


# ------------------------------------------------------------------------------ hub

class _Feed:
    """One subscribed device: the values last sent, shared by every connection."""

    __slots__ = ("device", "clients", "seen", "version", "values", "polled", "last_sent", "_snapshot")

    def __init__(self, device: str) -> None:
        self.device = device
        self.clients: set[_Client] = set()
        self.seen: int | None = None          # live_version last read
        self.version = 0                      # version of `values`
        self.values: dict[str, dict[str, Any]] = {}
        self.polled = False
        self.last_sent = -math.inf
        self._snapshot: tuple[int, str] | None = None

    def snapshot_text(self, names: frozenset[str] | None) -> str:
        if names is None:                     # whole device: encoded once per version
            if self._snapshot is None or self._snapshot[0] != self.version:
                changes = [{"tag": n, **tv} for n, tv in self.values.items()]
                self._snapshot = (self.version, encode({"type": "snapshot", "device": self.device,
                                                        "version": self.version, "changes": changes}))
            return self._snapshot[1]
        msg: dict[str, Any] = {"type": "snapshot", "device": self.device, "version": self.version,
                               "changes": [{"tag": n, **tv} for n, tv in self.values.items() if n in names]}
        missing = sorted(n for n in names if n not in self.values)
        if missing:
            msg["missing"] = missing
        return encode(msg)


class LiveHub:
    """Fan-out of value changes to WebSocket connections, conflated per device."""

    def __init__(self, poller: Any, *, conflate_s: float = CONFLATE_S, tick_s: float = TICK_S,
                 max_queue_bytes: int = MAX_QUEUE_BYTES, max_connections: int = MAX_CONNECTIONS) -> None:
        self.poller = poller
        self.conflate_s = conflate_s
        self.tick_s = tick_s
        self.max_queue_bytes = max_queue_bytes
        self.max_connections = max_connections
        self.clients: set[_Client] = set()
        self.feeds: dict[str, _Feed] = {}
        self.resyncs = 0                      # clients that fell behind and got snapshots (all time)
        self._task: asyncio.Task | None = None

    # --- connections -----------------------------------------------------------------
    def full(self) -> bool:
        return len(self.clients) >= self.max_connections

    def attach(self, client: _Client) -> None:
        self.clients.add(client)
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="api-live")

    def detach(self, client: _Client) -> None:
        self.clients.discard(client)
        self._unsubscribe(client)
        if not self.clients and self._task is not None:
            self._task.cancel()                # the last one left: no tick until the next attach
            self._task = None

    def _unsubscribe(self, client: _Client) -> None:
        for device in client.subs:
            feed = self.feeds.get(device)
            if feed is not None:
                feed.clients.discard(client)
                if not feed.clients:
                    del self.feeds[device]
        client.subs = {}

    def subscribe(self, client: _Client, devices: list[str], tags: dict[str, frozenset[str]]) -> None:
        """Replace the client's subscription; it then gets a snapshot of each device."""
        self._unsubscribe(client)
        subs: dict[str, frozenset[str] | None] = dict.fromkeys(devices)
        subs.update((d, names) for d, names in tags.items() if d not in subs)
        client.subs = subs
        for device in subs:
            feed = self.feeds.get(device)
            if feed is None:
                feed = self.feeds[device] = _Feed(device)
                self._load(feed)
            feed.clients.add(client)
        client.restart()

    # --- reading the poller -------------------------------------------------------------
    def _read(self, device: str) -> tuple[int, dict[str, dict[str, Any]]] | None:
        try:
            version = self.poller.live_version(device)
            if version is None:
                return None
            return version, self.poller.device_values(device)
        except KeyError:
            return None

    def _load(self, feed: _Feed) -> None:
        got = self._read(feed.device)
        if got is None:
            feed.polled, feed.seen, feed.values = False, None, {}
        else:
            feed.polled = True
            feed.seen = feed.version = got[0]
            feed.values = got[1]

    # --- the tick -----------------------------------------------------------------------
    async def _run(self) -> None:
        try:
            while self.clients:
                await asyncio.sleep(self.tick_s)
                try:
                    self.tick(time.monotonic())
                except Exception:  # noqa: BLE001 - keep the feed alive; the next tick retries
                    log.exception("live_tick_failed")
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    def tick(self, now: float) -> None:
        for feed in list(self.feeds.values()):
            try:
                version = self.poller.live_version(feed.device)
            except KeyError:
                version = None
            if version is None:
                if feed.polled:
                    self._gone(feed)
                continue
            if feed.polled and version == feed.seen:
                continue
            if now - feed.last_sent < self.conflate_s:
                continue                      # conflation: the changes wait for the next slot
            self._flush(feed, now)

    def _gone(self, feed: _Feed) -> None:
        feed.polled, feed.seen, feed.values, feed._snapshot = False, None, {}, None
        for client in feed.clients:
            client.resync(feed.device)

    def _flush(self, feed: _Feed, now: float) -> None:
        got = self._read(feed.device)
        if got is None:
            if feed.polled:
                self._gone(feed)
            return
        version, values = got
        feed.seen = version
        if not feed.polled or values.keys() != feed.values.keys():
            # back after a gap, or the tag set changed (reload): everyone gets a snapshot
            feed.polled, feed.version, feed.values, feed.last_sent = True, version, values, now
            feed._snapshot = None
            for client in feed.clients:
                client.resync(feed.device)
            return
        old = feed.values
        changed = [n for n, tv in values.items() if old[n] != tv]
        feed.values, feed.version = values, version
        if not changed:
            return
        feed.last_sent = now
        whole: str | None = None
        for client in list(feed.clients):
            names = client.subs.get(feed.device)
            if names is None:
                if whole is None:
                    whole = encode({"type": "changes", "device": feed.device, "version": version,
                                    "changes": [{"tag": n, **values[n]} for n in changed]})
                client.offer(feed.device, whole)
            else:
                mine = [n for n in changed if n in names]
                if mine:
                    client.offer(feed.device, encode({"type": "changes", "device": feed.device, "version": version,
                                                      "changes": [{"tag": n, **values[n]} for n in mine]}))

    def message_for(self, client: _Client, device: str) -> str | None:
        """What a client waiting on `device` gets now: its snapshot, or `unavailable`."""
        if device not in client.subs:
            return None
        feed = self.feeds.get(device)
        if feed is None:
            return None
        if not feed.polled:
            return encode({"type": "unavailable", "device": device, "detail": NOT_POLLED})
        return feed.snapshot_text(client.subs[device])


class _Client:
    """One connection: its subscription, a bounded queue and the task that sends it."""

    def __init__(self, ws: WebSocket, hub: LiveHub, principal: Principal) -> None:
        self.ws = ws
        self.hub = hub
        self.principal = principal
        self.subs: dict[str, frozenset[str] | None] = {}   # device -> tag names (None = every tag)
        self.control: deque[str] = deque()                 # acks and errors, sent first
        self.pending: dict[str, None] = {}                 # devices whose next message is a snapshot
        self.queue: deque[tuple[str, str]] = deque()       # (device, encoded batch)
        self.queued_bytes = 0
        self.wake = asyncio.Event()
        self.sent = 0
        self.resyncs = 0

    def _queue_full(self, size: int) -> bool:
        return (self.queued_bytes + size > self.hub.max_queue_bytes
                or len(self.queue) >= 16 + 4 * len(self.subs))

    def offer(self, device: str, text: str) -> None:
        if device in self.pending:
            return                            # its snapshot is not sent yet and will hold this
        if self._queue_full(len(text)):
            # fell behind: drop the backlog, send fresh snapshots instead
            self.queue.clear()
            self.queued_bytes = 0
            self.pending = dict.fromkeys(self.subs)
            self.resyncs += 1
            self.hub.resyncs += 1
            log.info("live_client_resync", who=self.principal.name, devices=len(self.subs))
        else:
            self.queue.append((device, text))
            self.queued_bytes += len(text)
        self.wake.set()

    def resync(self, device: str) -> None:
        """The device's next message is a snapshot (or `unavailable`); older batches go."""
        if any(d == device for d, _ in self.queue):
            kept = [(d, t) for d, t in self.queue if d != device]
            self.queue = deque(kept)
            self.queued_bytes = sum(len(t) for _, t in kept)
        self.pending[device] = None
        self.wake.set()

    def restart(self) -> None:
        self.queue.clear()
        self.queued_bytes = 0
        self.pending = dict.fromkeys(self.subs)
        self.wake.set()

    def say(self, msg: dict[str, Any]) -> None:
        self.control.append(encode(msg))
        self.wake.set()

    def _next(self) -> str | None:
        if self.control:
            return self.control.popleft()
        while self.pending:
            device = next(iter(self.pending))
            del self.pending[device]
            text = self.hub.message_for(self, device)
            if text is not None:
                return text
        if self.queue:
            _, text = self.queue.popleft()
            self.queued_bytes -= len(text)
            return text
        return None

    async def run_sender(self) -> None:
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                while (text := self._next()) is not None:
                    await self.ws.send_text(text)
                    self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the peer went away; the receive loop ends too
            log.debug("live_send_failed", who=self.principal.name, error=str(exc))


# ------------------------------------------------------------------------------ endpoint

def parse_subscribe(text: str) -> tuple[list[str], dict[str, frozenset[str]]]:
    """(devices, {device: tag names}) of a subscribe message. Raises ValueError."""
    try:
        sub = WsSubscribe.model_validate_json(text).subscribe
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "message"
        raise ValueError(f"{where}: {first.get('msg', 'invalid')}; expected {SUBSCRIBE_HELP}") from None
    if len(sub.devices) + len(sub.tags) > MAX_SUBSCRIBE_ITEMS:
        raise ValueError(f"at most {MAX_SUBSCRIBE_ITEMS} devices and tags per subscribe")
    devices = list(dict.fromkeys(sub.devices))
    whole = set(devices)
    tags: dict[str, set[str]] = {}
    for ref in sub.tags:
        if ref.device not in whole:
            tags.setdefault(ref.device, set()).add(ref.tag)
    return devices, {d: frozenset(names) for d, names in tags.items()}


async def live_endpoint(ws: WebSocket) -> None:
    ctx = ws.app.state.ctx
    hub: LiveHub = ws.app.state.live
    try:
        principal = authenticate(ws, ctx.auth, ctx.cfg.cors_origins)
    except AuthError as exc:
        await deny(ws, exc.status, exc.message)
        return
    if hub.full():
        await deny(ws, 503, f"too many live connections (at most {hub.max_connections})")
        return
    await ws.accept(subprotocol=PROTOCOL if offered_protocols(ws) else None)
    client = _Client(ws, hub, principal)
    hub.attach(client)
    sender = asyncio.create_task(client.run_sender(), name="api-live-send")
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                client.say({"type": "error", "detail": "send JSON text frames"})
                continue
            if len(text) > MAX_MESSAGE:
                client.say({"type": "error", "detail": f"message larger than {MAX_MESSAGE} bytes"})
                continue
            try:
                devices, tags = parse_subscribe(text)
            except ValueError as exc:
                client.say({"type": "error", "detail": str(exc)})
                continue
            hub.subscribe(client, devices, tags)
            client.say({"type": "subscribed", "devices": devices,
                        "tags": [{"device": d, "tag": t} for d in tags for t in sorted(tags[d])],
                        "conflate_ms": round(hub.conflate_s * 1000)})
    except WebSocketDisconnect:
        pass
    finally:
        hub.detach(client)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
