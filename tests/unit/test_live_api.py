"""Live values over WebSocket, `GET /api/v1/ws` (#31): handshake auth, the protocol, conflation,
slow clients and the message contract (api/openapi.json `x-websocket`).

Protocol tests drive the app through Starlette's test client against the config-backed fake
poller; the hub tests call `LiveHub.tick(now)` with a synthetic clock, so conflation is checked
without wall-clock timing. The real poller with fake drivers is in
tests/integration/test_live_feed.py, the 50-client load run in test_live_load.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from opencap.api import live
from opencap.api.live import LiveHub, _Client, encode, origin_allowed, parse_subscribe
from opencap.api.server import create_app
from opencap.config.models import ApiCfg
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ENV, api_cfg, make_config

DEV = "SITE MODBUS/Device1"
OTHER = "SITE M800/8 PUMP"


def proto(token: str) -> list[str]:
    return ["opencap.v1", "opencap.bearer." + base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")]


VIEWER_P = proto(ENV["T_VIEW"])
ADMIN_P = proto(ENV["T_ADMIN"])


@pytest.fixture
def env(tmp_path):
    poller = ConfigPoller(make_config(tmp_path))
    app = create_app(poller, api_cfg(tmp_path, cors_origins=["http://desk.example:1420"]), environ=ENV)
    return TestClient(app), poller, app


def recv(ws) -> dict[str, Any]:
    return json.loads(ws.receive_text())


def subscribe(ws, devices=(), tags=()) -> list[dict[str, Any]]:
    """Send a subscribe; return the ack and one message per subscribed device."""
    ws.send_text(json.dumps({"subscribe": {"devices": list(devices), "tags": list(tags)}}))
    ack = recv(ws)
    assert ack["type"] == "subscribed", ack
    return [ack] + [recv(ws) for _ in range(len(ack["devices"]) + len({t["device"] for t in ack["tags"]}))]


def denied(c: TestClient, **kw) -> WebSocketDenialResponse:
    with pytest.raises(WebSocketDenialResponse) as info:
        with c.websocket_connect("/api/v1/ws", **kw):
            pass
    return info.value


# --- handshake -----------------------------------------------------------------------------------

def test_token_in_the_subprotocol_or_the_header(env):
    c, _, _ = env
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        assert ws.accepted_subprotocol == "opencap.v1"           # never the token
        assert subscribe(ws, [DEV])[1]["type"] == "snapshot"
    with c.websocket_connect("/api/v1/ws", headers={"Authorization": "Bearer " + ENV["T_ADMIN"]}) as ws:
        assert ws.accepted_subprotocol is None
        assert subscribe(ws, [DEV])[1]["type"] == "snapshot"


def test_bad_or_missing_token_is_refused_before_the_upgrade(env):
    c, _, _ = env
    for kw in ({}, {"subprotocols": ["opencap.v1"]}, {"subprotocols": proto("nope")},
               {"subprotocols": ["opencap.v1", "opencap.bearer.%%%"]},
               {"headers": {"Authorization": "Bearer nope"}}):
        r = denied(c, **kw)
        assert r.status_code == 401, kw
        assert r.headers["www-authenticate"] == "Bearer"
        assert json.loads(r.content)["detail"] == "missing or bad bearer token"
    r = denied(c, subprotocols=["graphql-ws", "opencap.bearer.x"])
    assert r.status_code == 400 and "opencap.v1" in json.loads(r.content)["detail"]


def test_origin_must_be_the_api_or_a_cors_origin(env):
    c, _, _ = env
    r = denied(c, subprotocols=VIEWER_P, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and "origin" in json.loads(r.content)["detail"]
    assert denied(c, subprotocols=VIEWER_P, headers={"Origin": "null"}).status_code == 403
    for origin in ("http://testserver", "http://desk.example:1420"):        # own origin; api.cors_origins
        with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P, headers={"Origin": origin}) as ws:
            assert subscribe(ws, [DEV])[1]["type"] == "snapshot"


def test_origin_rule():
    assert origin_allowed(None, "h:1", [])                                 # not a browser
    assert origin_allowed("http://127.0.0.1:8080", "127.0.0.1:8080", [])
    assert origin_allowed("https://Opencap.example", "opencap.example", [])
    assert origin_allowed("http://a.example/", "b.example", ["http://a.example"])
    assert not origin_allowed("http://127.0.0.1:9999", "127.0.0.1:8080", [])
    assert not origin_allowed("file://", "127.0.0.1:8080", [])
    assert not origin_allowed("http://[::1", "x", [])


def test_open_mode_answers_only_loopback_host_names(tmp_path):
    poller = ConfigPoller(make_config(tmp_path))
    app = create_app(poller, ApiCfg(audit_log=str(tmp_path / "audit.jsonl")), environ={})
    c = TestClient(app)
    local = {"Host": "127.0.0.1:8080"}
    with c.websocket_connect("/api/v1/ws", subprotocols=["opencap.v1"], headers=local) as ws:   # anonymous viewer
        assert subscribe(ws, [DEV])[1]["type"] == "snapshot"
    with c.websocket_connect("/api/v1/ws", subprotocols=["opencap.v1"],
                             headers={**local, "Origin": "http://127.0.0.1:8080"}) as ws:     # the UI itself
        assert subscribe(ws, [DEV])[1]["type"] == "snapshot"
    # a foreign page reaching the loopback API through DNS rebinding sends its own Host
    r = denied(c, subprotocols=["opencap.v1"], headers={"Host": "evil.example", "Origin": "http://evil.example"})
    assert r.status_code == 403
    # ... and a foreign page that connects to 127.0.0.1 directly is stopped by the Origin check
    r = denied(c, subprotocols=["opencap.v1"], headers={**local, "Origin": "http://evil.example"})
    assert r.status_code == 403 and "origin" in json.loads(r.content)["detail"]


def test_connection_cap(env):
    c, _, app = env
    app.state.live.max_connections = 1
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        subscribe(ws, [DEV])
        r = denied(c, subprotocols=VIEWER_P)
        assert r.status_code == 503 and "too many" in json.loads(r.content)["detail"]


# --- protocol --------------------------------------------------------------------------------------

def test_snapshot_then_changes_then_resubscribe(env):
    c, poller, app = env
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        ack, snap, gone = subscribe(ws, [DEV, "NOPE/X"])
        assert ack == {"type": "subscribed", "devices": [DEV, "NOPE/X"], "tags": [], "conflate_ms": 250}
        assert snap["type"] == "snapshot" and snap["device"] == DEV
        assert {ch["tag"] for ch in snap["changes"]} == set(poller.device_values(DEV))
        assert all(set(ch) >= {"tag", "value", "quality", "source_ts"} for ch in snap["changes"])
        assert gone == {"type": "unavailable", "device": "NOPE/X", "detail": live.NOT_POLLED}

        poller.set_tag(DEV, "FLOW_1", 42.5)
        m = recv(ws)
        assert m["type"] == "changes" and m["device"] == DEV and m["version"] > snap["version"]
        assert m["changes"] == [{"tag": "FLOW_1", "value": 42.5, "quality": "good", "source_ts": poller.values[DEV]["FLOW_1"]["source_ts"]}]

        # a new subscribe replaces the old one: snapshots again, nothing more for DEV
        ack, snap2 = subscribe(ws, [OTHER])
        assert snap2["device"] == OTHER
        assert set(app.state.live.feeds) == {OTHER}
        poller.set_tag(DEV, "FLOW_1", 1.5)
        poller.set_tag(OTHER, next(iter(poller.device_values(OTHER))), 7)
        assert recv(ws)["device"] == OTHER
        # an empty subscribe stops everything
        assert subscribe(ws) == [{"type": "subscribed", "devices": [], "tags": [], "conflate_ms": 250}]
    assert app.state.live.feeds == {} and app.state.live.clients == set()


def test_single_tags(env):
    c, poller, _ = env
    names = list(poller.device_values(DEV))
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        ack, snap = subscribe(ws, tags=[{"device": DEV, "tag": names[0]}, {"device": DEV, "tag": "NO_SUCH_TAG"}])
        assert ack["tags"] == [{"device": DEV, "tag": names[0]}, {"device": DEV, "tag": "NO_SUCH_TAG"}]
        assert [ch["tag"] for ch in snap["changes"]] == [names[0]] and snap["missing"] == ["NO_SUCH_TAG"]
        poller.set_tag(DEV, names[1], 5)                  # not subscribed: no message
        poller.set_tag(DEV, names[0], 6)
        m = recv(ws)
        assert [ch["tag"] for ch in m["changes"]] == [names[0]] and m["changes"][0]["value"] == 6


def test_bad_messages_get_an_error_and_the_connection_stays(env):
    c, _, _ = env
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        for bad in ("not json", "[]", '{"subscribe": {"devices": "x"}}', '{"subscribe": {"tags": [{"device": 1}]}}'):
            ws.send_text(bad)
            m = recv(ws)
            assert m["type"] == "error" and '"subscribe"' in m["detail"], bad
        ws.send_bytes(b"\x00")
        assert recv(ws) == {"type": "error", "detail": "send JSON text frames"}
        assert subscribe(ws, [DEV])[1]["type"] == "snapshot"


def test_device_that_stops_and_starts_being_polled(env):
    c, poller, app = env
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        subscribe(ws, [DEV])
        poller.loaded.channels[0].enabled = False                    # as a reload that disables it
        assert poller.live_version(DEV) is None
        assert recv(ws) == {"type": "unavailable", "device": DEV, "detail": live.NOT_POLLED}
        poller.loaded.channels[0].enabled = True
        poller.set_tag(DEV, "FLOW_1", 3.0)
        m = recv(ws)
        assert m["type"] == "snapshot" and {"tag": "FLOW_1", "value": 3.0, "quality": "good",
                                            "source_ts": poller.values[DEV]["FLOW_1"]["source_ts"]} in m["changes"]


def test_parse_subscribe():
    assert parse_subscribe('{"subscribe": {"devices": ["A/B", "A/B"], "tags": [{"device": "A/B", "tag": "T"}, '
                           '{"device": "C/D", "tag": "T"}, {"device": "C/D", "tag": "U"}], "future": 1}}') == \
        (["A/B"], {"C/D": frozenset({"T", "U"})})
    with pytest.raises(ValueError, match="at most"):
        parse_subscribe(json.dumps({"subscribe": {"devices": ["x"] * (live.MAX_SUBSCRIBE_ITEMS + 1)}}))


def test_non_finite_floats_are_valid_json():
    text = encode({"changes": [{"value": math.nan}, {"value": [math.inf, -math.inf, 1.5]}]})
    assert json.loads(text) == {"changes": [{"value": "NaN"}, {"value": ["Infinity", "-Infinity", 1.5]}]}


# --- the hub, with a synthetic clock -----------------------------------------------------------------

class Source:
    """The poller's two live methods, scripted."""

    def __init__(self, devices: dict[str, int]) -> None:
        self.versions = {d: 1 for d in devices}
        self.values = {d: {f"T{i}": {"value": 0, "quality": "good", "source_ts": None} for i in range(n)}
                       for d, n in devices.items()}

    def live_version(self, device: str) -> int | None:
        return self.versions.get(device)

    def device_values(self, device: str) -> dict[str, dict[str, Any]]:
        if device not in self.versions:
            raise KeyError(device)
        return {k: dict(v) for k, v in self.values[device].items()}

    def set(self, device: str, tag: str, value: Any) -> None:
        self.values[device][tag] = {"value": value, "quality": "good", "source_ts": None}
        self.versions[device] += 1


