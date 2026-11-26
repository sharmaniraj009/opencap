"""Simulator driver: computed values for demos, the UI, desktop tests and benchmarks (#12).

No network and no protocol library. `connect()` always succeeds (unless `params.fail_connect`)
and `read()` computes each tag's value from its address and the injected clock, so a run is
deterministic for a given clock, `params.epoch` and seed. Read-only by construction: there is
no write path, and memory registers only ever return their configured value.

Addresses (function names are case-insensitive; optional trailing arguments in brackets):

    RAMP(period_ms, low, high[, step])     low, low+step, ... up to high, then back to low;
                                           one step per period_ms. step < 0 ramps down from high.
    SINE(period_ms, low, high[, phase_deg]) sine wave between low and high, period_ms per cycle
    RANDOM(period_ms, low, high[, seed])   a new pseudo-random value every period_ms (SplitMix64)
    SQUARE(period_ms, low, high)           high for the first half of each period, low for the second
    CONST(value)                           a number, true/false or a "quoted string"
    K<n>                                   memory register n (params.memory["K<n>"], default 0)
    TIME                                   the read time, UTC, "YYYY-MM-DDTHH:MM:SS.mmmZ" (string tags)
    COUNTER                                1, 2, 3, ... one per read of the tag (integers wrap)

Time is integer milliseconds since `params.epoch` (default the Unix epoch), so two engines
reading at the same instant return the same values, and a restart does not reset a waveform.
The exact formulas are in docs/driver_params.md ("simulator") and pinned by the shared
conformance vectors in tests/conformance/simulator/ (ADR-0014).
"""

from __future__ import annotations

import asyncio
import math
import re
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers.base import Block, CommError, ConnectionLost, DecodeError, Driver, Plan

UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)
MASK64 = (1 << 64) - 1
GOLDEN_GAMMA = 0x9E3779B97F4A7C15          # SplitMix64 increment
RAMP_EPSILON = 1e-9                        # absorbs float error in (high - low) / step
MAX_REGISTER = 65535
MAX_PERIOD_MS = 86_400_000 * 366           # one (leap) year

INT_RANGES: dict[str, tuple[int, int]] = {
    "int16": (-(1 << 15), (1 << 15) - 1), "uint16": (0, (1 << 16) - 1),
    "int32": (-(1 << 31), (1 << 31) - 1), "uint32": (0, (1 << 32) - 1),
    "int64": (-(1 << 63), (1 << 63) - 1), "uint64": (0, (1 << 64) - 1),
}
FLOAT_DTYPES = frozenset({"float32", "float64"})
NUMERIC_DTYPES = frozenset(INT_RANGES) | FLOAT_DTYPES
FUNCTIONS = ("RAMP", "SINE", "RANDOM", "SQUARE", "CONST", "K", "TIME", "COUNTER")

_CALL_RE = re.compile(r"^\s*([A-Za-z]+)\s*\((.*)\)\s*$", re.DOTALL)
_REGISTER_RE = re.compile(r"^\s*[Kk](\d+)\s*$")
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
# (minimum, maximum) argument count per waveform
_ARITY: dict[str, tuple[int, int]] = {"RAMP": (3, 4), "SINE": (3, 4), "RANDOM": (3, 4), "SQUARE": (3, 3)}


# --- params ------------------------------------------------------------------------

@dataclass(frozen=True)
class SimParams:
    """Typed view of `Device.params` for the simulator. Unknown keys are ignored."""

    epoch_ms: int = 0                                  # params.epoch, as ms since the Unix epoch
    memory: Mapping[int, Any] = field(default_factory=dict)   # register number -> value
    fail_connect: bool = False
    fail_every_n_reads: int = 0                        # 0 = never
    latency_ms: int = 0
    bad_decode_tags: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any] | None) -> SimParams:
        """Validate `Device.params`. Raises ValueError with the offending key."""
        p = dict(params or {})
        return cls(
            epoch_ms=_parse_epoch(p.get("epoch")),
            memory=_parse_memory(p.get("memory")),
            fail_connect=_flag(p, "fail_connect"),
            fail_every_n_reads=_count(p, "fail_every_n_reads"),
            latency_ms=_count(p, "latency_ms"),
            bad_decode_tags=_names(p.get("bad_decode_tags")),
        )


