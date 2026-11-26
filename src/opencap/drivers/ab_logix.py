"""Allen-Bradley symbolic-tag driver: ControlLogix / CompactLogix and Micro800, read
with pycomm3 `LogixDriver` (PLAN.md section 5.1, `ab_logix`; docs/phase2_contracts.md §1).

Pure parts (no pycomm3 import, D-002): a light symbolic-address validator, batching of
distinct names for `plc.read(*names)`, an estimate of the CIP packets pycomm3 will put
on the wire, and decoding of the returned values.

I/O parts (Phase 2): `AbLogixDriver.connect/read/close/identity`, the tag-existence check
against the controller's tag list, and Micro800 batching. Every pycomm3 call runs on the
channel's single-thread executor. pycomm3 is reached through `logix_driver_class()`, a
small `LogixDriver` subclass whose hooks are pinned by tests/unit/test_ab_logix_io.py.

pycomm3 1.2.16 facts this module is built on (see docs/decisions.md):
- Micro800: `LogixDriver` never uses the Multiple Service Packet; every name is its own
  request (`_read_build_requests`). The subclass lifts that when the connect-time probe
  (or `params.batching = "on"`) says the controller accepts it.
- Logix: names are packed into Multiple Service Packets greedily, in the order given.
  Each name costs `data bytes + len(request message) + 2` against the connection size,
  starting from `MULTISERVICE_READ_OVERHEAD` (10). A name that alone exceeds the
  connection size becomes a fragmented read.
- The connection size is 4000 with Extended Forward Open, else 500 (Micro800 rejects the
  large forward open, so it ends up at 500).
- `read()` does not de-duplicate repeated names; this module does.
- A Multiple Service Packet reply with no data (e.g. status 0x08) makes pycomm3 raise from
  inside its reply parser; a peer close in the middle of a reply makes its socket loop forever.
  The subclass turns the first into `WholeReplyError` and replaces the socket reader.

The server is read-only: nothing here builds a write, and the subclass refuses pycomm3's
write paths (`write`, `set_plc_time`, Set/Write services, write packets).
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from itertools import batched
from typing import Any, ClassVar, Sequence

import structlog

from opencap.config.models import Device, Tag
from opencap.core.cache import TagValue
from opencap.drivers._cip_common import WRITE_SERVICES, describe_exc, is_comm_error, is_timeout, oo_socket_class
from opencap.drivers._cip_common import Attempt as _Attempt
from opencap.drivers._cip_common import exc_chain as _chain
from opencap.drivers import _schema as sc
from opencap.drivers.ab_pccc import coerce_value, parse_pccc_address
from opencap.drivers.base import Block, BrowseResult, CommError, ConnectionLost, DecodeError, Driver, Plan

__all__ = [
    "AbLogixDriver", "DecodeError", "LogixAddress", "LogixBlock", "LogixItem", "LogixRead",
    "NameResult", "WholeReplyError", "browse_tag_list", "connection_path", "decode_block", "logix_driver_class",
    "parse_batching", "parse_logix_address", "probe_names", "resolve_read_name",
]

DEFAULT_MAX_BATCH = 40
CONNECTION_SIZE_LARGE = 4000      # pycomm3 Extended Forward Open
CONNECTION_SIZE_STANDARD = 500    # pycomm3 fallback; Micro800
MULTISERVICE_READ_OVERHEAD = 10   # pycomm3.const
FRAGMENT_REPLY_OVERHEAD = 8       # sequence + reply header + type code, per fragment (estimate)
MAX_BIT = 63

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_INDEX = r"\[\d+(?:,\d+){0,2}\]"
_SEG = rf"{_IDENT}(?:{_INDEX})?"
_SYMBOL_RE = re.compile(rf"(?:Program:{_IDENT}\.)?{_SEG}(?:\.{_SEG})*")
_BIT_RE = re.compile(r"(?P<name>.+)\.(?P<bit>\d+)")

# Reply data bytes per value, from the configured dtype (CIP atomic sizes; STRING is the
# 88-byte Logix STRING structure). A bit is read through its host integer: assume a DINT.
_DTYPE_SIZE = {
    "bool": 1, "int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "int64": 8, "uint64": 8,
    "float32": 4, "float64": 8, "string": 88,
}
_BIT_HOST_SIZE = 4


@dataclass(frozen=True, slots=True)
class LogixAddress:
    read_name: str                 # name passed to plc.read(), without a trailing bit
    bit: int | None = None         # `.n` bit of an integer, decoded locally


def parse_logix_address(address: str, micro800: bool = False) -> LogixAddress:
    """Validate a symbolic address (bare `Tag`, `A.B`, `Program:X.Y`, `T[1,2]`, `T.5`).

    Light check only: the PLC's tag list decides existence (Phase 2). Raises ValueError.
    """
    text = (address or "").strip()
    if not text:
        raise ValueError("empty address")
    if "{" in text or "}" in text:
        raise ValueError(f"array block reads ({{n}}) are not supported in tag addresses: {text!r}")
    try:
        parse_pccc_address(text)
    except ValueError:
        pass
    else:
        raise ValueError("PCCC data-table address on a symbolic (Logix/Micro800) device")

    name, bit = text, None
    if (m := _BIT_RE.fullmatch(text)) and _SYMBOL_RE.fullmatch(m["name"]):
        name, bit = m["name"], int(m["bit"])
        if bit > MAX_BIT:
            raise ValueError(f"bit {bit} out of range (0-{MAX_BIT}) in {text!r}")
    if not _SYMBOL_RE.fullmatch(name):
        raise ValueError(f"illegal symbolic address {text!r}")
    if micro800 and name.startswith("Program:"):
        raise ValueError(f"program-scoped tags are not readable on Micro800 (globals only): {text!r}")
    return LogixAddress(name, bit)


def _member_size(index: int) -> int:
    # padded logical member segment: 8-bit 2 bytes, 16-bit 4 bytes, 32-bit 6 bytes
    return 2 if index <= 0xFF else 4 if index <= 0xFFFF else 6


def epath_size(read_name: str) -> int:
    """Bytes of the symbolic request path pycomm3 builds (incl. its length byte).

    Symbolic segments for every part; v21+ Logix uses a shorter instance-id path for the
    base tag, so this is the upper bound (exact for Micro800 and program tags).
    """
    size = 1
    for part in read_name.split("."):
        ident, _, idx = part.partition("[")
        n = len(ident.encode())
        size += 2 + n + (n % 2)
        if idx:
            size += sum(_member_size(int(i)) for i in idx.rstrip("]").split(","))
    return size


@dataclass(frozen=True, slots=True)
class LogixRead:
    """One distinct name in a batch, with the sizes pycomm3 uses to pack it."""

    name: str
    data_size: int                 # estimated reply data bytes
    path_size: int                 # request EPATH bytes

    @property
    def message_size(self) -> int:
        # sequence (2) + service (1) + path + element count (2), as ReadTagRequestPacket.message
        return 2 + 1 + self.path_size + 2

    def fragments(self, connection_size: int) -> int:
        per_fragment = max(1, connection_size - FRAGMENT_REPLY_OVERHEAD)
        return max(1, math.ceil(self.data_size / per_fragment))


@dataclass(frozen=True, slots=True)
class LogixItem:
    tag: str
    index: int                     # position of its name in LogixBlock.names
    dtype: str
    bit: int | None = None


@dataclass(kw_only=True)
class LogixBlock(Block):
    """One `plc.read(*names)` call; pycomm3 may split it into several CIP packets."""

    reads: list[LogixRead] = dc_field(default_factory=list)
    items: list[LogixItem] = dc_field(default_factory=list)
    micro800: bool = False
    connection_size: int = CONNECTION_SIZE_LARGE

    @property
    def names(self) -> list[str]:
        """Distinct symbolic names, in the order to pass to `plc.read(*names)`."""
        return [r.name for r in self.reads]

    def name_to_tags(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {r.name: [] for r in self.reads}
        for item in self.items:
            out[self.reads[item.index].name].append(item.tag)
        return out

    def wire_requests(self) -> int:
        """Estimated CIP request/reply round trips, following pycomm3's packing rules."""
        conn = self.connection_size
        if self.micro800 or len(self.reads) == 1:
            # single ReadTag requests (no Multiple Service Packet)
            return sum(
                r.fragments(conn) if r.data_size + r.message_size > conn else 1 for r in self.reads
            )
        total = 0
        groups = [0]                               # tags per Multiple Service Packet
        current = MULTISERVICE_READ_OVERHEAD
        for r in self.reads:
            size = r.data_size + r.message_size + 2
            if size > conn:
                total += r.fragments(conn)
                continue
            if current + size > conn:
                groups.append(0)
                current = MULTISERVICE_READ_OVERHEAD
            groups[-1] += 1
            current += size
        return total + sum(1 for g in groups if g)

    def describe(self) -> str:
        kind = "Micro800" if self.micro800 else "Logix"
        w = self.wire_requests()
        return (f"{kind} read {len(self.reads)} names / {len(self.items)} tags "
                f"({w} request{'s' if w != 1 else ''} @ {self.connection_size} B)")


