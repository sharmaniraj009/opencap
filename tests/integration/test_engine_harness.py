"""The conformance harness itself (tests/helpers/engine.py, tests/conftest.py): both engine
adapters answer the same queries the acceptance suites use, and the Rust port-gap summary is
right. The Rust cases skip when the binary has not been built (`cd engine && cargo build`).
"""

from __future__ import annotations

import asyncio
import time
import typing

import pytest

from tests.helpers.broker import TestBroker
from tests.helpers.configdir import write_config
from tests.helpers import engine as harness
from tests.helpers.engine import (
    NOT_AGNOSTIC,
    NOT_COMPILED,
    NOT_PORTED,
    implemented_drivers,
    make_engine_factory,
    port_gap_markdown,
    rust_binary,
    unsupported_reason,
)
from tests.helpers.modbus_sim import free_port, make_sim_devices, start_sims, stop_sims

BIN = rust_binary()
ENGINES = ["python", pytest.param("rust", marks=pytest.mark.skipif(
    BIN is None or not BIN.exists(), reason="Rust engine not built (cd engine && cargo build)"))]


async def _wait(pred, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("kind", ENGINES)
async def test_both_adapters_answer_the_same_queries(tmp_path, kind):
    from opencap.logsetup import configure_logging

    configure_logging("INFO")
    sims = await start_sims(make_sim_devices(2, 5, scan_ms=500))
    try:
        devices = [s.device for s in sims]
        async with asyncio.timeout(60), TestBroker() as broker:
            config_dir = write_config(tmp_path, devices, mqtt_port=broker.port, api_port=free_port(),
                                      poller={"stats_interval_s": 1, "log_level": "INFO"},
                                      publish={"interval_ms": 500})
            engine = make_engine_factory(kind, tmp_path)(config_dir)
            assert engine.kind == kind and engine.is_python is (kind == "python")
            await engine.start()
            try:
                dev = devices[0].id
                await _wait(lambda: engine.health()["devices"].get("ok") == 2, 15, "both devices ok")
                assert engine.device_detail(dev)["state"] == "ok"
                assert {v["quality"] for v in engine.device_values(dev).values()} == {"good"}
                await _wait(lambda: any(t.endswith("/data") for t in engine.sent), 10, "a data message handed to the link")
                await _wait(lambda: {e["device"] for e in engine.log_events("device.stats")} >= {d.id for d in devices},
                            10, "a device.stats line per device")
                stats = engine.log_events("device.stats")
                # A fast local cycle can measure 0 ms where time.monotonic is coarse (~15.6 ms on
                # Windows with Python 3.12, see #199); everywhere else it must be > 0.
                coarse = time.get_clock_info("monotonic").resolution > 1e-3
                assert all(e["event"] == "device.stats" and (e["mean_cycle_ms"] > 0 or coarse) for e in stats), stats
                assert engine.log_events("no.such.event") == []

                code, out = await engine.cli("read", dev, "--config", str(config_dir), cwd=tmp_path)
                assert code == 0, out
                assert sum(" good " in line for line in out.splitlines()) == len(devices[0].tags), out
                code, out = await engine.cli("read", "NOWHERE/PLC", "--config", str(config_dir), cwd=tmp_path)
                assert code != 0 and "unknown device" in out, out
            finally:
                await engine.stop()
    finally:
        await stop_sims(sims)


def test_implemented_drivers():
    assert implemented_drivers("python") is None                  # the reference: no port gap
    if BIN is None or not BIN.exists():
        pytest.skip("Rust engine not built (cd engine && cargo build)")
    rust = implemented_drivers("rust")
    assert "modbus_tcp" in rust
    from opencap.config.models import DriverName

    known = set(typing.get_args(DriverName))
    assert rust <= known, rust - known


def test_unsupported_reason(monkeypatch):
    report = {"implemented": ["modbus_tcp"], "not_compiled_in": ["s7"], "not_implemented": ["ab_logix", "ab_df1"]}
    monkeypatch.setattr(harness, "_rust_report", lambda: report)
    assert unsupported_reason("python", ("ab_logix",)) is None
    assert unsupported_reason("rust", ("modbus_tcp",)) is None
    assert unsupported_reason("rust", ()) is None
    assert unsupported_reason("rust", ("modbus_tcp", "ab_logix", "ab_df1")) == f"{NOT_PORTED}: ab_df1, ab_logix"
    assert unsupported_reason("rust", ("s7", "ab_logix")).startswith(f"{NOT_COMPILED}: s7 ")


def test_port_gap_markdown():
    gap = {
        "tests/integration/test_phase1_acceptance.py::test_phase1_acceptance":
            {"drivers": ("modbus_tcp",), "outcome": "passed", "reason": ""},
        "tests/integration/test_s7_melsec_acceptance.py::test_s7_melsec_acceptance":
            {"drivers": ("s7", "melsec_mc"), "outcome": "skipped", "reason": f"{NOT_PORTED}: melsec_mc, s7"},
        "tests/integration/test_ui_browser.py::test_ui_in_a_browser":
            {"drivers": (), "outcome": "skipped", "reason": f"{NOT_AGNOSTIC}: drives the Python engine directly"},
    }
    md = port_gap_markdown(gap)
    assert "1 of 3 suites pass on the Rust engine" in md
    assert "Drivers not ported yet: `melsec_mc`, `s7`." in md
    assert "| `tests/integration/test_phase1_acceptance.py::test_phase1_acceptance` | `modbus_tcp` | ✅ passed |  |" in md
    assert "| `tests/integration/test_ui_browser.py::test_ui_in_a_browser` | — | ⏭️ skipped |" in md
