"""simulator driver (#12): address parser, every value function with a fake clock, params, fault
switches, the read-only guard, and the shared conformance vectors (tests/conformance/simulator/)."""

from __future__ import annotations

import asyncio
import json
import math
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers import get_driver_class
from opencap.drivers.base import CommError, ConnectionLost, DecodeError
from opencap.drivers.simulator import (
    UNIX_EPOCH,
    SimAddress,
    SimBlock,
    SimParams,
    SimulatorDriver,
    compute,
    format_time,
    parse_address,
    round_half_away,
    splitmix64,
)

VECTORS = Path(__file__).resolve().parents[1] / "conformance" / "simulator" / "vectors.json"
T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += timedelta(milliseconds=ms)


class FakeSleep:
    """Records the delays a read asks for instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def tag(name: str, address: str, dtype: str = "float64", **kw) -> Tag:
    return Tag(name=name, address=address, dtype=dtype, **kw)


def dev(tags: list[Tag], timeout_ms: int = 3000, **params) -> Device:
    return Device(id="Sim/Demo", channel="Sim", name="Demo", driver="simulator", host="sim", port=0,
                  params=params, timeout_ms=timeout_ms, tags=tags)


def at(ms: int) -> datetime:
    return UNIX_EPOCH + timedelta(milliseconds=ms)


def value(address: str, dtype: str, ms: int, params: dict | None = None, reads: int = 1):
    p = SimParams.from_mapping(params)
    return compute(parse_address(address, dtype, p), dtype, at(ms), p.epoch_ms, reads)


# --- registration and read-only ------------------------------------------------------

def test_registered_under_its_driver_name():
    assert get_driver_class("simulator") is SimulatorDriver
    assert SimulatorDriver.name == "simulator"
    Device.model_validate(dev([]).model_dump())          # "simulator" is in the DriverName literal


async def test_write_is_refused():
    drv = SimulatorDriver(dev([tag("a", "K0", "int16")]))
    await drv.connect()
    with pytest.raises(NotImplementedError, match="read-only"):
        await drv.write("a", 1)
    assert SimulatorDriver.write is get_driver_class("modbus_tcp").write   # not overridden


# --- the parser ----------------------------------------------------------------------

@pytest.mark.parametrize("address,dtype,expected", [
    ("RAMP(1000, 0, 5)", "int16", SimAddress("RAMP", (1000, 0, 5, 1))),
    ("ramp( 500 ,-1.5, 2.5 , 0.5 )", "float32", SimAddress("RAMP", (500, -1.5, 2.5, 0.5))),
    ("Sine(4000, -10, 10)", "float64", SimAddress("SINE", (4000, -10, 10, 0))),
    ("SINE(4000, -10, 10, 90)", "int32", SimAddress("SINE", (4000, -10, 10, 90))),
    ("RANDOM(1000, 1, 6)", "uint16", SimAddress("RANDOM", (1000, 1, 6, 0))),
    ("RANDOM(1000, 1, 6, 42)", "uint16", SimAddress("RANDOM", (1000, 1, 6, 42))),
    ("SQUARE(1000, 0, 1)", "bool", SimAddress("SQUARE", (1000, 0, 1))),
    ("CONST(3.5)", "float64", SimAddress("CONST", value=3.5)),
    ('CONST("a, (b)")', "string", SimAddress("CONST", value="a, (b)")),
    ("CONST(false)", "bool", SimAddress("CONST", value=False)),
    ("K12", "int32", SimAddress("K", value=0, register=12)),
    (" k12 ", "string", SimAddress("K", value="", register=12)),
    ("TIME", "string", SimAddress("TIME")),
    ("counter", "uint32", SimAddress("COUNTER")),
])
def test_parse_address(address, dtype, expected):
    assert parse_address(address, dtype) == expected


def test_memory_register_takes_its_configured_value():
    p = SimParams.from_mapping({"memory": {"K3": 12.5, "k4": "x"}})
    assert parse_address("K3", "float32", p).value == 12.5
    assert parse_address("K4", "string", p).value == "x"
    with pytest.raises(ValueError, match="memory K3 = 12.5 is not a whole number"):
        parse_address("K3", "int16", p)


# --- value functions with a fake clock -------------------------------------------------

def test_ramp_steps_once_per_period_and_wraps():
    assert [value("RAMP(1000, 0, 5, 2)", "int16", s * 1000) for s in range(7)] == [0, 2, 4, 0, 2, 4, 0]
    assert value("RAMP(1000, 0, 5, 2)", "int16", 999) == 0
    assert value("RAMP(1000, 0, 5, 2)", "int16", -1) == 4          # floor division before the epoch


def test_ramp_down_float_and_bool():
    assert [value("RAMP(1000, 0, 5, -2)", "int16", s * 1000) for s in range(4)] == [5, 3, 1, 5]
    assert [value("RAMP(1000, 0, 1, 0.25)", "float32", s * 1000) for s in range(6)] == [0, .25, .5, .75, 1, 0]
    # a float step that does not divide the span exactly still reaches high (epsilon)
    assert value("RAMP(1000, 0, 0.3, 0.1)", "float64", 3000) == pytest.approx(0.3)
    assert [value("RAMP(1000, 0, 1)", "bool", s * 1000) for s in range(3)] == [False, True, False]


def test_ramp_integer_arguments_are_exact_over_the_whole_int64_range():
    lo, hi = -(1 << 63), (1 << 63) - 1
    addr = f"RAMP(1000, {lo}, {hi})"
    assert value(addr, "int64", 0) == lo
    assert value(addr, "int64", -1000) == hi                       # the last step, not hi + 1


def test_sine_quarter_points_phase_and_integer_rounding():
    got = [value("SINE(4000, -10, 10)", "float64", ms) for ms in (0, 1000, 2000, 3000, 4000)]
    assert got == pytest.approx([0, 10, 0, -10, 0], abs=1e-9)
    assert [value("SINE(4000, -10, 10, 90)", "int32", ms) for ms in (0, 1000, 2000)] == [10, 0, -10]
    assert value("SINE(4000, 0, 100)", "int16", 333) == round_half_away(50 + 50 * math.sin(2 * math.pi * 333 / 4000))
    assert isinstance(value("SINE(4000, 0, 100)", "int16", 333), int)


def test_sine_stays_inside_low_high():
    for ms in range(0, 4000, 7):
        assert -3 <= value("SINE(4000, -3, 3, 33)", "float64", ms) <= 3
        assert -3 <= value("SINE(4000, -3, 3, 33)", "int16", ms) <= 3


def test_splitmix64_matches_the_reference_sequence():
    # Vigna's splitmix64.c seeded with 1234567 (also java.util.SplittableRandom's mixer)
    assert [splitmix64(1234567, n) for n in range(5)] == [
        6457827717110365317, 3203168211198807973, 9817491932198370423,
        4593380528125082431, 16408922859458223821,
    ]


def test_random_is_deterministic_per_seed_and_changes_once_per_period():
    a = [value("RANDOM(1000, 0, 1000000, 1)", "int32", s * 1000) for s in range(20)]
    assert a == [value("RANDOM(1000, 0, 1000000, 1)", "int32", s * 1000 + 999) for s in range(20)]
    assert a != [value("RANDOM(1000, 0, 1000000, 2)", "int32", s * 1000) for s in range(20)]
    assert len(set(a)) > 15


def test_random_integers_reach_both_ends_and_floats_stay_in_range():
    dice = {value("RANDOM(10, 1, 6, 9)", "int16", s * 10) for s in range(500)}
    assert dice == {1, 2, 3, 4, 5, 6}
    floats = [value("RANDOM(10, -2.5, 2.5, 9)", "float64", s * 10) for s in range(500)]
    assert all(-2.5 <= f < 2.5 for f in floats) and min(floats) < -2 and max(floats) > 2
    assert {value("RANDOM(10, 0, 1, 3)", "bool", s * 10) for s in range(50)} == {True, False}


def test_square_is_high_for_the_first_half():
    assert [value("SQUARE(1000, 0, 100)", "uint16", ms) for ms in (0, 499, 500, 999, 1000)] == [100, 100, 0, 0, 100]
    assert value("SQUARE(1000, 0, 1)", "bool", 0) is True


def test_const_and_memory_in_every_dtype():
    assert value("CONST(42)", "int16", 0) == 42
    f32 = value("CONST(0.1)", "float32", 0)
    assert f32 != 0.1 and f32 == pytest.approx(0.1, abs=1e-8)                      # rounded to float32
    assert value("CONST(0.1)", "float64", 0) == 0.1
    assert value("CONST(3)", "float64", 0) == 3.0 and isinstance(value("CONST(3)", "float64", 0), float)
    assert value("CONST(1)", "bool", 0) is True
    assert value('CONST("say \\"hi\\"")', "string", 0) == 'say "hi"'
    assert value("K0", "uint16", 0) == 0 and value("K0", "string", 0) == "" and value("K0", "bool", 0) is False
    assert value("K9", "float64", 0, {"memory": {"K9": -4}}) == -4.0


def test_time_and_counter():
    assert value("TIME", "string", 1_795_000_000_250) == "2026-11-18T11:06:40.250Z"
    assert format_time(datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC)) == "2026-01-02T03:04:05.999Z"
    assert value("COUNTER", "int16", 0, reads=32768) == -32768     # wraps like a PLC counter
    assert value("COUNTER", "uint64", 0, reads=7) == 7


def test_epoch_shifts_time_zero():
    epoch = int((T0 - UNIX_EPOCH) / timedelta(milliseconds=1))
    for params in ({"epoch": epoch}, {"epoch": "2026-11-18T09:00:00Z"}, {"epoch": "2026-11-18T10:00:00+01:00"}):
        assert value("RAMP(1000, 0, 100, 10)", "int32", epoch + 3000, params) == 30


# --- params ----------------------------------------------------------------------------

@pytest.mark.parametrize("params,match", [
    ({"fail_connect": "yes"}, "fail_connect"),
    ({"fail_every_n_reads": -1}, "fail_every_n_reads"),
    ({"fail_every_n_reads": 1.5}, "fail_every_n_reads"),
    ({"latency_ms": True}, "latency_ms"),
    ({"bad_decode_tags": "a"}, "bad_decode_tags"),
    ({"bad_decode_tags": [1]}, "bad_decode_tags"),
    ({"epoch": "yesterday"}, "epoch"),
    ({"epoch": "2026-01-01T00:00:00"}, "epoch"),          # no zone
    ({"memory": [1, 2]}, "memory"),
    ({"memory": {"D1": 1}}, "not a register"),
    ({"memory": {"K1": [1]}}, "memory.K1"),
    ({"memory": {"K1": 1, "k1": 2}}, "given twice"),
])
def test_bad_params_raise_at_construction(params, match):
    with pytest.raises(ValueError, match=match):
        SimulatorDriver(dev([], **params))


def test_params_defaults_and_yaml_style_flags():
    assert SimParams.from_mapping(None) == SimParams()
    assert SimParams.from_mapping({"fail_connect": 1, "unknown_key": "ignored"}).fail_connect is True


# --- plan -------------------------------------------------------------------------------

def test_plan_puts_valid_tags_in_one_block_and_rejects_the_rest():
    tags = [tag("ramp", "RAMP(1000, 0, 10)", "int16"), tag("bad", "RAMP(1000, 10, 0)", "int16"),
            tag("off", "K0", "int16", enabled=False), tag("t", "TIME", "string"), tag("wrong", "TIME", "int16")]
    plan = SimulatorDriver(dev(tags)).plan(tags)
    assert [b.tags for b in plan.blocks] == [["ramp", "t"]]
    assert set(plan.invalid) == {"bad", "wrong"}
    assert "greater than high" in plan.invalid["bad"] and "string tag" in plan.invalid["wrong"]
    assert plan.wire_requests == 0 and plan.blocks[0].describe() == "SIM 2 tags"
    assert SimulatorDriver(dev([])).plan([]).blocks == []


# --- the driver -------------------------------------------------------------------------

TAGS = [tag("ramp", "RAMP(1000, 0, 100, 10)", "int16"), tag("sine", "SINE(4000, -1, 1)"),
        tag("c", "COUNTER", "uint32"), tag("now", "TIME", "string"), tag("k", "K5", "float32")]


async def _driver(tags=TAGS, clock=None, sleep=None, **params) -> tuple[SimulatorDriver, SimBlock]:
    kw = {"clock": clock or FakeClock(), **({"sleep": sleep} if sleep else {})}
    drv = SimulatorDriver(dev(tags, timeout_ms=params.pop("timeout_ms", 3000), **params), **kw)
    plan = drv.plan(tags)
    assert not plan.invalid
    await drv.connect()
    return drv, plan.blocks[0]


async def test_read_computes_every_tag_with_the_clock_as_source_ts():
    clock = FakeClock(at(3000))
    drv, block = await _driver(clock=clock, memory={"K5": 2.5})
    first = await drv.read(block)
    assert {n: v.quality for n, v in first.items()} == dict.fromkeys(block.tags, "good")
    assert {n: v.value for n, v in first.items()} == {
        "ramp": 30, "sine": pytest.approx(-1), "c": 1, "now": "1970-01-01T00:00:03.000Z", "k": 2.5}
    assert all(v.source_ts == at(3000) for v in first.values())
    clock.advance(1000)
    second = await drv.read(block)
    assert (second["ramp"].value, second["c"].value, second["sine"].value) == (40, 2, pytest.approx(0, abs=1e-9))
    assert second["now"].value == "1970-01-01T00:00:04.000Z" and second["k"].source_ts == at(4000)
    assert await drv.identity() == {"vendor": "opencap", "model": "simulator", "simulated": True}


async def test_connect_close_and_fail_connect():
    drv, block = await _driver()
    assert drv.connected
    await drv.close()
    await drv.close()                                   # idempotent
    assert not drv.connected and await drv.identity() == {}
    with pytest.raises(ConnectionLost):
        await drv.read(block)
    bad = SimulatorDriver(dev(TAGS, fail_connect=True))
    for _ in range(2):
        with pytest.raises(ConnectionLost, match="fail_connect"):
            await bad.connect()
    assert not bad.connected and bad.connects == 2


async def test_fail_every_n_reads_fails_exactly_every_nth_call():
    drv, block = await _driver(fail_every_n_reads=3)
    outcomes = []
    for _ in range(9):
        try:
            await drv.read(block)
            outcomes.append("ok")
        except CommError as exc:
            assert not isinstance(exc, ConnectionLost)
            outcomes.append("fail")
    assert outcomes == ["ok", "ok", "fail"] * 3
    assert (await drv.read(block))["c"].value == 7       # COUNTER counts good reads only


async def test_latency_below_and_at_the_timeout():
    sleep = FakeSleep()
    drv, block = await _driver(sleep=sleep, latency_ms=250, timeout_ms=1000)
    await drv.read(block)
    assert sleep.calls == [0.25]
    sleep = FakeSleep()
    drv, block = await _driver(sleep=sleep, latency_ms=1000, timeout_ms=1000)
    with pytest.raises(CommError, match="simulated latency"):
        await drv.read(block)
    assert sleep.calls == [1.0]


async def test_latency_really_waits_without_an_injected_sleep():
    drv, block = await _driver(latency_ms=30)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await drv.read(block)
    assert loop.time() - t0 >= 0.025


async def test_bad_decode_tags():
    drv, block = await _driver(bad_decode_tags=["sine", "not-a-tag"])
    out = await drv.read(block)
    assert out["sine"].quality == "bad_decode" and isinstance(out["sine"].value, DecodeError)
    assert out["ramp"].quality == "good"


async def test_no_socket_is_ever_opened(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the simulator opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    drv, block = await _driver()
    await drv.read(block)
    await drv.identity()
    await drv.close()


# --- shared conformance vectors (ADR-0014) ------------------------------------------------

def _vectors(kind: str) -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))[kind]


@pytest.mark.parametrize("vec", _vectors("valid"), ids=lambda v: f"{v['address']}:{v['dtype']}@{v['at_ms']}")
def test_conformance_valid(vec):
    got = value(vec["address"], vec["dtype"], vec["at_ms"], vec.get("params"), vec.get("reads", 1))
    want = vec["expected"]
    assert type(got) is type(want), (got, want)
    if "tolerance" in vec:
        assert math.isclose(got, want, abs_tol=vec["tolerance"]), (got, want)
    else:
        assert got == want


@pytest.mark.parametrize("vec", _vectors("invalid"), ids=lambda v: f"{v['address']}:{v['dtype']}")
def test_conformance_invalid(vec):
    with pytest.raises(ValueError) as exc:
        parse_address(vec["address"], vec["dtype"], SimParams.from_mapping(vec.get("params")))
    assert vec["error"] in str(exc.value)


def test_vectors_cover_every_function():
    seen = {parse_address(v["address"], v["dtype"], SimParams.from_mapping(v.get("params"))).function
            for v in _vectors("valid")}
    assert seen == {"RAMP", "SINE", "RANDOM", "SQUARE", "CONST", "K", "TIME", "COUNTER"}