def decode_block(block: LogixBlock, values: Sequence[Any] | None) -> dict[str, Any | DecodeError]:
    """Decode values returned by `plc.read(*block.names)`, aligned with `block.names`.

    `None` (pycomm3 Tag with an error) or a missing entry gives a `DecodeError` for the
    tags of that name only.
    """
    seq = list(values) if values is not None else []
    out: dict[str, Any | DecodeError] = {}
    for item in block.items:
        name = block.reads[item.index].name
        if item.index >= len(seq) or seq[item.index] is None:
            out[item.tag] = DecodeError(f"{name}: no value in reply")
            continue
        raw = seq[item.index]
        try:
            if item.bit is not None:
                if not isinstance(raw, int):
                    raise TypeError(f"expected an integer for a bit, got {type(raw).__name__}")
                raw = bool((raw >> item.bit) & 1)
            out[item.tag] = coerce_value(raw, item.dtype)
        except (TypeError, ValueError, OverflowError, KeyError) as exc:
            out[item.tag] = DecodeError(f"{name}: {item.tag}: {exc}")
    return out



# --- browse: the uploaded tag list as opencap tags (#11) --------------------------------------

# Logix / Micro800 atomic type -> opencap dtype, as `coerce_value` reads them. SINT/USINT have
# no 8-bit dtype and widen to 16 bits (lossless). Bit-string types (BYTE, WORD, DWORD, LWORD) and
# Logix BOOL arrays (DWORD-packed) are not in the table: pycomm3 returns them as bit lists.
LOGIX_DTYPES: dict[str, str] = {
    "BOOL": "bool", "SINT": "int16", "USINT": "uint16", "INT": "int16", "UINT": "uint16",
    "DINT": "int32", "UDINT": "uint32", "LINT": "int64", "ULINT": "uint64",
    "REAL": "float32", "LREAL": "float64", "SHORT_STRING": "string",
}
MAX_EXPANDED_ELEMENTS = 4096        # per array; a larger array is skipped with a reason


def _dims(info: Mapping[str, Any]) -> list[int]:
    return [int(d) for d in (info.get("dimensions") or []) if d]


def _elements(name: str, dims: list[int]) -> list[str]:
    """Element addresses in row-major order: `A[0]`, `A[1]`, ... or `A[0,0]`, `A[0,1]`, ..."""
    out = [""]
    for d in dims:
        out = [f"{prefix},{i}" if prefix else str(i) for prefix in out for i in range(d)]
    return [f"{name}[{index}]" for index in out]


def _type_name(info: Mapping[str, Any]) -> str:
    name = info.get("data_type_name")
    if name:
        return str(name)
    dt = info.get("data_type")
    return str(dt.get("name", "?")) if isinstance(dt, Mapping) else str(dt)


def _is_string(info: Mapping[str, Any]) -> bool:
    dt = info.get("data_type")
    return (isinstance(dt, Mapping) and "string" in dt) or _type_name(info) == "SHORT_STRING"


def _atomic_dtype(info: Mapping[str, Any]) -> str | None:
    if _is_string(info):
        return "string"
    if info.get("tag_type") == "struct":
        return None
    return LOGIX_DTYPES.get(_type_name(info).upper())


def _add_item(out: BrowseResult, name: str, info: Mapping[str, Any], dims: list[int], description: str,
              expand_arrays: bool) -> None:
    """One atomic tag or member (`dims` = its array dimensions), expanded to elements if asked."""
    type_name = _type_name(info)
    dtype = _atomic_dtype(info)
    if dtype is None:
        out.skipped[name] = f"type {type_name} is not supported by ab_logix (read it by hand if it is atomic)"
        return
    if not dims:
        out.tags.append(Tag(name=name, address=name, dtype=dtype, description=description))
        return
    shape = ",".join(map(str, dims))
    if type_name.upper() == "BOOL":
        out.skipped[name] = f"BOOL[{shape}]: Logix BOOL arrays are DWORD-packed; not supported"
        return
    if not expand_arrays:
        out.skipped[name] = (f"array {type_name}[{shape}]: ab_logix reads single elements only; "
                             "use --expand-arrays for one tag per element")
        return
    count = math.prod(dims)
    if count > MAX_EXPANDED_ELEMENTS:
        out.skipped[name] = f"array {type_name}[{shape}]: {count} elements, more than {MAX_EXPANDED_ELEMENTS}"
        return
    for element in _elements(name, dims):
        out.tags.append(Tag(name=element, address=element, dtype=dtype, description=f"{description} element"))