def _flag(p: Mapping[str, Any], key: str) -> bool:
    value = p.get(key)
    if value is None:
        return False
    if type(value) is int and value in (0, 1):          # hand-written YAML may say 0/1
        return bool(value)
    if not isinstance(value, bool):
        raise ValueError(f"simulator param {key}={value!r}: expected true or false")
    return value


def _count(p: Mapping[str, Any], key: str) -> int:
    value = p.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"simulator param {key}={value!r}: expected an integer >= 0")
    return value


def _names(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"simulator param bad_decode_tags={value!r}: expected a list of tag names")
    return frozenset(value)


def _parse_epoch(value: Any) -> int:
    """`epoch`: null (Unix epoch), integer ms since the Unix epoch, or an ISO 8601 time with a zone."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"simulator param epoch={value!r}: expected ms since 1970 or an ISO 8601 time")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            ts = None
        if ts is not None and ts.tzinfo is not None:
            return (ts - UNIX_EPOCH) // _MS
    raise ValueError(f"simulator param epoch={value!r}: expected ms since 1970 or an ISO 8601 time "
                     "with a zone, e.g. \"2026-01-01T00:00:00Z\"")


def _parse_memory(value: Any) -> dict[int, Any]:
    """`memory`: {"K<n>": value}. Values are numbers, booleans or strings."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"simulator param memory={value!r}: expected a mapping like {{\"K0\": 12.5}}")
    out: dict[int, Any] = {}
    for key, v in value.items():
        m = _REGISTER_RE.match(str(key))
        if m is None or int(m.group(1)) > MAX_REGISTER:
            raise ValueError(f"simulator param memory: key {key!r} is not a register K0..K{MAX_REGISTER}")
        if not isinstance(v, (bool, int, float, str)):
            raise ValueError(f"simulator param memory.{key}={v!r}: expected a number, true/false or a string")
        if int(m.group(1)) in out:
            raise ValueError(f"simulator param memory: register K{int(m.group(1))} is given twice")
        out[int(m.group(1))] = v
    return out


# --- addresses ---------------------------------------------------------------------

@dataclass(frozen=True)
class SimAddress:
    """A parsed simulator address. `args` are the function's numeric arguments, defaults filled in."""

    function: str                      # one of FUNCTIONS
    args: tuple[float | int, ...] = ()
    value: Any = None                  # CONST literal, or the memory register's value
    register: int | None = None        # K<n>


def _number(text: str, what: str) -> int | float:
    t = text.strip()
    if not _NUMBER_RE.match(t):
        raise ValueError(f"{what} {t!r} is not a number")
    if _INT_RE.match(t):
        return int(t)
    value = float(t)
    if not math.isfinite(value):
        raise ValueError(f"{what} {t!r} is not finite")
    return value


def _split_args(text: str) -> list[str]:
    return [] if not text.strip() else text.split(",")


def _literal(text: str) -> Any:
    """CONST argument: a number, true/false, or a double-quoted string (\\" and \\\\ escapes)."""
    t = text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        body, out, i = t[1:-1], [], 0
        while i < len(body):
            ch = body[i]
            if ch == "\\":
                if i + 1 >= len(body) or body[i + 1] not in '"\\':
                    raise ValueError(f"CONST string {t}: only \\\" and \\\\ escapes are allowed")
                out.append(body[i + 1])
                i += 2
                continue
            if ch == '"':
                raise ValueError(f"CONST string {t}: escape an inner quote as \\\"")
            out.append(ch)
            i += 1
        return "".join(out)
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    return _number(t, "CONST value")


def _check_int_fits(value: int | float, dtype: str, what: str) -> None:
    lo, hi = INT_RANGES[dtype]
    if not float(value).is_integer():
        raise ValueError(f"{what} {value} is not a whole number (tag is {dtype})")
    if not lo <= int(value) <= hi:
        raise ValueError(f"{what} {value} is out of range for {dtype} ({lo}..{hi})")


def _check_value_for_dtype(value: Any, dtype: str, what: str) -> None:
    """A CONST or memory value must be representable in the tag's dtype."""
    if dtype == "string":
        if not isinstance(value, str):
            raise ValueError(f"{what} {value!r} is not a string (tag is string)")
    elif dtype == "bool":
        if not isinstance(value, bool) and value not in (0, 1):
            raise ValueError(f"{what} {value!r} is not true/false or 0/1 (tag is bool)")
    elif isinstance(value, (str, bool)):
        raise ValueError(f"{what} {value!r} is not a number (tag is {dtype})")
    elif dtype in INT_RANGES:
        _check_int_fits(value, dtype, what)
    elif dtype == "float32" and abs(value) > struct.unpack("<f", b"\xff\xff\x7f\x7f")[0]:
        raise ValueError(f"{what} {value} is out of range for float32")


def parse_address(address: str, dtype: str, params: SimParams | None = None) -> SimAddress:
    """Parse a simulator address for a tag of `dtype`. Pure. Raises ValueError with a clear reason."""
    p = params or SimParams()
    text = address.strip()
    upper = text.upper()
    if upper == "TIME":
        if dtype != "string":
            raise ValueError(f"TIME needs a string tag (tag is {dtype})")
        return SimAddress("TIME")
    if upper == "COUNTER":
        if dtype not in NUMERIC_DTYPES:
            raise ValueError(f"COUNTER needs a numeric tag (tag is {dtype})")
        return SimAddress("COUNTER")
    m = _REGISTER_RE.match(text)
    if m:
        n = int(m.group(1))
        if n > MAX_REGISTER:
            raise ValueError(f"register K{n} is out of range (K0..K{MAX_REGISTER})")
        if n in p.memory:
            value = p.memory[n]
            _check_value_for_dtype(value, dtype, f"memory K{n} =")
        else:
            value = "" if dtype == "string" else 0
        return SimAddress("K", value=value, register=n)
    m = _CALL_RE.match(text)
    if not m:
        raise ValueError(f"unknown simulator address {address!r}; expected one of RAMP(...), SINE(...), "
                         "RANDOM(...), SQUARE(...), CONST(...), K<n>, TIME, COUNTER")
    func, inner = m.group(1).upper(), m.group(2)
    if func == "CONST":
        value = _literal(inner)
        _check_value_for_dtype(value, dtype, "CONST value")
        return SimAddress("CONST", value=value)
    if func not in _ARITY:
        raise ValueError(f"unknown simulator function {m.group(1)!r}")
    if dtype == "string":
        raise ValueError(f"{func} needs a numeric or bool tag (tag is string)")
    parts = _split_args(inner)
    lo_n, hi_n = _ARITY[func]
    if not lo_n <= len(parts) <= hi_n:
        want = str(lo_n) if lo_n == hi_n else f"{lo_n} or {hi_n}"
        raise ValueError(f"{func} takes {want} arguments, got {len(parts)}")
    period = _number(parts[0], f"{func} period_ms")
    if not isinstance(period, int) or not 1 <= period <= MAX_PERIOD_MS:
        raise ValueError(f"{func} period_ms {parts[0].strip()!r} must be a whole number 1..{MAX_PERIOD_MS}")
    low = _number(parts[1], f"{func} low")
    high = _number(parts[2], f"{func} high")
    if low > high:
        raise ValueError(f"{func} low {low} is greater than high {high}")
    if func == "RAMP":
        step = _number(parts[3], "RAMP step") if len(parts) > 3 else 1
        if step == 0:
            raise ValueError("RAMP step must not be 0")
        if dtype in INT_RANGES:
            _check_int_fits(step, dtype, "RAMP step")
        args: tuple[float | int, ...] = (period, low, high, step)
    elif func == "SINE":
        phase = _number(parts[3], "SINE phase_deg") if len(parts) > 3 else 0
        args = (period, low, high, phase)
    elif func == "RANDOM":
        seed = _number(parts[3], "RANDOM seed") if len(parts) > 3 else 0
        if not isinstance(seed, int) or not 0 <= seed <= MASK64:
            raise ValueError(f"RANDOM seed {parts[3].strip()!r} must be a whole number 0..2^64-1")
        args = (period, low, high, seed)
    else:
        args = (period, low, high)
    if dtype in INT_RANGES:
        _check_int_fits(low, dtype, f"{func} low")
        _check_int_fits(high, dtype, f"{func} high")
    elif dtype == "bool":
        if low not in (0, 1) or high not in (0, 1):
            raise ValueError(f"{func} on a bool tag needs low and high of 0 or 1 (got {low}, {high})")
    elif dtype == "float32":
        _check_value_for_dtype(low, dtype, f"{func} low")
        _check_value_for_dtype(high, dtype, f"{func} high")
    return SimAddress(func, args=args)


# --- value functions (pure; the spec the Rust port follows) --------------------------

def splitmix64(seed: int, n: int) -> int:
    """The n-th output (n = 0, 1, ...) of SplitMix64 seeded with `seed`, computed directly."""
    z = (seed + (n + 1) * GOLDEN_GAMMA) & MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


def round_half_away(x: float) -> int:
    """Round to the nearest integer, ties away from zero (Rust's `f64::round`)."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _to_float32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _coerce(raw: int | float, dtype: str) -> Any:
    """A computed number in the tag's dtype. Integer results of RAMP/RANDOM are exact."""
    if dtype == "bool":
        return (raw if isinstance(raw, int) else round_half_away(raw)) != 0
    if dtype in INT_RANGES:
        return raw if isinstance(raw, int) else round_half_away(raw)
    if dtype == "float32":
        return _to_float32(float(raw))
    return float(raw)


def _wrap(n: int, dtype: str) -> int:
    lo, hi = INT_RANGES[dtype]
    return (n - lo) % (hi - lo + 1) + lo


def waveform(addr: SimAddress, dtype: str, elapsed_ms: int) -> Any:
    """Value of a RAMP/SINE/RANDOM/SQUARE address `elapsed_ms` after the epoch (may be negative)."""
    period, low, high = addr.args[0], addr.args[1], addr.args[2]
    n = elapsed_ms // period                                  # floor division
    if addr.function == "RAMP":
        step = addr.args[3]
        if isinstance(low, int) and isinstance(high, int) and isinstance(step, int):
            steps = (high - low) // abs(step) + 1                 # exact for integer arguments
        else:
            steps = math.floor((high - low) / abs(step) + RAMP_EPSILON) + 1
        k = n % steps                                         # Euclidean (always >= 0)
        raw = low + step * k if step > 0 else high + step * k
        return _coerce(raw, dtype)
    if addr.function == "SINE":
        frac = (elapsed_ms % period) / period
        mid, amp = (low + high) / 2, (high - low) / 2
        raw = mid + amp * math.sin(2 * math.pi * frac + math.radians(addr.args[3]))
        raw = min(max(raw, low), high)
        return _coerce(raw, dtype)
    if addr.function == "RANDOM":
        u = (splitmix64(addr.args[3], n & MASK64) >> 11) / float(1 << 53)    # [0, 1)
        if dtype in INT_RANGES or dtype == "bool":              # low, high are whole numbers here
            span = int(high) - int(low)
            return _coerce(int(low) + min(math.floor(u * (span + 1)), span), dtype)
        return _coerce(low + u * (high - low), dtype)
    # SQUARE
    first_half = (elapsed_ms % period) * 2 < period
    return _coerce(high if first_half else low, dtype)


def format_time(now: datetime) -> str:
    """TIME value: UTC, millisecond precision, e.g. 2026-11-18T09:30:00.250Z."""
    utc = now.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def static_value(addr: SimAddress, dtype: str) -> Any:
    """Value of a CONST or K<n> address."""
    v = addr.value
    if dtype == "string":
        return v
    if dtype == "bool":
        return bool(v)
    return _coerce(v, dtype)


def compute(addr: SimAddress, dtype: str, now: datetime, epoch_ms: int, reads: int = 1) -> Any:
    """The value of `addr` at `now`. `reads` is the 1-based read count (COUNTER only)."""
    if addr.function in ("CONST", "K"):
        return static_value(addr, dtype)
    if addr.function == "TIME":
        return format_time(now)
    if addr.function == "COUNTER":
        return _wrap(reads, dtype) if dtype in INT_RANGES else _coerce(reads, dtype)
    return waveform(addr, dtype, (now - UNIX_EPOCH) // _MS - epoch_ms)


# --- plan ----------------------------------------------------------------------------

@dataclass(kw_only=True)
class SimBlock(Block):
    """All simulator tags of one rate group: one 'request', no wire."""

    addresses: dict[str, SimAddress] = field(default_factory=dict)
    dtypes: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        return f"SIM {len(self.tags)} tags"

    def wire_requests(self) -> int:
        return 0


def plan_tags(tags: list[Tag], params: SimParams) -> Plan:
    """Pure: parse every enabled tag; all valid ones go into one block."""
    block = SimBlock()
    invalid: dict[str, str] = {}
    for tag in tags:
        if not tag.enabled:
            continue
        try:
            addr = parse_address(tag.address, tag.dtype, params)
        except ValueError as exc:
            invalid[tag.name] = str(exc)
            continue
        block.tags.append(tag.name)
        block.addresses[tag.name] = addr
        block.dtypes[tag.name] = tag.dtype
    return Plan(blocks=[block] if block.tags else [], invalid=invalid)


# --- driver --------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(UTC)


class SimulatorDriver(Driver):
    """Computed values, no I/O. `clock` is injectable so tests are deterministic."""

    name: ClassVar[str] = "simulator"
    is_async: ClassVar[bool] = True

    def __init__(self, device: Device, executor: Any = None, *,
                 clock: Callable[[], datetime] = _utc_now,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        super().__init__(device, executor)
        self.params = SimParams.from_mapping(device.params)      # ValueError on bad params
        self._clock = clock
        self._sleep = sleep
        self._connected = False
        self.reads = 0                       # read() calls, failed ones included (fail_every_n_reads)
        self.connects = 0
        self._served: dict[str, int] = {}    # tag name -> good reads (COUNTER)

    def plan(self, tags: list[Tag]) -> Plan:
        return plan_tags(tags, self.params)

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connects += 1
        if self.params.fail_connect:
            raise ConnectionLost("simulated connect failure (params.fail_connect)")
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def identity(self) -> dict[str, Any]:
        if not self._connected:
            return {}
        return {"vendor": "opencap", "model": "simulator", "simulated": True}

    async def read(self, block: Block) -> dict[str, TagValue]:
        if not self._connected:
            raise ConnectionLost("not connected")
        assert isinstance(block, SimBlock)
        p = self.params
        self.reads += 1
        if p.latency_ms:
            timeout_ms = max(self.device.timeout_ms, 0)
            if p.latency_ms >= timeout_ms:
                await self._sleep(timeout_ms / 1000)
                raise CommError(f"no answer within {timeout_ms} ms (simulated latency {p.latency_ms} ms)")
            await self._sleep(p.latency_ms / 1000)
        if p.fail_every_n_reads and self.reads % p.fail_every_n_reads == 0:
            raise CommError(f"simulated read failure (read {self.reads}, params.fail_every_n_reads="
                            f"{p.fail_every_n_reads})")
        now = self._clock()
        out: dict[str, TagValue] = {}
        for name in block.tags:
            if name in p.bad_decode_tags:
                out[name] = TagValue(DecodeError("simulated decode failure (params.bad_decode_tags)"),
                                     "bad_decode", now)
                continue
            addr, dtype = block.addresses[name], block.dtypes[name]
            count = self._served.get(name, 0) + 1
            if addr.function == "COUNTER":
                self._served[name] = count
            out[name] = TagValue(compute(addr, dtype, now, p.epoch_ms, count), "good", now)
        return out
