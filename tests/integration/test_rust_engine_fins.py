"""Omron FINS in the Rust engine (#54, Rust-first: there is no Python FINS driver, ADR-0002)
against the FINS simulator (tests/helpers/fins_sim.py, docs/protocols/omron-fins.md §6).

Values are checked against the simulator's own encoder and parser (`sim.value()`), never a
driver's. Every case runs over FINS/UDP and FINS/TCP:

- every address form (DM, CIO bits, W/H/A, EM banks and the current bank, timers and counters
  in BCD, completion and task flags, IR, DR, BCD suffix, 32-bit values, strings, arrays) reads
  the simulator's values, scattered tags go in one MULTIPLE MEMORY AREA READ (0104);
- `check -v` shows the plan and the reasons for rejected addresses; `identity` the CPU model;
- fault switches: a silent CPU, an end code for one area (only its tags become bad_config), a
  transient end code, replies with another SID, a wrong destination node, split TCP replies,
  a CPU that refuses 0104 (the driver falls back to 0101);
- the whole engine (acceptance, Rust only): values follow the PLC, a power-off demotes the
  device and it resumes on the same port; nothing but reads ever reaches the simulator.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
from collections.abc import Callable

import pytest

from opencap.config.models import Device, Tag
from tests.helpers import fins_sim
from tests.helpers.configdir import write_config
from tests.helpers.engine import rust_binary
from tests.helpers.fins_sim import FinsSim
from tests.helpers.modbus_sim import free_port
from tests.helpers.netcut import stable_port

BIN = rust_binary()
needs_rust = pytest.mark.skipif(BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)")
TRANSPORTS = pytest.mark.parametrize("transport", ["udp", "tcp"])

# (tag name, address, dtype); the expected value is sim.value(address, sim_dtype)
TAGS: list[tuple[str, str, str]] = [
    ("D100", "D100", "uint16"), ("D101", "D101", "int16"), ("D102_F", "D102", "float32"),
    ("D104_L", "D104", "int32"), ("D106_DW", "D106", "uint32"),
    ("D110_BCD", "D110D", "uint16"), ("D111_SBCD", "D111D", "int16"), ("D112_LBCD", "D112D", "uint32"),
    ("CIO10_05", "CIO10.05", "bool"), ("CIO10_15", "CIO10.15", "bool"),
    ("W3", "W3", "uint16"), ("H5", "H5", "int16"), ("A100", "A100", "uint16"),
    ("EM0_100", "E00:00100", "uint16"), ("EM1_10", "E01:00010", "float32"), ("E_CUR", "E00200", "uint16"),
    ("T5", "T5", "uint16"), ("C7", "C7", "uint16"), ("TS5", "TS5", "bool"), ("CS7", "CS7", "bool"),
    ("TK1", "TK1", "bool"), ("IR2", "IR2", "uint32"), ("DR3", "DR3", "uint16"),
    ("S_H", "D200.10H", "string"), ("S_L", "D210.6L", "string"), ("S_D", "D220.5D", "string"),
    ("S_E", "D230.4E", "string"), ("ARR", "D300[4]", "uint16"), ("ARR2", "D310[2][3]", "int16"),
    # scattered single words: one 0104 instead of one 0101 each
    ("D500", "D500", "uint16"), ("D900", "D900", "uint16"), ("CIO100", "CIO100", "uint16"), ("W200", "W200", "uint16"),
]
BAD = [("BAD_SYNTAX", "E0_100", "uint16"), ("BAD_RANGE", "D32768", "uint16"), ("BAD_IR16", "IR0", "uint16")]


def _sim_dtype(address: str, dtype: str) -> str:
    return "bcd16" if re.fullmatch(r"[TC]\d+", address) else dtype


def _load(sim: FinsSim) -> None:
    """Deterministic values; BCD words get BCD-valid values, timers and counters BCD PVs."""
    sim.fill([(a, d) for _, a, d in TAGS if not a.endswith("D") and not re.fullmatch(r"[TC]\d+", a)], seed=54)
    sim.set("D110D", 1234, "uint16")
    sim.set("D111D", -50, "int16")
    sim.set("D112D", 12345678, "uint32")
    sim.set("T5", 750, "bcd16")
    sim.set("C7", 42, "bcd16")
    sim.set("TS5", True)
    sim.set("CS7", True)


def _expected(sim: FinsSim) -> dict[str, object]:
    return {name: sim.value(addr, _sim_dtype(addr, dtype)) for name, addr, dtype in TAGS}


def _device(sim: FinsSim, transport: str, *, tags=TAGS, **params) -> Device:
    return Device(id="FINS_SITE/CJ1", channel="FINS_SITE", name="CJ1", driver="omron_fins", host="127.0.0.1",
                  port=sim.port, timeout_ms=400, retries=0,
                  params={"transport": transport, "dest_node": sim.node, "model": "CJ1", **params},
                  tags=[Tag(name=n, address=a, dtype=d) for n, a, d in tags])


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(str(BIN), *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    return proc.returncode, out.decode(), err.decode()


def _read_lines(out: str) -> dict[str, tuple[str, str]]:
    """`read` output -> {tag: (quality, Python repr of the value)}."""
    rows = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+) +(good|bad_comm|bad_config|bad_decode|stale) +(.*)$", line)
        if m:
            rows[m[1]] = (m[2], m[3].strip())
    return rows


async def _read(tmp_path, dev: Device) -> tuple[int, dict[str, tuple[str, str]], str]:
    config_dir = write_config(tmp_path, [dev])
    code, out, err = await _run("read", dev.id, "-c", str(config_dir))
    return code, _read_lines(out), out + err


@pytest.fixture()
async def sim():
    s = await FinsSim.start()
    _load(s)
    yield s
    await s.stop()


@needs_rust
@TRANSPORTS
async def test_every_address_form_reads_the_simulator(tmp_path, sim, transport):
    code, rows, text = await _read(tmp_path, _device(sim, transport))
    assert code == 0, text
    want = _expected(sim)
    assert set(rows) == set(want), text
    for name, value in want.items():
        assert rows[name] == ("good", repr(value)), (name, rows[name], value)
    # contiguous words merge into a few 0101 reads; every single-element range (scattered words,
    # flags, IR, DR, timers, counters) shares one 0104: 33 tags in 5 requests
    assert sim.command_counts[fins_sim.CMD_MULTI_READ] == 1
    assert 1 <= sim.command_counts[fins_sim.CMD_READ] <= 4, sim.command_counts
    assert sim.write_attempts == 0 and sim.misaddressed == 0
    if transport == "tcp":
        assert sim.handshakes, "the FINS/TCP node address handshake ran"


@needs_rust
async def test_check_shows_the_plan_and_the_rejected_addresses(tmp_path, sim):
    config_dir = write_config(tmp_path, [_device(sim, "udp", tags=TAGS + BAD)])
    code, out, err = await _run("check", "-c", str(config_dir), "-v")
    assert code == 0, out + err
    assert "0104 " in out and "D500 (1 word)" in out, out
    for name, _, _ in BAD:
        assert f"bad_config {name}:" in out, out
    assert re.search(r"omron_fins\s+\d+\s+3 ", out), out


@needs_rust
@TRANSPORTS
async def test_identity_reads_the_cpu_unit_data(tmp_path, sim, transport):
    config_dir = write_config(tmp_path, [_device(sim, transport)])
    code, out, err = await _run("identity", "FINS_SITE/CJ1", "-c", str(config_dir))
    assert code == 0 and fins_sim.DEFAULT_MODEL in out and fins_sim.DEFAULT_VERSION in out, out + err
    assert sim.command_counts[fins_sim.CMD_CPU_DATA] == 1


@needs_rust
@TRANSPORTS
async def test_fault_switches(tmp_path, sim, transport):
    dev = _device(sim, transport)
    # a silent CPU: every tag bad_comm
    sim.mute = True
    code, rows, text = await _read(tmp_path, dev)
    assert code == 3 and {q for q, _ in rows.values()} == {"bad_comm"}, text
    sim.mute = False
    # one area refused for good (1101): only its tags are bad_config, also W200 inside the 0104
    sim.area_end_codes = {"W": fins_sim.END_AREA}
    code, rows, text = await _read(tmp_path, dev)
    assert code == 3, text
    assert {n for n, (q, _) in rows.items() if q != "good"} == {"W3", "W200"}, text
    assert {rows["W3"][0], rows["W200"][0]} == {"bad_config"}, text
    sim.area_end_codes = {}
    # wrong CPU mode (2201) is transient: bad_comm
    sim.end_code = 0x2201
    code, rows, text = await _read(tmp_path, dev)
    assert code == 3 and {q for n, (q, _) in rows.items() if n not in ("TS5", "CS7", "TK1")} <= {"bad_comm"}, text
    sim.end_code = 0
    # replies with another SID are never taken for this request's: no reply, bad_comm
    sim.sid_offset = 1
    code, rows, text = await _read(tmp_path, dev)
    assert code == 3 and {q for q, _ in rows.values()} == {"bad_comm"}, text
    sim.sid_offset = 0
    # back to normal, and a TCP reply split over two segments is reassembled
    sim.split_replies = transport == "tcp"
    code, rows, text = await _read(tmp_path, dev)
    assert code == 0 and {q for q, _ in rows.values()} == {"good"}, text
    assert sim.write_attempts == 0


@needs_rust
async def test_a_wrong_destination_node_is_never_answered(tmp_path, sim):
    code, rows, text = await _read(tmp_path, _device(sim, "udp", dest_node=sim.node + 1))
    assert code == 3 and {q for q, _ in rows.values()} == {"bad_comm"}, text
    assert sim.misaddressed > 0


@needs_rust
@TRANSPORTS
async def test_a_cpu_without_0104_is_read_with_0101(tmp_path, sim, transport, monkeypatch):
    def refuse(data: bytes) -> bytes:
        raise fins_sim._Fault(fins_sim.END_UNSUPPORTED)

    monkeypatch.setattr(sim, "_multiple_memory_area_read", refuse)
    code, rows, text = await _read(tmp_path, _device(sim, transport))
    assert code == 0, text
    assert rows["D900"] == ("good", repr(sim.value("D900", "uint16")))
    assert sim.command_counts[fins_sim.CMD_MULTI_READ] == 1 and sim.error_replies[fins_sim.END_UNSUPPORTED] == 1


# --- the whole engine (Rust only: no Python FINS driver) ------------------------------------------

def _close(got, want) -> bool:
    if isinstance(want, float):
        return math.isclose(float(got), want, rel_tol=1e-6, abs_tol=1e-6)
    return got == want


async def _wait(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with contextlib.suppress(KeyError):
            if pred():
                return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.1)


@pytest.mark.acceptance
@pytest.mark.drivers("omron_fins")
@TRANSPORTS
async def test_fins_device_through_the_engine(tmp_path, engine_factory, request, transport):
    if request.config.getoption("--engine") != "rust":
        pytest.skip("Omron FINS is Rust-first: the Python engine has no FINS driver (ADR-0002)")
    sim = await FinsSim.start(port=stable_port())
    _load(sim)
    dev = _device(sim, transport).model_copy(update={
        "scan_ms": 500, "min_scan_ms": 500, "timeout_ms": 400, "retries": 1, "demote_after": 2, "demote_for_ms": 1500})
    config_dir = write_config(tmp_path, [dev], api_port=free_port(), mqtt_port=free_port())
    engine = engine_factory(config_dir)

    def matches() -> bool:
        values = engine.device_values(dev.id)
        return all(values[n]["quality"] == "good" and _close(values[n]["value"], v) for n, v in _expected(sim).items())

    await engine.start()
    try:
        async with asyncio.timeout(60):
            await _wait(matches, 15, "good values")
            sim.set("D100", 4321)
            sim.set("T5", 9990, "bcd16")
            sim.set("CIO10.05", not sim.value("CIO10.05"))
            await _wait(matches, 10, "new values")
            await sim.stop()                                   # power off
            await _wait(lambda: engine.device_detail(dev.id)["state"] == "demoted", 15, "demoted")
            await sim.restart()                                # same port, same memory
            await _wait(lambda: engine.device_detail(dev.id)["state"] == "ok", 20, "resumed")
            await _wait(matches, 10, "values after power-on")
        assert sim.write_attempts == 0
    finally:
        await engine.stop()
        await sim.stop()