def browse_tag_list(tag_list: Mapping[str, Mapping[str, Any]], *, expand_arrays: bool = False) -> BrowseResult:
    """Pure: pycomm3's uploaded tag list (`plc.tags`) -> opencap tags, sorted by name (no case).

    Controller-scoped atomic tags and strings become one tag each; a UDT becomes one tag per
    atomic or string member (one level, D-024); arrays (of atomics, and array members) become one
    tag per element with `expand_arrays`. Everything else is listed in `skipped` with the reason.
    """
    out = BrowseResult()
    for name in sorted(tag_list, key=str.casefold):
        info = tag_list[name]
        type_name = _type_name(info)
        if name.startswith("Program:"):
            out.skipped[name] = "program-scoped tag: browse lists controller-scoped tags only"
            continue
        if info.get("external_access") == "None":
            out.skipped[name] = "External Access is None: the controller does not serve it"
            continue
        dims = _dims(info)
        dt = info.get("data_type")
        if info.get("tag_type") != "struct" or _is_string(info):
            _add_item(out, name, info, dims, type_name + (f"[{','.join(map(str, dims))}]" if dims else ""),
                      expand_arrays)
            continue
        if dims:
            out.skipped[name] = f"array of structures {type_name}[{','.join(map(str, dims))}]: not supported"
            continue
        members = (dt.get("internal_tags") or {}) if isinstance(dt, Mapping) else {}
        visible = (dt.get("attributes") if isinstance(dt, Mapping) else None) or list(members)
        if not visible:
            out.skipped[name] = f"structure {type_name} has no readable members"
            continue
        for mname in visible:
            member = members.get(mname)
            full = f"{name}.{mname}"
            if member is None:
                out.skipped[full] = f"member {mname} of {type_name} is not in the uploaded template"
                continue
            mtype = _type_name(member)
            if member.get("tag_type") == "struct":
                why = "STRING members are not simulated (D-024)" if _is_string(member) else \
                    "nested structures: browse expands one level (D-024)"
                out.skipped[full] = f"member {mtype}: {why}; add it by hand"
                continue
            mdims = [int(member["array"])] if member.get("array") else []
            _add_item(out, full, member, mdims, f"{type_name}.{mname}: {mtype}", expand_arrays)
    return out


# --- I/O: pure helpers ---------------------------------------------------------------------
# --- I/O: pure helpers ---------------------------------------------------------------------

_log = structlog.get_logger("opencap.drivers.ab_logix")

DEFAULT_PORT = 44818
BATCHING_MODES = ("auto", "on", "off")
STANDARD_FORWARD_OPEN_MAX = 511     # 9-bit connection size field of a standard Forward Open
PROBE_GIVE_UP = 2                   # probes in a row with no usable answer -> batching off, no more probes
CIP_SUCCESS = 0x00
CIP_SERVICE_NOT_SUPPORTED = 0x08
CIP_EMBEDDED_SERVICE_ERROR = 0x1E   # Multiple Service Packet understood; some embedded service failed
CIP_GENERAL_ERROR = 0xFF
# Per-tag CIP statuses that mean "this name/path is wrong" (permanent) -> bad_config:
# 0x04 path segment (IOI syntax) error, 0x05 path destination unknown, 0x16 object does not
# exist, 0x26 invalid path size; 0xFF with Logix extended status 0x2104 (offset/index out of
# range), 0x2105 (elements beyond the end of the tag), 0x2107 (data type mismatch).
CONFIG_STATUSES = frozenset({0x04, 0x05, 0x16, 0x26})
CONFIG_EXTENDED = frozenset({0x2104, 0x2105, 0x2107})
# Services pycomm3 must never send for this driver (WRITE_SERVICES, from _cip_common):
# Set_Attributes_All (0x02), Set_Attribute_List (0x04), Set_Attribute_Single (0x10),
# Write Tag (0x4D), Write Tag Fragmented (0x53).
_PROBE_TYPES = frozenset({
    "BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL",
})


def parse_batching(value: Any) -> str:
    """`params.batching` -> "auto" | "on" | "off" (booleans accepted). Raises ValueError."""
    if value is None:
        return "auto"
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in BATCHING_MODES:
        return text
    raise ValueError(f"ab_logix param batching={value!r} must be one of {', '.join(BATCHING_MODES)}")


def connection_path(device: Device) -> str:
    """pycomm3 path: `host:port` for Micro800, `host:port/<slot>` for a Logix slot,
    `host:port/<port>/<link>/...` for a longer CIP route in `params.path`. Raises ValueError."""
    params = device.params
    host = (device.host or "").strip()
    if not host:
        raise ValueError("ab_logix: empty host")
    base = f"{host}:{int(device.port or DEFAULT_PORT)}"
    if params.get("micro800"):
        return base          # Micro800: no backplane; pycomm3 strips its default bp/0 itself
    route = params.get("path")
    segs = [s.strip() for s in re.split(r"[,/\\]", str(route)) if s.strip()] if route else []
    if segs and not (len(segs) == 2 and segs[0].lower() in ("1", "bp", "backplane")):
        if len(segs) % 2:
            raise ValueError(f"ab_logix param path={route!r}: a CIP route needs port/link pairs")
        return base + "/" + "/".join(segs)
    slot = params.get("slot")
    if slot is None and segs:
        slot = segs[1]
    if slot is None:
        return base          # pycomm3 defaults to backplane slot 0
    try:
        slot_n = int(slot)
    except (TypeError, ValueError):
        raise ValueError(f"ab_logix param slot={slot!r} must be an integer") from None
    if not 0 <= slot_n <= 255:
        raise ValueError(f"ab_logix param slot={slot_n} out of range 0-255")
    return f"{base}/{slot_n}"


def _split_index(part: str) -> tuple[str, str]:
    i = part.find("[")
    return (part, "") if i < 0 else (part[:i], part[i:])