class Peer:
    """Stands in for the WebSocket; the tests pop messages with `drain()`."""


def drain(client: _Client) -> list[dict[str, Any]]:
    out = []
    while (text := client._next()) is not None:
        out.append(json.loads(text))
    return out


def make(devices: dict[str, int], **kw) -> tuple[Source, LiveHub, _Client]:
    src = Source(devices)
    hub = LiveHub(src, **kw)
    client = _Client(Peer(), hub, live.Principal("t", "viewer"))
    hub.subscribe(client, list(devices), {})
    return src, hub, client


def test_conflation_100_updates_in_250_ms_give_one_message():
    src, hub, client = make({"A/B": 5})
    assert [m["type"] for m in drain(client)] == ["snapshot"]
    hub.tick(0.0)
    for i in range(100):                            # a burst between two ticks
        src.set("A/B", f"T{i % 5}", i)
    hub.tick(0.05)
    msgs = drain(client)
    assert len(msgs) == 1 and msgs[0]["type"] == "changes"
    assert {c["tag"]: c["value"] for c in msgs[0]["changes"]} == {"T0": 95, "T1": 96, "T2": 97, "T3": 98, "T4": 99}


def test_conflation_holds_changes_until_the_slot():
    src, hub, client = make({"A/B": 5})
    drain(client)
    sent: list[tuple[float, dict]] = []
    t, i = 0.0, 0
    while t < 1.0:                                   # 100 updates per 250 ms, a tick every 10 ms
        for _ in range(4):
            src.set("A/B", f"T{i % 5}", i)
            i += 1
        t = round(t + 0.01, 3)
        hub.tick(t)
        sent += [(t, m) for m in drain(client)]
    times = [s[0] for s in sent]
    assert 4 <= len(times) <= 5
    assert all(b - a >= 0.25 - 1e-9 for a, b in zip(times, times[1:])), times
    hub.tick(t + 0.3)
    sent += [(t, m) for m in drain(client)]
    final = {}
    for _, m in sent:
        final.update({c["tag"]: c["value"] for c in m["changes"]})
    assert final == {k: tv["value"] for k, tv in src.device_values("A/B").items()}


