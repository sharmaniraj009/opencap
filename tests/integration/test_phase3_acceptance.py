"""PLAN.md Phase 3 acceptance in simulation: Allen-Bradley SLC 500 / MicroLogix data tables
(`ab_pccc`) through the whole engine (channel workers, cache, publisher, MQTT, API, CLI), against
the PCCC simulator (tests/helpers/pccc_sim.py), which stock pycomm3 `SLCDriver` talks to.

- 12 PLCs: MicroLogix 1400, SLC 5/05 and MicroLogix 1100 file-directory layouts, F/N/B tables,
  word/bit and bit-number forms, N words typed float32 (docs/phase3_contracts.md);
- every device publishes correct values; a tag in a file the PLC does not have is rejected by
  the file directory (bad_config, BadTags) and never sent, and a MicroLogix RTC function-file
  tag is bad_config at plan time;
- the file directory is read once per connection, not on every scan;
- values follow the PLC; powering off one PLC demotes only that device, and it resumes after
  power-on;
- `identity` reports the processor type and the file directory; `device.stats` logs cycle times;
- nothing but PCCC reads reaches any PLC (no write attempt).

Engine-agnostic (ADR-0014 §3): the engine is driven only through its config directory, its
API, its CLI, its log and the wire, so `--engine=rust` runs the same scenario against the Rust
binary once it implements `ab_pccc` (until then: skipped, "driver not ported").

The real-shaped run (the 30 real PCCC devices with their real tag lists, 10,298 tags) needs the
private export: tests/integration/test_ab_pccc_sim.py::test_real_project_device and D-027.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

pccc_sim = pytest.importorskip("tests.helpers.pccc_sim")      # needs pycomm3 (the `ab` extra)

from opencap.config.models import Device, Tag  # noqa: E402
from opencap.logsetup import configure_logging  # noqa: E402
from tests.helpers.broker import TestBroker  # noqa: E402
from tests.helpers.configdir import write_config  # noqa: E402
from tests.helpers.modbus_sim import free_port  # noqa: E402
from tests.helpers.netcut import stable_port  # noqa: E402
from tests.integration.test_ab_pccc_sim import DIRECTORY_READ, SYSTEM_FILES, TAGS, TYPED_READ, expected  # noqa: E402

pytestmark = [pytest.mark.acceptance, pytest.mark.drivers("ab_pccc")]

PcccSim = pccc_sim.PcccSim
N_DEVICES = 12
INTERVAL_MS = 2000
PRODUCTS = ["1766-L32BXB B/14.00", "1747-L551/C 5/05", "1763-L16BWA B/12.00"]   # ML1400, SLC 5/05, ML1100
GHOST = Tag(name="ghost", address="N99:0", dtype="uint16")          # no file 99 in the PLC
RTC = Tag(name="clock_hour", address="RTC:0.HR", dtype="uint16")    # function file pycomm3 cannot read
VICTIM = 4


def _tags(i: int) -> list[Tag]:
    """TAGS (every address form the driver decodes) plus two longer tables, ~70 tags a PLC."""
    extra = [Tag(name=f"flow_{k}", address=f"F20:{k}", dtype="float32") for k in range(30)]
    extra += [Tag(name=f"count_{k}", address=f"N21:{k}", dtype="int16" if k % 2 else "uint16") for k in range(30)]
    return TAGS + extra


def _device(i: int, sim, tags: list[Tag]) -> Device:
    site = f"SLC_SITE_{i:02d}"
    return Device(id=f"{site}/PLC", channel=site, name="PLC", driver="ab_pccc", host="127.0.0.1", port=sim.port,
                  scan_ms=1000, min_scan_ms=1000, timeout_ms=1000, retries=1, demote_after=3,
                  demote_for_ms=2000, tags=tags)


def _matches(got, want) -> bool:
    if isinstance(want, float):
        return math.isclose(float(got), round(want, 4), rel_tol=1e-6, abs_tol=1e-4)
    return got == want


def _record_files_asked(sim) -> set[int]:
    """File numbers of the typed reads that reach `sim` (on the wire, whatever engine sends them)."""
    asked: set[int] = set()
    real = sim._typed_read

    def spy(body: bytes, *, three_fields: bool) -> bytes:
        asked.add(pccc_sim._address_field(body, 1)[0])
        return real(body, three_fields=three_fields)

    sim._typed_read = spy
    return asked


async def _wait(pred, timeout: float, what: str, step: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(step)


async def test_phase3_acceptance(tmp_path, engine_factory):
    configure_logging("INFO")            # device.stats is an INFO line; the filter must let it through
    sims = []
    try:
        for i in range(N_DEVICES):
            tables = {**SYSTEM_FILES, **pccc_sim.tables_for(_tags(i), seed=300 + i)}
            sims.append(await PcccSim.start(tables, product_name=PRODUCTS[i % len(PRODUCTS)], port=stable_port()))
        devices = [_device(i, sim, _tags(i) + ([GHOST, RTC] if i == 0 else [])) for i, sim in enumerate(sims)]
        asked = _record_files_asked(sims[0])
        async with asyncio.timeout(240), TestBroker() as broker, broker.collector("opencap/#") as col:
            await col.wait_ready()
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=free_port(),
                                      poller={"stats_interval_s": 3, "log_level": "INFO"},
                                      publish={"interval_ms": INTERVAL_MS})
            engine = engine_factory(config_dir)     # the Python Poller, or the Rust engine (--engine=rust)
            await engine.start()
            try:
                await _scenario(tmp_path, config_dir, engine, col, sims, devices)
            finally:
                await engine.stop()
            stats = engine.log_events("device.stats")
            assert {e["device"] for e in stats} >= {d.id for d in devices}, "every device logged its cycle time"
            assert all(e["mean_cycle_ms"] > 0 for e in stats)
        assert [s.write_attempts for s in sims] == [0] * N_DEVICES, "a write reached a PLC"
        assert 99 not in asked and {7, 8, 20, 21} <= asked, sorted(asked)    # the ghost was never sent
    finally:
        await asyncio.gather(*(s.stop() for s in sims), return_exceptions=True)


async def _scenario(tmp_path, config_dir, engine, col, sims, devices) -> None:
    loaded = {d.id: d for c in engine.loaded.channels for d in c.devices}
    topics = {d.id: loaded[d.id].publish.topic for d in devices}

    # 1. Every device publishes correct values; the ghost and the RTC tag are reported, nothing else.
    await _wait(lambda: all(len(col.topic(t)) >= 2 for t in topics.values()), 40, "2 messages per device")
    for i, (dev, sim) in enumerate(zip(devices, sims)):
        msg = col.last(topics[dev.id]).json()
        want_bad = sorted([GHOST.name, RTC.name]) if i == 0 else []
        assert msg.get("BadTags", []) == want_bad, (dev.id, msg.get("BadTags"))
        assert msg["DataValid"] is (not want_bad), dev.id
        for tag in dev.tags:
            if tag.name in want_bad:
                continue
            assert _matches(msg["Data"][tag.name], expected(sim, tag)), (dev.id, tag.name)
    values = engine.device_values(devices[0].id)
    assert values[GHOST.name]["quality"] == "bad_config" and values[RTC.name]["quality"] == "bad_config"

    # 2. The file directory is read once per connection; every scan is typed reads only.
    for s in sims:
        s.reset_counters()
    await asyncio.sleep(2.5)
    assert all(s.pccc_counts[TYPED_READ] > 0 for s in sims), [s.pccc_counts[TYPED_READ] for s in sims]
    assert all(s.pccc_counts[DIRECTORY_READ] == 0 for s in sims), [s.pccc_counts[DIRECTORY_READ] for s in sims]
    assert set().union(*(s.pccc_counts for s in sims)) <= {TYPED_READ}, [dict(s.pccc_counts) for s in sims]

    # 3. Values follow the PLC: a MicroLogix 1100 float and an SLC 5/05 bit-number bit.
    for idx, address, name, value in ((2, "F8:1", "level", 12.5), (1, "B3/33", "bitnum_33", None)):
        if value is None:
            value = not sims[idx].value(address)
        sims[idx].set(address, value)
        await _wait(lambda: col.last(topics[devices[idx].id]).json()["Data"].get(name) == value,
                    3 * INTERVAL_MS / 1000 + 2, f"{devices[idx].id} {name} = {value}")

    # 4. Power off one PLC: only it is demoted; it resumes after power-on.
    victim = devices[VICTIM].id
    await sims[VICTIM].stop()
    await _wait(lambda: engine.device_detail(victim)["state"] == "demoted", 30, "victim demoted")
    others = {d.id: engine.device_detail(d.id)["state"] for d in devices if d.id != victim}
    assert set(others.values()) == {"ok"}, {k: v for k, v in others.items() if v != "ok"}
    await sims[VICTIM].restart()
    await _wait(lambda: engine.device_detail(victim)["state"] == "ok", 30, "victim resumed")
    await _wait(lambda: col.last(topics[victim]).json()["DataValid"] is True, 3 * INTERVAL_MS / 1000 + 2,
                "victim valid data again")

    # 5. `identity` reports the processor type and the file directory for each layout.
    for idx in range(len(PRODUCTS)):
        code, out = await engine.cli("identity", devices[idx].id, "--config", str(config_dir), cwd=tmp_path)
        assert code == 0, out
        ident = json.loads(out[out.index("{"):])
        sim = sims[idx]
        assert ident["product_name"] == sim.product_name, ident
        assert ident["processor_type"] == sim.processor_type, ident
        assert ident["file_directory"].startswith("ok"), ident
        assert ident["files"] == sim.files, ident
        print(f"[phase3] identity {devices[idx].id}: product={ident['product_name']!r} "
              f"processor={ident['processor_type']!r} directory={ident['file_directory']!r}")