def resolve_read_name(
    name: str, tags: Mapping[str, Any], folded: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Check a read name against the controller's tag list (pycomm3 `plc.tags` shape).

    Returns `(plc_name, None)` when it exists, with the controller's spelling of the base
    tag and members (Logix names are case-insensitive, pycomm3's lookup is not), or
    `(None, reason)`. Array indexes are left to the controller.
    """
    base, *attrs = name.split(".")
    if base.startswith("Program:"):
        if not attrs:
            return None, f"{name!r}: program name without a tag"
        base = f"{base}.{attrs.pop(0)}"
    ident, index = _split_index(base)
    info = tags.get(ident)
    if info is None and folded is not None and (canon := folded.get(ident.casefold())) is not None:
        ident, info = canon, tags[canon]
    if info is None:
        return None, f"tag {ident!r} is not in the controller's tag list"
    if info.get("external_access") == "None":
        return None, f"tag {ident!r} has External Access None"
    parts = [ident + index]
    for attr in attrs:
        member, index = _split_index(attr)
        data_type = info.get("data_type")
        if info.get("tag_type") != "struct" or not isinstance(data_type, Mapping):
            return None, f"{'.'.join(parts)!r} is not a structure (no member {member!r})"
        members = data_type.get("internal_tags") or {}
        found = members.get(member)
        if found is None:
            canon = next((m for m in members if m.casefold() == member.casefold()), None)
            if canon is None:
                return None, f"structure {data_type.get('name', '?')} of {'.'.join(parts)!r} has no member {member!r}"
            member, found = canon, members[canon]
        parts.append(member + index)
        info = found
    return ".".join(parts), None


def probe_names(tags: Mapping[str, Any], count: int = 2) -> list[str]:
    """Readable scalar atomic controller tags for the batching probe (sorted, deterministic).
    One candidate is used twice; none gives []."""
    picked: list[str] = []
    for name in sorted(tags):
        t = tags[name]
        if (t.get("tag_type") == "atomic" and not t.get("dim") and t.get("data_type") in _PROBE_TYPES
                and t.get("external_access") != "None" and not name.startswith("Program:")):
            picked.append(name)
            if len(picked) == count:
                break
    if len(picked) == 1:
        picked *= count
    return picked


@dataclass(frozen=True, slots=True)
class NameResult:
    """Outcome of one name in `plc.oo_read(names)`."""

    value: Any
    error: str | None
    sent: bool                      # a request for this name went on the wire
    status: int | None = None       # CIP general status of its reply (None: no reply seen)
    ext_status: int | None = None   # first extended status word, if any

    @property
    def ok(self) -> bool:
        return self.error is None and self.value is not None


def is_config_status(status: int | None, ext_status: int | None = None) -> bool:
    return status in CONFIG_STATUSES or (status == CIP_GENERAL_ERROR and ext_status in CONFIG_EXTENDED)


def result_quality(r: NameResult) -> str | None:
    """Quality for a failed name, None when it has a value (decoding decides then)."""
    if r.ok:
        return None
    if not r.sent:
        return "bad_config"        # pycomm3 could not build the request (e.g. "Tag doesn't exist")
    if r.status not in (None, CIP_SUCCESS):
        return "bad_config" if is_config_status(r.status, r.ext_status) else "bad_comm"
    if r.status == CIP_SUCCESS:
        return "bad_decode"        # good reply, but pycomm3 could not parse the value
    return "bad_comm"


class WholeReplyError(Exception):
    """A reply that is unusable as a whole: a Multiple Service Packet rejected (general
    status other than 0x00/0x1E), unparseable or with the wrong number of replies, or an
    encapsulation-level error on any read."""

    def __init__(self, reason: str, *, status: int | None = None, multi: bool = False,
                 encapsulation: bool = False) -> None:
        super().__init__(reason)
        self.status = status
        self.multi = multi
        self.encapsulation = encapsulation


def _reply_status(raw: bytes | None) -> tuple[int | None, int | None]:
    """(general status, first extended status word) of a SendUnitData reply, as pycomm3 lays
    it out (status at byte 48, extended size in words at 49, words from 50). pycomm3 pads
    embedded Multiple Service Packet replies to the same offsets."""
    if not raw or len(raw) < 50:
        return None, None
    status = raw[48]
    ext = int.from_bytes(raw[50:52], "little") if status and raw[49] and len(raw) >= 52 else None
    return status, ext


def _status_text(status: int | None) -> str:
    if status is None:
        return "no status"
    try:
        from pycomm3.cip import SERVICE_STATUS
        text = SERVICE_STATUS.get(status, "unknown")
    except ImportError:   # pragma: no cover
        text = "unknown"
    return f"CIP status {status:#04x} ({text})"


# `_chain`, `is_timeout`, `describe_exc`, `is_comm_error`: shared with ab_pccc (_cip_common).


# --- I/O: pycomm3 LogixDriver subclass -------------------------------------------------------

@functools.cache
def logix_driver_class() -> type:
    """The pycomm3 `LogixDriver` subclass the driver instantiates (built lazily, D-002).

    Hooks, each minimal and pinned by tests against pycomm3 1.2.16:
    - `_read_build_requests`: `oo_batching` decides Multiple Service Packet vs one request
      per name, Micro800 included (pycomm3 alone never batches a Micro800).
    - `send`: records which reads went out and their CIP status per name; raises
      `WholeReplyError` for an unusable reply (pycomm3 would raise from its parser or hand
      out empty results); refuses write packets.
    - `_receive`: keeps the last raw reply so a reply pycomm3 cannot parse still yields its status.
    - `open`: installs a socket reader that reads exactly one encapsulation message and
      fails on a peer close (pycomm3's reader loops forever on a close mid-reply).
    - `write`, `set_plc_time`, and Set/Write services in `generic_message` raise (read-only).
    """
    from pycomm3 import LogixDriver
    from pycomm3.cip_driver import with_forward_open
    from pycomm3.exceptions import CommError as PycommCommError
    from pycomm3.exceptions import RequestError

    OoSocket = oo_socket_class()      # one message per receive(), fails fast on a peer close

    class OoLogixDriver(LogixDriver):
        socket_class = OoSocket

        def __init__(self, path: str, *args: Any, **kwargs: Any) -> None:
            super().__init__(path, *args, **kwargs)
            self.oo_batching: bool | None = None      # None: pycomm3's own choice
            self._oo_status: dict[int, tuple[int | None, int | None]] = {}
            self._oo_sent: set[int] = set()
            self._oo_last_raw: bytes | None = None
            self.oo_multi_sent = 0                    # Multiple Service Packets sent by the last oo_read

        # --- connection ---

        @property
        def oo_micro800(self) -> bool:
            return bool(self._micro800)

        def open(self) -> bool:
            if self._sock is None:
                self._sock = OoSocket(self._cfg["socket_timeout"])
            return super().open()

        def oo_adopt_tags(self, tags: dict, data_types: dict) -> None:
            """Reuse a tag list uploaded on an earlier connection (pycomm3's documented way)."""
            self._tags = tags
            self._data_types = data_types

        def oo_symbolic(self) -> None:
            """Address tags by name, never by Symbol instance id (safe with a reused tag list)."""
            self._cfg["use_instance_ids"] = False

        def oo_forward_open(self) -> None:
            with_forward_open(lambda _plc: None)(self)    # raises ResponseError if it fails

        def oo_abort(self) -> None:
            """Drop the TCP connection without Forward Close / UnRegister Session (the peer
            is not answering or the socket is dead)."""
            self._target_is_connected = False
            self._session = 0
            self.close()

        # --- reads ---

        def oo_read(self, names: Sequence[str]) -> list[NameResult]:
            self._oo_status = {}
            self._oo_sent = set()
            self.oo_multi_sent = 0
            tags = self.read(*names)
            if not isinstance(tags, list):            # pycomm3 returns a bare Tag for one name
                tags = [tags]
            out = []
            for i, t in enumerate(tags):
                status, ext = self._oo_status.get(i, (None, None))
                out.append(NameResult(t.value, t.error, i in self._oo_sent, status, ext))
            return out

        def _read_build_requests(self, parsed_tags: dict) -> list:
            if self.oo_batching is None:
                return super()._read_build_requests(parsed_tags)
            if self.oo_batching and len(parsed_tags) != 1:
                return self._read_build_multi_requests(parsed_tags)
            requests = (self._read_build_single_request(parsed_tags[i]) for i in parsed_tags)
            return [r for r in requests if r is not None]

        def send(self, request: Any) -> Any:
            kind = getattr(request, "type_", None)
            if kind == "write":
                raise RequestError("opencap is read-only: write request refused")
            if kind not in ("read", "multi"):
                return super().send(request)
            multi = kind == "multi"
            if multi:
                self.oo_multi_sent += 1
                self._oo_sent.update(r.request_id for r in request.requests)
            else:
                self._oo_sent.add(request.request_id)
            self._oo_last_raw = None
            try:
                response = super().send(request)
            except PycommCommError:
                raise
            except Exception as exc:
                if not multi:
                    raise
                status, _ = _reply_status(self._oo_last_raw)
                raise WholeReplyError(
                    f"unreadable Multiple Service Packet reply, {_status_text(status)}: {exc!r}",
                    status=status, multi=True) from exc
            cmd = response.command_status
            if cmd not in (None, CIP_SUCCESS):
                raise WholeReplyError(f"encapsulation status {cmd:#06x}", multi=multi, encapsulation=True)
            if multi:
                status = response.service_status
                if status not in (CIP_SUCCESS, CIP_EMBEDDED_SERVICE_ERROR):
                    raise WholeReplyError(f"Multiple Service Packet rejected: {_status_text(status)}",
                                          status=status, multi=True)
                if len(response.responses) != len(request.requests):
                    raise WholeReplyError(
                        f"Multiple Service Packet reply has {len(response.responses)} replies "
                        f"for {len(request.requests)} requests", status=status, multi=True)
                for resp in response.responses:
                    self._oo_status[resp.request.request_id] = _reply_status(resp.raw)
            else:
                self._oo_status[request.request_id] = (
                    _reply_status(response.raw) if response.raw else (response.service_status, None))
            return response

        def _receive(self) -> bytes:
            raw = super()._receive()
            self._oo_last_raw = raw
            return raw

        # --- read-only guards ---

        def write(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("opencap is read-only: writes to PLCs are not supported")

        def set_plc_time(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("opencap is read-only: writes to PLCs are not supported")

        def generic_message(self, service: Any, *args: Any, **kwargs: Any) -> Any:
            code = service[0] if isinstance(service, (bytes, bytearray)) else int(service)
            if code in WRITE_SERVICES:
                raise NotImplementedError(f"opencap is read-only: CIP service {code:#04x} refused")
            return super().generic_message(service, *args, **kwargs)

    return OoLogixDriver


# --- I/O: driver state ---------------------------------------------------------------------------

@dataclass(eq=False)
class _TagCache:
    """Tag list uploaded from one controller, kept across reconnects of one driver instance."""

    key: tuple                                  # controller identity it was uploaded from
    tags: Mapping[str, Any]
    data_types: Mapping[str, Any]
    folded: dict[str, str]                      # casefold(name) -> controller spelling
    resolved: dict[str, tuple[str | None, str | None]] = dc_field(default_factory=dict)


@dataclass(eq=False)
class _Session:
    """One open pycomm3 connection (touched by pycomm3 calls only on the executor)."""

    plc: Any
    micro800: bool
    batching: bool
    batching_reason: str
    identity: dict[str, Any]
    cache: _TagCache
    dead: bool = False          # no longer usable (closed, or failed in the executor)
    closed: bool = False        # pycomm3 close/abort already done


# `_Attempt` (connect hand-over to the executor job): shared with ab_pccc (_cip_common).


class _ReadFailure(Exception):
    def __init__(self, reason: str, *, lost: bool = False) -> None:
        super().__init__(reason)
        self.lost = lost


def _identity_snapshot(info: Mapping[str, Any]) -> dict[str, Any]:
    rev = info.get("revision")
    if isinstance(rev, Mapping):
        revision = f"{rev.get('major', '?')}.{rev.get('minor', '?')}"
    else:
        revision = None if rev is None else str(rev)
    out = {
        "vendor": info.get("vendor"),
        "product_name": info.get("product_name"),
        "product_code": info.get("product_code"),
        "revision": revision,
        "serial": info.get("serial"),
    }
    for key, src in (("product_type", "product_type"), ("keyswitch", "keyswitch"), ("program_name", "name")):
        if info.get(src) is not None:
            out[key] = info[src]
    return out


def _identity_key(identity: Mapping[str, Any]) -> tuple:
    return tuple(identity.get(k) for k in ("product_name", "product_code", "serial", "revision"))


# --- driver -------------------------------------------------------------------------------------

class AbLogixDriver(Driver):
    """ControlLogix/CompactLogix/Micro800 symbolic tags via pycomm3 `LogixDriver` (sync).

    Contract: docs/phase2_contracts.md §1. Every pycomm3 call runs on the channel's
    single-thread executor (`_run_sync`). One attempt per call: retries, backoff and
    reconnects belong to the ChannelWorker.
    """

    name: ClassVar[str] = "ab_logix"
    is_async: ClassVar[bool] = False
    transport: ClassVar[str] = "tcp (EtherNet/IP)"
    supports_browse: ClassVar[bool] = True      # Driver.browse() maps the uploaded tag list (#11, D-077)
    address_examples: ClassVar[tuple[str, ...]] = (
        "Pump1_Speed", "Program:MainProgram.Counter", "Recipe[3]", "Motor1.Status.Running", "Flags.5")
    params_schema: ClassVar[dict[str, Any]] = sc.params_schema("ab_logix params", {
        "micro800": sc.boolean("the device is a Micro800 (no backplane, no tag-list upload)", False),
        "path": sc.string("CIP route after the IP address, as port,link pairs, e.g. 1,0", nullable=True),
        "slot": sc.integer("backplane slot of the controller", minimum=0, maximum=255, nullable=True),
        "max_batch": sc.integer("tags per read call", DEFAULT_MAX_BATCH, minimum=1),
        "batching": {**sc.string("Multiple Service Packet batching (Micro800): auto probes on connect",
                                 "auto", enum=(*BATCHING_MODES, True, False)), "type": ["string", "boolean"]},
        "connection_size": sc.integer("CIP connection size in bytes (wire estimate)", minimum=1, nullable=True),
    })

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.batching_mode = parse_batching(device.params.get("batching"))   # ValueError on bad params
        self.path = connection_path(device)
        conn = device.params.get("connection_size")
        if conn is not None and (isinstance(conn, bool) or not isinstance(conn, int) or conn < 1):
            raise ValueError(f"ab_logix param connection_size={conn!r} must be a positive integer")
        self._session: _Session | None = None
        self._tag_cache: _TagCache | None = None          # survives reconnects of this instance
        self._probe_misses = 0                            # probes in a row with no usable answer
        self._batching_off: str | None = None             # sticky: batching off without probing
        self._last_batching: tuple[bool, str] | None = None
        self._warned_kind = False

    @property
    def micro800(self) -> bool:
        return bool(self.device.params.get("micro800", False))

    @property
    def connection_size(self) -> int:
        value = self.device.params.get("connection_size")
        if value is None:
            return CONNECTION_SIZE_STANDARD if self.micro800 else CONNECTION_SIZE_LARGE
        return int(value)

    def plan(self, tags: list[Tag]) -> Plan:
        micro800 = self.micro800
        conn = self.connection_size
        max_batch = int(self.device.params.get("max_batch") or DEFAULT_MAX_BATCH)
        if max_batch < 1:
            raise ValueError(f"ab_logix param max_batch={max_batch} must be >= 1")

        invalid: dict[str, str] = {}
        by_name: dict[str, list[tuple[Tag, LogixAddress]]] = defaultdict(list)
        for tag in tags:
            if not tag.enabled:
                continue
            try:
                addr = parse_logix_address(tag.address, micro800=micro800)
                if addr.bit is not None and tag.dtype == "string":
                    raise ValueError(f"dtype string is not supported on a bit address ({tag.address})")
            except ValueError as exc:
                invalid[tag.name] = str(exc)
                continue
            by_name[addr.read_name].append((tag, addr))

        blocks: list[Block] = []
        for chunk in batched(sorted(by_name), max_batch):
            reads = [
                LogixRead(name, _data_size(by_name[name]), epath_size(name)) for name in chunk
            ]
            items = sorted(
                (LogixItem(tag.name, i, tag.dtype, addr.bit)
                 for i, name in enumerate(chunk) for tag, addr in by_name[name]),
                key=lambda it: (it.index, it.tag),
            )
            blocks.append(LogixBlock(
                tags=[it.tag for it in items], reads=reads, items=items,
                micro800=micro800, connection_size=conn,
            ))
        return Plan(blocks=blocks, invalid=dict(sorted(invalid.items())))

    # --- I/O ---

    def connect_timeout_s(self) -> float | None:
        """Connect = Forward Open + identity + batching probe, plus a tag-list upload the first
        time (one paged request per few hundred tags, and UDT templates). Give it room."""
        per_request = self.device.timeout_ms / 1000
        if self._tag_cache is None:
            return max(60.0, 20 * per_request)
        return 5 * per_request + 1.0

    @property
    def connected(self) -> bool:
        """True while the CIP connection is believed usable. pycomm3 notices a peer close
        only on the next request: that read raises ConnectionLost."""
        s = self._session
        return s is not None and not s.dead

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def _where(self) -> str:
        return f"{self.device.id} ({self.path})"

    async def connect(self) -> None:
        """Open the connection, load the tag list (uploaded once per driver instance and
        controller identity), force symbolic addressing, Forward Open, decide batching.

        An open connection is closed first. Raises ConnectionLost on any failure.
        """
        await self.close()
        if self._executor is None:
            raise RuntimeError(f"{self.name}: sync driver needs the channel executor")
        attempt = _Attempt()
        started = time.monotonic()
        try:
            session = await self._run_sync(self._open_sync, attempt)
        except asyncio.CancelledError:
            leftover = attempt.abandon()
            if leftover is not None:
                self._close_later(leftover)
            raise
        except Exception as exc:
            reason = describe_exc(exc)
            _log.debug("ab_logix connect", device=self.device.id, result="failed", reason=reason,
                       ms=round((time.monotonic() - started) * 1000, 1))
            raise ConnectionLost(f"{self._where}: connect failed: {reason}") from exc
        if session is None:    # pragma: no cover - only after abandon(), i.e. cancelled
            raise ConnectionLost(f"{self._where}: connect abandoned")
        self._session = session
        _log.debug("ab_logix connect", device=self.device.id, result="ok", micro800=session.micro800,
                   batching=session.batching, ms=round((time.monotonic() - started) * 1000, 1))

    async def close(self) -> None:
        """Close the connection (Forward Close, UnRegister Session). Idempotent, never raises."""
        session, self._session = self._session, None
        if session is None or session.closed:
            return
        session.dead = True
        try:
            await self._run_sync(self._dispose_sync, session, True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:     # executor missing or shut down: drop the socket at least
            _log.debug("ab_logix close error", device=self.device.id, error=describe_exc(exc))
            _abort_socket(session)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """Read one block with one `plc.read(*names)` call (pycomm3 may send several packets).

        Names missing from the tag list are `bad_config` without going on the wire. Raises
        ConnectionLost (not connected, connection reset/closed) or CommError (reply timeout,
        after closing; a reply unusable as a whole).
        """
        if not isinstance(block, LogixBlock):
            raise TypeError(f"ab_logix cannot read {type(block).__name__}")
        session = self._session
        if session is None or session.dead:
            raise ConnectionLost(f"{self._where}: not connected")
        plc_names, missing = self._resolve(session, block)
        ts = datetime.now(UTC)
        results: dict[str, NameResult] = {}
        if plc_names:
            wire = list(dict.fromkeys(plc_names.values()))
            try:
                replies, ts = await self._run_sync(self._read_sync, session, wire)
            except _ReadFailure as exc:
                if session.dead and self._session is session:
                    self._session = None
                err = ConnectionLost if exc.lost else CommError
                raise err(f"{self._where} {block.describe()}: {exc}") from exc.__cause__
            by_plc = dict(zip(wire, replies))
            results = {name: by_plc[p] for name, p in plc_names.items()}
        return self._values(block, results, missing, ts)

    async def identity(self) -> dict[str, Any]:
        """Controller identity captured at connect (no extra request): vendor, product_name,
        product_code, revision, serial, micro800, batching (+ batching_reason, connection_size,
        tag_count, product_type, keyswitch, program_name when known). {} when not connected."""
        s = self._session
        if s is None or s.dead:
            return {}
        return {
            **s.identity, "micro800": s.micro800, "batching": s.batching,
            "batching_reason": s.batching_reason, "tag_count": len(s.cache.tags),
        }

    async def browse(self, *, expand_arrays: bool = False) -> BrowseResult:
        """Tags generated from the tag list uploaded at connect (#11): no extra request, so only
        the read/list services of `connect()` ever reach the controller."""
        s = self._session
        if s is None or s.dead:
            raise ConnectionLost(f"{self._where}: not connected")
        return browse_tag_list(s.cache.tags, expand_arrays=expand_arrays)

    # --- executor side (every pycomm3 call happens below, on the channel executor) ---

    def _open_sync(self, attempt: _Attempt) -> _Session | None:
        if attempt.abandoned:
            return None
        plc = logix_driver_class()(self.path, init_tags=False)
        plc._cfg["socket_timeout"] = self.timeout_s
        conn = self.device.params.get("connection_size")
        if conn is not None:
            plc._cfg["connection_size"] = int(conn)
            plc._cfg["extended forward open"] = int(conn) > STANDARD_FORWARD_OPEN_MAX
        try:
            if not plc.open():
                raise ConnectionError("EtherNet/IP session not registered")
            identity = _identity_snapshot(plc.info)
            micro800 = plc.oo_micro800
            self._check_kind(micro800, identity)
            cache = self._load_tags(plc, identity)
            plc.oo_symbolic()
            plc.oo_forward_open()
            identity["connection_size"] = plc.connection_size
            if attempt.abandoned:
                raise _Abandoned()
            batching, reason = self._decide_batching(plc, micro800, cache)
            plc.oo_batching = batching
        except _Abandoned:
            _close_quietly(plc, graceful=True)
            return None
        except BaseException as exc:
            _close_quietly(plc, graceful=not is_comm_error(exc))
            raise
        session = _Session(plc, micro800, batching, reason, identity, cache)
        if not attempt.finish(session):
            self._dispose_sync(session, True)
            return None
        return session

    def _check_kind(self, micro800: bool, identity: Mapping[str, Any]) -> None:
        if micro800 != self.micro800 and not self._warned_kind:
            self._warned_kind = True
            _log.warning("ab_logix.kind_mismatch", device=self.device.id, product_name=identity.get("product_name"),
                         configured_micro800=self.micro800, detected_micro800=micro800,
                         note="pycomm3 follows the detected kind; check params.micro800")

    def _load_tags(self, plc: Any, identity: Mapping[str, Any]) -> _TagCache:
        key = _identity_key(identity)
        cache = self._tag_cache
        if cache is not None and cache.key == key:
            plc.oo_adopt_tags(cache.tags, cache.data_types)
            _log.debug("ab_logix.tag_list", device=self.device.id, source="reused", count=len(cache.tags))
            return cache
        if cache is not None:
            _log.info("ab_logix.identity_changed", device=self.device.id, note="re-uploading the tag list",
                      before=dict(zip(("product_name", "product_code", "serial", "revision"), cache.key)),
                      after=dict(zip(("product_name", "product_code", "serial", "revision"), key)))
        started = time.monotonic()
        plc.get_tag_list(program="*")
        tags, data_types = plc.tags, plc.data_types
        folded: dict[str, str] = {}
        for name in tags:
            folded.setdefault(name.casefold(), name)
        cache = _TagCache(key, tags, data_types, folded)
        self._tag_cache = cache      # kept even if this attempt is abandoned: the next one reuses it
        _log.info("ab_logix.tag_list", device=self.device.id, source="uploaded", count=len(tags),
                  data_types=len(data_types), ms=round((time.monotonic() - started) * 1000, 1))
        return cache

    def _decide_batching(self, plc: Any, micro800: bool, cache: _TagCache) -> tuple[bool, str]:
        mode = self.batching_mode
        if not micro800:
            decision = (mode != "off", "Logix: Multiple Service Packet (pycomm3 standard)"
                        if mode != "off" else "off by params.batching")
        elif mode == "on":
            decision = (True, "on by params.batching (no probe)")
        elif mode == "off":
            decision = (False, "off by params.batching")
        elif self._batching_off is not None:
            decision = (False, self._batching_off)
        else:
            decision = self._probe(plc, cache)
        log = _log.info if decision != self._last_batching else _log.debug   # INFO once, then on change
        log("ab_logix.batching", device=self.device.id, micro800=micro800, mode=mode,
            batching=decision[0], reason=decision[1])
        self._last_batching = decision
        return decision

    def _probe(self, plc: Any, cache: _TagCache) -> tuple[bool, str]:
        """Micro800 + batching auto: one 2-name Multiple Service Packet read of existing tags.

        Accepted (every probe name read) -> on. Rejected (e.g. status 0x08), malformed reply
        or a probe name failing -> off for this connection. No reply / connection dropped ->
        the connect fails (ConnectionLost); after PROBE_GIVE_UP such probes in a row, batching
        stays off for this driver instance without probing.
        """
        names = probe_names(cache.tags)
        if not names:
            return False, "probe impossible: no readable scalar controller tag"
        plc.oo_batching = True
        try:
            results = plc.oo_read(names)
        except WholeReplyError as exc:
            if exc.encapsulation:
                raise
            self._probe_misses = 0
            return False, f"probe rejected: {exc}"
        except Exception as exc:
            if is_comm_error(exc):
                self._probe_misses += 1
                if self._probe_misses >= PROBE_GIVE_UP:
                    self._batching_off = (f"off without probing: {self._probe_misses} probes in a row got no "
                                          f"usable answer (last: {describe_exc(exc)})")
                    _log.warning("ab_logix.batching_probe_given_up", device=self.device.id,
                                 reason=self._batching_off)
                else:
                    _log.warning("ab_logix.batching_probe_no_answer", device=self.device.id,
                                 error=describe_exc(exc), note="connect fails; the next connect probes again")
                raise
            self._probe_misses = 0
            return False, f"probe reply not usable: {describe_exc(exc)}"
        self._probe_misses = 0
        if not plc.oo_multi_sent:
            return False, "probe did not go out as a Multiple Service Packet (pycomm3 hook not effective?)"
        bad = [(n, r) for n, r in zip(names, results) if not r.ok]
        if bad:
            name, r = bad[0]
            return False, f"probe read of {name!r} failed: {r.error or 'no value'}"
        return True, f"probe ok: {len(names)}-name Multiple Service Packet accepted"

    def _read_sync(self, session: _Session, names: list[str]) -> tuple[list[NameResult], datetime]:
        plc = session.plc
        started = time.monotonic()
        try:
            replies = plc.oo_read(names)
        except WholeReplyError as exc:
            if exc.encapsulation:
                self._dispose_sync(session, False)
                raise _ReadFailure(f"{exc}; connection closed", lost=True) from exc
            if exc.multi:
                self._demote_batching(session, exc)
            raise _ReadFailure(str(exc)) from exc
        except Exception as exc:
            if is_comm_error(exc):
                self._dispose_sync(session, False)
                if is_timeout(exc):
                    raise _ReadFailure(f"no reply within {self.device.timeout_ms} ms; connection closed") from exc
                raise _ReadFailure(f"connection lost: {describe_exc(exc)}", lost=True) from exc
            raise _ReadFailure(f"pycomm3 read failed: {describe_exc(exc)}") from exc
        if len(replies) != len(names):     # pragma: no cover - pycomm3 returns one Tag per name
            raise _ReadFailure(f"pycomm3 returned {len(replies)} results for {len(names)} names")
        _log.debug("ab_logix read", device=self.device.id, names=len(names), batching=session.batching,
                   failed=sum(1 for r in replies if not r.ok), ms=round((time.monotonic() - started) * 1000, 1))
        return replies, datetime.now(UTC)

    def _demote_batching(self, session: _Session, exc: WholeReplyError) -> None:
        """A batched read failed as a whole on a Micro800 whose batching came from the probe:
        switch to one request per name for good (this driver instance)."""
        if not (session.micro800 and session.batching and self.batching_mode == "auto"):
            return
        reason = f"off after a failed batched read: {exc}"
        session.plc.oo_batching = False
        session.batching, session.batching_reason = False, reason
        self._batching_off = reason
        self._last_batching = (False, reason)
        _log.warning("ab_logix.batching_demoted", device=self.device.id, reason=reason)

    def _dispose_sync(self, session: _Session, graceful: bool) -> None:
        session.dead = True
        if session.closed:
            return
        session.closed = True
        _close_quietly(session.plc, graceful=graceful)
        _log.debug("ab_logix close", device=self.device.id, graceful=graceful)

    def _close_later(self, session: _Session) -> None:
        session.dead = True
        try:
            assert self._executor is not None
            self._executor.submit(self._dispose_sync, session, True)
        except Exception:
            _abort_socket(session)

    # --- loop side, no pycomm3 calls ---

    def _resolve(self, session: _Session, block: LogixBlock) -> tuple[dict[str, str], dict[str, str]]:
        """Split block names into {name: controller name} and {name: reason missing}.
        Resolutions are cached with the tag list; each problem is logged once."""
        cache = session.cache
        found: dict[str, str] = {}
        missing: dict[str, str] = {}
        new_missing: list[str] = []
        recased: list[tuple[str, str]] = []
        for name in block.names:
            hit = cache.resolved.get(name)
            if hit is None:
                hit = cache.resolved[name] = resolve_read_name(name, cache.tags, cache.folded)
                if hit[0] is None:
                    new_missing.append(name)
                elif hit[0] != name:
                    recased.append((name, hit[0]))
            plc_name, reason = hit
            if plc_name is None:
                missing[name] = reason or "missing"
            else:
                found[name] = plc_name
        if new_missing:
            tags_of = block.name_to_tags()
            _log.warning("ab_logix.tags_missing", device=self.device.id, count=len(new_missing),
                         names=new_missing[:20], tags=[t for n in new_missing[:20] for t in tags_of.get(n, [])][:20],
                         reasons=[missing[n] for n in new_missing[:5]], quality="bad_config")
        if recased:
            _log.info("ab_logix.tags_recased", device=self.device.id, count=len(recased),
                      names=[f"{a} -> {b}" for a, b in recased[:20]],
                      note="address spelling differs from the controller's; read with the controller's")
        return found, missing

    def _values(self, block: LogixBlock, results: Mapping[str, NameResult], missing: Mapping[str, str],
                ts: datetime) -> dict[str, TagValue]:
        failed: dict[str, str] = {}
        values: list[Any] = []
        for name in block.names:
            if name in missing:
                failed[name] = "bad_config"
                values.append(None)
                continue
            r = results[name]
            quality = result_quality(r)
            if quality is None:
                values.append(r.value)
            else:
                failed[name] = quality
                values.append(None)
                _log.debug("ab_logix tag error", device=self.device.id, name=name, quality=quality,
                           status=r.status, ext_status=r.ext_status, error=r.error)
        decoded = decode_block(block, values)
        out: dict[str, TagValue] = {}
        for item in block.items:
            name = block.reads[item.index].name
            if name in failed:
                out[item.tag] = TagValue(None, failed[name], ts)
                continue
            value = decoded.get(item.tag, DecodeError(f"{name}: not decoded"))
            if isinstance(value, DecodeError):
                _log.debug("ab_logix decode error", device=self.device.id, tag=item.tag, reason=value.reason)
                out[item.tag] = TagValue(None, "bad_decode", ts)
            else:
                out[item.tag] = TagValue(value, "good", ts)
        return out


class _Abandoned(Exception):
    pass


def _close_quietly(plc: Any, *, graceful: bool) -> None:
    try:
        if graceful:
            plc.close()
        else:
            plc.oo_abort()
    except Exception as exc:
        _log.debug("ab_logix close error", error=describe_exc(exc), graceful=graceful)
        if graceful:
            try:
                plc.oo_abort()
            except Exception:   # pragma: no cover
                pass


def _abort_socket(session: _Session) -> None:
    """Last resort when the executor is gone: close the TCP socket without any CIP traffic."""
    session.dead = session.closed = True
    sock = getattr(session.plc, "_sock", None)
    try:
        if sock is not None:
            sock.close()
    except Exception:   # pragma: no cover
        pass


def _data_size(entries: list[tuple[Tag, LogixAddress]]) -> int:
    return max(_BIT_HOST_SIZE if addr.bit is not None else _DTYPE_SIZE[tag.dtype] for tag, addr in entries)