def test_version_bump_without_a_visible_change_sends_nothing():
    src, hub, client = make({"A/B": 2})
    drain(client)
    src.versions["A/B"] += 1                         # e.g. an update with the same values
    hub.tick(1.0)
    assert drain(client) == []


def test_slow_client_gets_a_fresh_snapshot_instead_of_a_backlog():
    src, hub, client = make({"A/B": 50}, max_queue_bytes=4000)
    drain(client)
    t = 0.0
    for i in range(40):                              # the client never reads
        for k in range(50):
            src.set("A/B", f"T{k}", i)
        t += 0.25
        hub.tick(t)
    assert client.resyncs >= 1
    assert client.queued_bytes <= 4000
    msgs = drain(client)
    snaps = [m for m in msgs if m["type"] == "snapshot"]
    assert len(snaps) == 1 and msgs[0]["type"] == "snapshot"
    assert all(c["value"] == 39 for c in msgs[-1]["changes"])          # ends on the latest values
    assert [m["version"] for m in msgs] == sorted(m["version"] for m in msgs)


def test_resync_drops_older_batches_of_that_device():
    src, hub, client = make({"A/B": 2, "C/D": 2})
    drain(client)
    src.set("A/B", "T0", 1)
    src.set("C/D", "T0", 1)
    hub.tick(1.0)
    src.values["A/B"]["T9"] = {"value": 9, "quality": "good", "source_ts": None}    # tag set changes
    src.versions["A/B"] += 1
    hub.tick(2.0)
    msgs = drain(client)
    assert [(m["type"], m["device"]) for m in msgs] == [("snapshot", "A/B"), ("changes", "C/D")]
    assert {c["tag"] for c in msgs[0]["changes"]} == {"T0", "T1", "T9"}


