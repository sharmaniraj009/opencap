"""Inter-device delay, connect timeout and inter-request delay (#105, CH-05, DEV-16, DEV-17).

Wall-clock checks use delays of a few hundred ms and bounds of 60 ms slack in the "not earlier than"
direction (Windows ticks `time.monotonic` every ~15.6 ms and asyncio timers can fire a tick early), and
generous bounds in the other. Durations are taken from the fake driver's own monotonic timestamps.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import ClassVar

import pytest
from pydantic import ValidationError

from opencap.config import kep_import as ki
from opencap.config.models import Channel, Device, Tag
from opencap.core.cache import ValueCache
from opencap.core.channel_worker import ChannelWorker, WireLock
from opencap.core.registry import Registry, effective_connect_timeout_ms
from tests.helpers.fake_driver import FakeDriver, FakeFactory, channel, device, fake_plans, tags, uniq
from tests.unit.test_kep_import import SAMPLE

pytestmark = pytest.mark.usefixtures("no_gc")

SLACK = 0.06      # a timer may fire this much early (coarse clocks) without failing a "not before" bound


# --- config ----------------------------------------------------------------------------------------

def _dev(**kw) -> dict:
    return {"id": "S/P", "channel": "S", "name": "P", "driver": "modbus_tcp", "host": "127.0.0.1", "port": 502,
            "tags": [], **kw}


def test_timing_fields_default_to_todays_behaviour() -> None:
    d = Device.model_validate(_dev())
    assert (d.connect_timeout_ms, d.inter_request_delay_ms) == (None, 0)
    assert Channel(name="S", devices=[d]).inter_device_delay_ms == 0


@pytest.mark.parametrize("field,ok,bad", [
    ("connect_timeout_ms", (1000, 30_000), (999, 30_001)),
    ("inter_request_delay_ms", (0, 300_000), (-1, 300_001)),
])
def test_device_timing_ranges(field: str, ok: tuple, bad: tuple) -> None:
    for v in ok:
        assert getattr(Device.model_validate(_dev(**{field: v})), field) == v
    for v in bad:
        with pytest.raises(ValidationError, match=field):
            Device.model_validate(_dev(**{field: v}))


def test_channel_timing_range() -> None:
    assert Channel(name="S", inter_device_delay_ms=60_000, devices=[]).inter_device_delay_ms == 60_000
    for v in (-1, 60_001):
        with pytest.raises(ValidationError, match="inter_device_delay_ms"):
            Channel(name="S", inter_device_delay_ms=v, devices=[])


# --- pacing, no clocks -------------------------------------------------------------------------------

def test_pace_inter_device_only_when_the_device_changes() -> None:
    w = WireLock()
    assert w.pace_s("A", 0.5, 0.0, now=10.0) == 0          # nothing sent yet
    w.last_device, w.last_end, w.device_end = "A", 10.0, {"A": 10.0}
    assert w.pace_s("A", 0.5, 0.0, now=10.1) == 0          # same device again: no inter-device delay
    assert w.pace_s("B", 0.5, 0.0, now=10.1) == pytest.approx(0.4)
    assert w.pace_s("B", 0.5, 0.0, now=11.0) == 0          # the line was idle long enough
    assert w.pace_s("B", 0.0, 0.0, now=10.1) == 0          # 0 = no delay


def test_pace_inter_request_is_per_device_and_takes_the_longer_wait() -> None:
    w = WireLock()
    w.last_device, w.last_end, w.device_end = "B", 10.0, {"A": 9.0, "B": 10.0}
    assert w.pace_s("A", 0.0, 2.0, now=10.0) == pytest.approx(1.0)      # A's last request ended at 9.0
    assert w.pace_s("A", 0.5, 2.0, now=10.0) == pytest.approx(1.0)      # inter-device (0.5) < inter-request (1.0)
    assert w.pace_s("A", 1.5, 2.0, now=10.0) == pytest.approx(1.5)
    assert w.pace_s("C", 0.0, 2.0, now=10.0) == 0                       # C never sent: no wait


# --- the worker, real time --------------------------------------------------------------------------

def _worker(ch, factory):
    return ChannelWorker(ch, fake_plans(ch), ValueCache(), driver_factory=factory, start_jitter=0.0)


async def _gaps(factory: FakeFactory, ids: list[str]) -> list[tuple[str, str, float]]:
    """(previous device, next device, idle time between the end of one read and the start of the next)."""
    spans = sorted((start, end, did) for did in ids for _k, start, end in factory.scripts[did].read_spans)
    return [(a[2], b[2], b[0] - a[1]) for a, b in zip(spans, spans[1:])]


async def test_inter_device_delay_keeps_the_line_idle_between_devices() -> None:
    name = uniq()
    devs = [device(name, f"D{i}", tags("A", 1)) for i in range(2)]
    ch = channel(name, devs)
    ch.inter_device_delay_ms = 250
    factory = FakeFactory()
    w = _worker(ch, factory)
    try:
        await asyncio.gather(w.read_once(devs[0].id), w.read_once(devs[1].id))
        await w.read_once(devs[1].id)                       # same device twice in a row: no delay
        await w.read_once(devs[1].id)
    finally:
        await w.stop()
    gaps = await _gaps(factory, [d.id for d in devs])
    switches = [g for g in gaps if g[0] != g[1]]
    same = [g for g in gaps if g[0] == g[1]]
    assert switches and same
    assert all(g[2] >= 0.25 - SLACK for g in switches), gaps
    assert all(g[2] < 0.25 - SLACK for g in same), gaps


async def test_inter_request_delay_waits_after_the_devices_previous_request_and_holds_the_channel() -> None:
    name = uniq()
    slow = device(name, "SLOW", tags("A", 1) + tags("B", 1), inter_request_delay_ms=300)
    other = device(name, "FAST", tags("C", 1))
    ch = channel(name, [slow, other])
    factory = FakeFactory()
    w = _worker(ch, factory)
    try:
        await asyncio.gather(w.read_once(slow.id), w.read_once(other.id))
    finally:
        await w.stop()
    spans = {did: sorted((s, e, k) for k, s, e in factory.scripts[did].read_spans) for did in (slow.id, other.id)}
    (a_start, a_end, _), (b_start, b_end, _) = spans[slow.id]
    assert b_start - a_end >= 0.3 - SLACK
    # the pause holds the channel: FAST's request cannot start inside it, so it comes before A or after B
    (c_start, c_end, _), = spans[other.id]
    assert not (a_end - 0.005 < c_start < b_start - 0.005)
    everything = sorted(spans[slow.id] + spans[other.id])
    assert all(x[1] <= y[0] + 0.005 for x, y in zip(everything, everything[1:]))   # one request on the wire


async def test_zero_delays_add_nothing() -> None:
    name = uniq()
    devs = [device(name, f"D{i}", tags("A", 1)) for i in range(2)]
    ch = channel(name, devs)
    factory = FakeFactory()
    w = _worker(ch, factory)
    t0 = time.perf_counter()
    try:
        await asyncio.gather(w.read_once(devs[0].id), w.read_once(devs[1].id))
    finally:
        await w.stop()
    assert time.perf_counter() - t0 < 0.5


# --- connect timeout ---------------------------------------------------------------------------------

class _SeesTimeout(FakeDriver):
    """Records the `timeout_ms` the driver sees during connect and during a read."""

    seen: ClassVar[dict[str, list[int]]] = {"connect": [], "read": []}

    async def connect(self) -> None:
        self.seen["connect"].append(self.device.timeout_ms)
        await super().connect()

    async def read(self, block):   # type: ignore[override]
        self.seen["read"].append(self.device.timeout_ms)
        return await super().read(block)


class _SeesFactory(FakeFactory):
    def __call__(self, device_, executor):
        drv = _SeesTimeout(device_, executor, script=self.script(device_.id), probes=self.probes)
        self.drivers[device_.id] = drv
        return drv


async def test_connect_timeout_applies_to_the_connect_only() -> None:
    name = uniq()
    dev = device(name, "PLC", tags("A", 1), timeout_ms=100, connect_timeout_ms=5000)
    ch = channel(name, [dev])
    _SeesTimeout.seen = {"connect": [], "read": []}
    w = _worker(ch, _SeesFactory())
    try:
        await w.read_once(dev.id)
    finally:
        await w.stop()
    assert _SeesTimeout.seen == {"connect": [5000], "read": [100]}


async def test_connect_timeout_bounds_a_black_holed_connect() -> None:
    name = uniq()
    dev = device(name, "PLC", tags("A", 1), timeout_ms=3000, connect_timeout_ms=1000)
    ch = channel(name, [dev])
    factory = FakeFactory()
    factory.script(dev.id).connect_delay_s = 30            # never answers
    w = _worker(ch, factory)
    t0 = time.perf_counter()
    try:
        await w.read_once(dev.id)
    finally:
        await w.stop()
    elapsed = time.perf_counter() - t0
    assert 1.0 - SLACK <= elapsed < 2.5, elapsed           # 1 s x 1.25 + 0.1 s safety net, not 3 s x 1.25 + 0.1 s


def test_effective_connect_timeout() -> None:
    base = Device.model_validate(_dev(timeout_ms=2000))
    assert effective_connect_timeout_ms(base) == 2000                              # derived from timeout_ms
    assert effective_connect_timeout_ms(base.model_copy(update={"connect_timeout_ms": 7000})) == 7000
    s7 = base.model_copy(update={"driver": "s7"})
    assert effective_connect_timeout_ms(s7) == 7000                                # the driver's own: 3 x 2 s + 1 s
    udp = base.model_copy(update={"driver": "melsec_mc", "params": {"protocol": "udp"},
                                  "connect_timeout_ms": 5000})
    assert effective_connect_timeout_ms(udp) is None                               # UDP ignores it


# --- opencap check -----------------------------------------------------------------------------------

def test_check_estimate_includes_both_delays() -> None:
    name = uniq()
    real = [Tag(name="T", address="400001", dtype="uint16")]
    a = device(name, "A", real, inter_request_delay_ms=40)
    b = device(name, "B", real)
    ch = channel(name, [a, b])
    ch.inter_device_delay_ms = 10
    plans = Registry.build([ch]).plans
    pa, pb = plans[a.id], plans[b.id]
    assert pa.wire_requests == 1
    assert pa.request_delay_ms(10, True) == 50 and pb.request_delay_ms(10, True) == 10
    assert pa.est_cycle_ms(60, 50) == 110
    assert pa.est_cycle_ms(60) == 60
    assert pa.request_delay_ms(10, False) == 40            # alone on the channel: no inter-device delay


# --- import-kep ---------------------------------------------------------------------------------------

def test_import_kep_maps_the_three_properties(tmp_path) -> None:
    project = ki._load_project(SAMPLE)
    ch = next(c for c in project["channels"] if c.get("devices") and c.get(ki.K_DRIVER) == "Modbus TCP/IP Ethernet")
    ch[ki.K_INTER_DEVICE_MS] = 25
    dev = ch["devices"][0]
    dev[ki.K_CONNECT_TIMEOUT_S] = 4
    dev[ki.K_INTER_REQUEST_MS] = 120
    if len(ch["devices"]) > 1:
        ch["devices"][1][ki.K_CONNECT_TIMEOUT_S] = 99            # out of range: clamped, with a note
        ch["devices"][1][ki.K_INTER_REQUEST_MS] = 999_999
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"project": project}), encoding="utf-8")
    result = ki.import_kep(path)
    out = next(c for c in result.devices_file.channels if c.name == str(ch[ki.K_NAME]))
    assert out.inter_device_delay_ms == 25
    first = next(d for d in out.devices if d.name == str(dev[ki.K_NAME]))
    assert (first.connect_timeout_ms, first.inter_request_delay_ms) == (4000, 120)
    if len(ch["devices"]) > 1:
        second = next((d for d in out.devices if d.name == str(ch["devices"][1][ki.K_NAME])), None)
        if second is not None:
            assert (second.connect_timeout_ms, second.inter_request_delay_ms) == (30_000, 300_000)
            assert any("outside" in p for p in result.problems)


def test_import_kep_without_the_properties_keeps_the_defaults() -> None:
    result = ki.import_kep(SAMPLE)
    for c in result.devices_file.channels:
        assert c.inter_device_delay_ms == 0
        assert all(d.connect_timeout_ms is None and d.inter_request_delay_ms == 0 for d in c.devices)