async def test_the_tick_runs_only_while_clients_are_connected():
    src = Source({"A/B": 1})
    hub = LiveHub(src, tick_s=0.01)
    first = _Client(Peer(), hub, live.Principal("a", "viewer"))
    hub.attach(first)
    task = hub._task
    assert task is not None and not task.done()
    hub.detach(first)
    assert hub._task is None
    second = _Client(Peer(), hub, live.Principal("b", "viewer"))
    hub.attach(second)                       # a new task, not clobbered by the old one ending
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and hub._task is not None and hub._task is not task
    hub.detach(second)
    await asyncio.sleep(0)


def test_one_encoding_per_batch_for_every_subscriber():
    src = Source({"A/B": 3})
    hub = LiveHub(src)
    clients = [_Client(Peer(), hub, live.Principal(f"c{i}", "viewer")) for i in range(5)]
    for cl in clients:
        hub.subscribe(cl, ["A/B"], {})
        drain(cl)
    src.set("A/B", "T1", 5)
    hub.tick(1.0)
    texts = [cl.queue[0][1] for cl in clients]
    assert all(t is texts[0] for t in texts)


# --- the contract ------------------------------------------------------------------------------------

def test_messages_match_the_contract(env):
    import jsonschema

    from tests.unit.test_openapi_contract import SPEC_PATH

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    xws = spec["paths"]["/api/v1/ws"]["get"]["x-websocket"]
    server = {**xws["server_messages"], "components": spec["components"]}
    client_schema = {**xws["client_messages"], "components": spec["components"]}
    c, poller, _ = env
    names = list(poller.device_values(DEV))
    sub = {"subscribe": {"devices": [OTHER, "NOPE/X"], "tags": [{"device": DEV, "tag": names[0]},
                                                                {"device": DEV, "tag": "NO_SUCH"}]}}
    jsonschema.validate(sub, client_schema)
    seen = set()
    with c.websocket_connect("/api/v1/ws", subprotocols=VIEWER_P) as ws:
        ws.send_text(json.dumps(sub))
        msgs = [recv(ws) for _ in range(4)]
        poller.set_tag(DEV, names[0], math.nan)
        msgs.append(recv(ws))
        ws.send_text("{}")
        msgs.append(recv(ws))
    for m in msgs:
        jsonschema.validate(m, server)
        seen.add(m["type"])
    assert seen == {"subscribed", "snapshot", "unavailable", "changes", "error"}
    assert msgs[4]["changes"][0]["value"] == "NaN"
