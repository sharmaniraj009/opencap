"""OPC UA client driver (PLAN.md §5.1) on asyncua: subscriptions (monitored items), not polling.

Pure part (no asyncua import, D-002): the NodeId parser, `OpcUaParams`, the block plan, the
StatusCode -> quality mapping and the value coercion. asyncua is imported in `connect()`.

How the subscription fits the core's polling model (docs/decisions.md D-045)
- `connect()` opens a session (security policy and message mode from params, user name and
  password from the environment variables NAMED in params), creates ONE subscription for the
  device and one monitored item per distinct NodeId of every enabled tag (all rate groups),
  then reads those nodes once, so the first `read()` has values without waiting for the
  first publish. Publishing interval = `publishing_interval_ms`, else the fastest rate
  group; sampling interval = `sampling_interval_ms`, else the tag's own rate; queue size 1.
- Data-change notifications update an in-memory sample per node. `read(block)` never goes on
  the wire: it returns the latest sample of each tag, with `source_ts` = the server's
  SourceTimestamp (ServerTimestamp when absent). The worker still "reads" every rate period,
  so the cache, publisher and health keep working unchanged.
- A monitored item reports only changes, so a constant value keeps an old SourceTimestamp.
  `stale_by_read_time = True` tells the worker to judge staleness by when `read()` last
  returned the tag good, not by SourceTimestamp (which also depends on the server's clock).
- The session is gone (TCP closed, asyncua's keep-alive probe failed, a bad subscription
  status, or no publish response or keep-alive for 1.5 x the keep-alive period + 2 x the
  timeout) -> `connected` turns False and `read()` raises `ConnectionLost`. The worker closes
  the driver and reconnects with its own backoff; `connect()` subscribes everything again.
  asyncua's own auto-reconnect is off (as pymodbus's is, D-013).
- StatusCode -> quality: Good* -> good; Uncertain* -> stale (the uncertain value is not stored,
  the last good one is kept); BadNodeIdUnknown, BadNodeIdInvalid, BadAttributeIdInvalid,
  BadNotReadable, BadUserAccessDenied -> bad_config; every other Bad -> bad_comm. A monitored
  item the server refuses keeps its status until the next reconnect.

Read-only: the driver sends no Write, Call, HistoryUpdate or node-management request.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import structlog

from opencap.config.models import Device, Quality, Tag
from opencap.core.cache import TagValue
from opencap.drivers.base import Block, ConnectionLost, DecodeError, Driver, Plan

_log = structlog.get_logger("opencap.drivers.opcua_client")

# --- StatusCode -> quality (numbers, so the pure part needs no asyncua) ------------------------

CONFIG_STATUS: dict[int, str] = {      # "this node, as configured, cannot be read" -> bad_config
    0x80340000: "BadNodeIdUnknown",
    0x80330000: "BadNodeIdInvalid",
    0x80350000: "BadAttributeIdInvalid",
    0x803A0000: "BadNotReadable",
    0x801F0000: "BadUserAccessDenied",
}
BAD_NODE_ID_UNKNOWN = 0x80340000
BAD_NO_DATA = 0x80310000               # BadNoCommunication: subscribed, nothing received yet
_CODE_MASK = 0xFFFF0000                # the low 16 bits are info bits (limits, overflow)


def status_quality(code: int | None) -> Quality:
    """OPC UA StatusCode -> opencap quality (module docstring, D-045)."""
    code = code or 0
    severity = (code >> 30) & 0b11
    if severity == 0:
        return "good"
    if severity == 1:
        return "stale"                 # Uncertain: usable maybe, but not a confirmed good value
    if (code & _CODE_MASK) in CONFIG_STATUS:
        return "bad_config"
    return "bad_comm"


def status_name(code: int | None) -> str:
    """'BadNodeIdUnknown' for a code (asyncua's table when installed, else hex)."""
    code = (code or 0) & _CODE_MASK
    try:
        from asyncua import ua

        return ua.StatusCode(code).name
    except Exception:  # pragma: no cover - asyncua missing or unknown code
        return CONFIG_STATUS.get(code, f"0x{code:08X}")


# --- NodeId parser ------------------------------------------------------------------------------

_NS_MAX = 0xFFFF
_UINT32_MAX = 0xFFFFFFFF


@dataclass(frozen=True)
class OpcUaAddress:
    """A parsed NodeId string. `namespace_uri` (nsu=) is resolved to an index at connect."""

    namespace: int                     # 0 when nsu= is used
    namespace_uri: str | None
    kind: str                          # "i" numeric, "s" string, "g" GUID, "b" ByteString (base64)
    identifier: int | str

    @property
    def key(self) -> str:
        """Canonical text; tags with the same key share one monitored item."""
        if self.namespace_uri is not None:
            prefix = f"nsu={self.namespace_uri};"
        else:
            prefix = f"ns={self.namespace};" if self.namespace else ""
        return f"{prefix}{self.kind}={self.identifier}"

    def with_index(self, index: int) -> str:
        """NodeId text with the namespace index (for asyncua)."""
        prefix = f"ns={index};" if index else ""
        return f"{prefix}{self.kind}={self.identifier}"


def parse_node_id(text: str) -> OpcUaAddress:
    """Parse `ns=7;s=LTI002_PV`, `i=2258`, `ns=4;g=<guid>`, `ns=5;b=<base64>`, `nsu=<uri>;s=...`.

    `s=` takes the rest of the string verbatim (it may contain `;` and `=`). Raises ValueError.
    """
    s = text.strip()
    if not s:
        raise ValueError("empty NodeId")
    parts = s.split(";")
    ns: int | None = None
    nsu: str | None = None
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        key, eq, value = part.partition("=")
        key = key.strip()
        if not eq:
            raise ValueError(f"NodeId {text!r}: expected key=value parts like 'ns=7;s=NAME', got {part!r}")
        if key in ("ns", "nsu"):
            if ns is not None or nsu is not None:
                what = "twice" if (key == "ns") == (ns is not None) else "both ns= and nsu="
                raise ValueError(f"NodeId {text!r}: namespace given {what}")
            if key == "ns":
                try:
                    ns = int(value.strip())
                except ValueError:
                    raise ValueError(f"NodeId {text!r}: namespace index {value!r} is not a number") from None
                if not 0 <= ns <= _NS_MAX:
                    raise ValueError(f"NodeId {text!r}: namespace index {ns} out of range 0..{_NS_MAX}")
            else:
                nsu = value.strip()
                if not nsu:
                    raise ValueError(f"NodeId {text!r}: empty namespace URI")
            continue
        if key == "s":
            ident: int | str = ";".join([value, *parts[i + 1:]])
            if not ident:
                raise ValueError(f"NodeId {text!r}: empty string identifier")
        elif key in ("i", "g", "b"):
            if any(p.strip() for p in parts[i + 1:]):
                raise ValueError(f"NodeId {text!r}: unexpected text after the identifier")
            ident = _identifier(text, key, value.strip())
        else:
            raise ValueError(f"NodeId {text!r}: unknown part {key!r} (expected ns, nsu, i, s, g or b)")
        return OpcUaAddress(ns or 0, nsu, key, ident)
    raise ValueError(f"NodeId {text!r}: no identifier (i=, s=, g= or b=)")


def _identifier(text: str, kind: str, value: str) -> int | str:
    if not value:
        raise ValueError(f"NodeId {text!r}: empty identifier")
    if kind == "i":
        if not value.isdigit() or int(value) > _UINT32_MAX:
            raise ValueError(f"NodeId {text!r}: numeric identifier must be 0..{_UINT32_MAX}")
        return int(value)
    if kind == "g":
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ValueError(f"NodeId {text!r}: {value!r} is not a GUID") from None
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"NodeId {text!r}: {value!r} is not base64") from None
    return value


# --- params -------------------------------------------------------------------------------------

# opencap name (as written by the importer, D-010) -> asyncua security policy class
SECURITY_POLICIES: dict[str, str | None] = {
    "None": None,
    "Basic256Sha256": "SecurityPolicyBasic256Sha256",
    "Aes128_Sha256_RsaOaep": "SecurityPolicyAes128Sha256RsaOaep",
    "Aes256_Sha256_RsaPss": "SecurityPolicyAes256Sha256RsaPss",
    "Basic256": "SecurityPolicyBasic256",              # deprecated by the OPC Foundation
    "Basic128Rsa15": "SecurityPolicyBasic128Rsa15",    # deprecated by the OPC Foundation
}
MESSAGE_MODES = ("None", "Sign", "SignAndEncrypt")
_POLICY_SPELLINGS = {k.lower().replace("_", ""): k for k in SECURITY_POLICIES}
_MODE_SPELLINGS = {k.lower(): k for k in MESSAGE_MODES}
_SECRET_KEYS = {"username": "username_env", "user": "username_env", "password": "password_env",
                "passwd": "password_env", "private_key_password": "private_key_password_env"}
_ENV_KEYS = ("username_env", "password_env", "private_key_password_env")
_PATH_KEYS = ("certificate", "private_key", "server_certificate")


# OPC UA services that change something on the server (CONTRIBUTING rule 1, ADR-0008). The driver
# never calls them; these stubs make sure nothing else in the process can through this session.
WRITE_SERVICES = ("write", "write_attributes", "call", "history_update", "add_nodes", "delete_nodes",
                  "add_references", "delete_references")


def _guard_read_only(client: Any) -> None:
    """Replace asyncua's write-capable services with raising stubs on both objects that send them:
    the client's `UaClient` and its `UaSession` (asyncua 2.1 `Node` objects call the session
    directly; it is created once per `UaClient`, and every connect builds a new `Client`)."""
    def refuse(name: str) -> Callable[..., Any]:
        async def refused(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError(f"opencap is read-only: OPC UA service {name} refused")
        return refused

    for target in (client.uaclient, client.uaclient.session):
        for name in WRITE_SERVICES:
            setattr(target, name, refuse(name))


@dataclass(frozen=True)
class OpcUaParams:
    """Typed view of `Device.params` for opcua_client (docs/driver_params.md). Unknown keys are
    ignored; a None value means "default". Secrets are never params: only env var NAMES."""

    endpoint: str
    security_policy: str = "None"
    message_mode: str = "None"
    username_env: str | None = None
    password_env: str | None = None
    publishing_interval_ms: int | None = None
    sampling_interval_ms: int | None = None
    certificate: str | None = None            # client application certificate (DER or PEM)
    private_key: str | None = None            # its private key (PEM or DER)
    private_key_password_env: str | None = None
    server_certificate: str | None = None     # pin the server certificate (else taken from GetEndpoints)
    application_uri: str = "urn:opencap:client"
    max_items: int = 100                      # monitored items per request (and per block); nodes per Read
    session_timeout_ms: int = 60_000
    allow_insecure_password: bool = False     # send a user name/password over security policy None

    @classmethod
    def from_device(cls, device: Device) -> OpcUaParams:
        raw = device.params
        for key, env_key in _SECRET_KEYS.items():
            if key in raw:
                raise ValueError(f"{device.id}: opcua_client param {key!r} is not allowed: put the secret "
                                 f"in an environment variable and name it in {env_key}")
        names = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in names and v is not None}
        kwargs.setdefault("endpoint", f"opc.tcp://{device.host}:{device.port}")
        if isinstance(kwargs.get("security_policy"), str):
            spelled = kwargs["security_policy"].lower().replace("_", "")
            kwargs["security_policy"] = _POLICY_SPELLINGS.get(spelled, kwargs["security_policy"])
        if isinstance(kwargs.get("message_mode"), str):
            kwargs["message_mode"] = _MODE_SPELLINGS.get(kwargs["message_mode"].lower(), kwargs["message_mode"])
        try:
            return cls(**kwargs)
        except ValueError as exc:
            raise ValueError(f"{device.id}: {exc}") from None

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("opc.tcp://"):
            raise ValueError(f"opcua_client param endpoint={self.endpoint!r}: must start with opc.tcp://")
        if self.security_policy not in SECURITY_POLICIES:
            raise ValueError(f"opcua_client param security_policy={self.security_policy!r}: expected one of "
                             f"{list(SECURITY_POLICIES)}")
        if self.message_mode not in MESSAGE_MODES:
            raise ValueError(f"opcua_client param message_mode={self.message_mode!r}: expected one of {MESSAGE_MODES}")
        if self.security_policy == "None" and self.message_mode != "None":
            raise ValueError(f"opcua_client message_mode {self.message_mode} needs a security_policy other than None")
        if self.security_policy != "None":
            if self.message_mode == "None":
                raise ValueError(f"opcua_client security_policy {self.security_policy} needs message_mode "
                                 f"Sign or SignAndEncrypt")
            if not self.certificate or not self.private_key:
                raise ValueError(f"opcua_client security_policy {self.security_policy} needs params certificate "
                                 f"and private_key (paths; a self-signed pair is generated there if both are missing)")
        if not isinstance(self.allow_insecure_password, bool):
            raise ValueError("opcua_client param allow_insecure_password: expected true or false")
        for key in (*_ENV_KEYS, *_PATH_KEYS, "application_uri"):
            value = getattr(self, key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"opcua_client param {key}: expected a non-empty string")
        _check_int("max_items", self.max_items, 1)
        _check_int("session_timeout_ms", self.session_timeout_ms, 1000)
        if self.publishing_interval_ms is not None:
            _check_int("publishing_interval_ms", self.publishing_interval_ms, 1)
        if self.sampling_interval_ms is not None:
            _check_int("sampling_interval_ms", self.sampling_interval_ms, 0)

    @property
    def secure(self) -> bool:
        return self.security_policy != "None"


def _check_int(name: str, value: Any, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"opcua_client param {name}={value!r}: expected an integer >= {minimum}")


# --- blocks -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OpcUaItem:
    """One monitored item: a node and every tag (name, dtype) served by it."""

    node: str                                  # OpcUaAddress.key
    sampling_ms: int
    tags: tuple[tuple[str, str], ...]


@dataclass(kw_only=True)
class OpcUaBlock(Block):
    """Up to `max_items` monitored items of one rate group. Reading it costs no request."""

    items: list[OpcUaItem] = field(default_factory=list)
    rate_ms: int = 1000

    def describe(self) -> str:
        return f"UA subscription {len(self.items)} items @ {self.rate_ms} ms"

    def wire_requests(self) -> int:
        return 0                               # served from notifications; nothing is polled


def _batched(seq: Sequence[Any], n: int) -> Iterator[list[Any]]:
    for i in range(0, len(seq), n):
        yield list(seq[i:i + n])


# --- values -------------------------------------------------------------------------------------

_INT_RANGES: dict[str, tuple[int, int]] = {
    "int16": (-2**15, 2**15 - 1), "uint16": (0, 2**16 - 1),
    "int32": (-2**31, 2**31 - 1), "uint32": (0, 2**32 - 1),
    "int64": (-2**63, 2**63 - 1), "uint64": (0, 2**64 - 1),
}


def decode_value(dtype: str, value: Any) -> Any:
    """Coerce a Variant value to the tag's dtype, or return DecodeError. Arrays element-wise.

    Numbers convert between int, float and bool (float -> int rounds; out of range is an
    error); strings accept str, LocalizedText, QualifiedName, bytes and plain scalars.
    """
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            d = decode_value(dtype, v)
            if isinstance(d, DecodeError):
                return d
            out.append(d)
        return out
    if value is None:
        return DecodeError("null value with a Good status")
    if dtype == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("latin-1")
        if isinstance(value, (bool, int, float)):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        for attr in ("Text", "Name"):          # LocalizedText, QualifiedName
            text = getattr(value, attr, None)
            if isinstance(text, str):
                return text
        return DecodeError(f"cannot read {type(value).__name__} as a string")
    if isinstance(value, bool):
        number: int | float = int(value)
    elif isinstance(value, (int, float)):
        number = value
    else:
        return DecodeError(f"cannot read {type(value).__name__} as {dtype}")
    if dtype == "bool":
        if isinstance(number, float) and math.isnan(number):
            return DecodeError("NaN as bool")
        return number != 0
    if dtype in ("float32", "float64"):
        return float(number)
    lo, hi = _INT_RANGES[dtype]
    if isinstance(number, float):
        if not math.isfinite(number):
            return DecodeError(f"{number} as {dtype}")
        number = round(number)
    if not lo <= number <= hi:
        return DecodeError(f"{number} out of range for {dtype}")
    return int(number)


@dataclass(slots=True)
class Sample:
    """Latest DataValue of one node: status code, raw Variant value, timestamp, origin."""

    status: int
    value: Any
    source_ts: datetime | None
    from_read: bool = False                    # from the connect-time Read, not a notification


def sample_to_tag(sample: Sample | None, dtype: str) -> TagValue:
    if sample is None:
        return TagValue(None, "bad_comm", None)
    quality = status_quality(sample.status)
    if quality != "good":
        return TagValue(None, quality, sample.source_ts)
    value = decode_value(dtype, sample.value)
    if isinstance(value, DecodeError):
        return TagValue(None, "bad_decode", sample.source_ts)
    return TagValue(value, "good", sample.source_ts)


def _sample(dv: Any, *, from_read: bool) -> Sample:
    """asyncua DataValue -> Sample."""
    status = dv.StatusCode.value if dv.StatusCode is not None else 0
    value = dv.Value.Value if dv.Value is not None else None
    ts = dv.SourceTimestamp or dv.ServerTimestamp
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return Sample(status, value, ts, from_read)


# --- I/O ----------------------------------------------------------------------------------------

KEEPALIVE_TARGET_MS = 5000      # ask the server for a keep-alive about every 5 s when nothing changes
LIFETIME_FACTOR = 4             # lifetime count = 4 x keep-alive count (the spec wants >= 3)
SILENCE_FACTOR = 1.5            # no publish for 1.5 x keep-alive period (+ 2 x timeout) = session lost

_TASK_ATTRS = ("_supervisor_task", "_stale_watchdog_task", "_renew_channel_task")


@dataclass
class _Wanted:
    """A node to monitor (merged over every rate group of the device)."""

    address: OpcUaAddress
    sampling_ms: int


class _Session:
    """One asyncua Client plus its subscription and the samples it delivered."""

    def __init__(self, client: Any, connected_state: Any, timeout_s: float) -> None:
        self.client = client
        self.connected_state = connected_state
        self.timeout_s = timeout_s
        self.sub: Any = None
        self.handles: dict[int, str] = {}          # client handle -> node key
        self.samples: dict[str, Sample] = {}
        self.subscribed: set[str] = set()
        self.failed: dict[str, int] = {}           # node key -> status the server refused it with
        self.revised_interval_ms: float = 0.0
        self.keepalive_count: int = 0
        self.silence_s: float = math.inf
        self.created = time.monotonic()
        self.lost: str | None = None               # first reason the session is unusable
        self.reported = False                      # the loss was logged
        self.closed = False

    def store(self, key: str, sample: Sample) -> None:
        old = self.samples.get(key)
        if old is not None:
            if sample.from_read and not old.from_read:
                return                                     # a notification already arrived: newer
            if (not sample.from_read and old.from_read and sample.source_ts is not None
                    and old.source_ts is not None and sample.source_ts < old.source_ts):
                return                                     # the initial sample predates our Read
        self.samples[key] = sample

    def dead_reason(self) -> str | None:
        if self.lost is None:
            if self.closed:
                self.lost = "closed"
            elif self.client.state is not self.connected_state:
                self.lost = f"session lost (client {self.client.state.value})"
            elif self.sub is not None:
                last = self.sub.last_publish_at or self.created
                silent = time.monotonic() - last
                if silent > self.silence_s:
                    self.lost = f"no publish response or keep-alive for {silent:.1f} s"
        return self.lost


class _Handler:
    """asyncua subscription handler (callbacks run as tasks on the event loop)."""

    def __init__(self, session: _Session, device_id: str) -> None:
        self.session = session
        self.device_id = device_id

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        s = self.session
        if s.closed:
            return
        key = s.handles.get(data.monitored_item.ClientHandle)
        if key is not None:
            s.store(key, _sample(data.monitored_item.Value, from_read=False))

    def status_change_notification(self, status: Any) -> None:
        code = status.Status.value if status.Status is not None else 0
        if status_quality(code) != "good" and self.session.lost is None and not self.session.closed:
            self.session.lost = f"subscription status {status_name(code)}"

    def event_notification(self, event: Any) -> None:   # pragma: no cover - no event items
        pass


class _QuietLostConnection(logging.Filter):
    """asyncua's publish loop logs a full traceback at ERROR ("Publish iteration crashed") when
    the connection drops under a pending Publish. The loss is reported once by this driver
    (opcua.session_lost), so drop exactly that record."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, ConnectionError) and record.getMessage().startswith("Publish iteration"))


_quiet_filter = _QuietLostConnection()


def _install_log_filter() -> None:
    logger = logging.getLogger("asyncua.client.ua_session")
    if _quiet_filter not in logger.filters:
        logger.addFilter(_quiet_filter)


def _abort_client(client: Any) -> None:
    """Synchronous teardown that leaves no asyncua task behind (supervisor, keep-alive probe,
    channel renewal, publish loop). Private attributes, so every step is guarded."""
    uac = getattr(client, "uaclient", None)
    with contextlib.suppress(Exception):
        uac.request_disconnect()
    tasks = [getattr(client, name, None) for name in _TASK_ATTRS]
    tasks.append(getattr(getattr(uac, "session", None), "_publish_task", None))
    for task in tasks:
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
    with contextlib.suppress(Exception):
        client.disconnect_socket()


async def _disconnect(client: Any, timeout_s: float) -> None:
    """CloseSession (deleting the subscription) + CloseSecureChannel, bounded; never raises.
    A client whose transport is already gone is only torn down (nothing could be sent)."""
    try:
        if getattr(client.state, "value", "disconnected") != "disconnected":
            await asyncio.wait_for(client.disconnect(), timeout_s)
    except asyncio.CancelledError:
        _abort_client(client)
        raise
    except Exception as exc:
        _log.debug("opcua close error", error=_describe(exc))
    finally:
        _abort_client(client)


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class OpcUaClientDriver(Driver):
    """OPC UA client via asyncua subscriptions. See the module docstring and D-045."""

    name: ClassVar[str] = "opcua_client"
    is_async: ClassVar[bool] = True
    stale_by_read_time: ClassVar[bool] = True

    def __init__(self, device: Device, executor: Any = None) -> None:
        super().__init__(device, executor)
        self.params = OpcUaParams.from_device(device)        # ValueError on bad params
        self._session: _Session | None = None
        self._wanted_cache: list[_Wanted] | None = None
        self._warned: set[str] = set()                       # nodes already logged as refused

    # --- pure ---

    def plan(self, tags: list[Tag]) -> Plan:
        p = self.params
        invalid: dict[str, str] = {}
        by_node: dict[str, list[Tag]] = {}
        for tag in tags:
            if not tag.enabled:
                continue
            try:
                addr = parse_node_id(tag.address)
            except ValueError as exc:
                invalid[tag.name] = str(exc)
                continue
            by_node.setdefault(addr.key, []).append(tag)
        if not by_node:
            return Plan(blocks=[], invalid=invalid)
        rate = min(self.device.effective_rate_ms(t) for group in by_node.values() for t in group)
        sampling = p.sampling_interval_ms if p.sampling_interval_ms is not None else rate
        items = [OpcUaItem(node, sampling, tuple((t.name, t.dtype) for t in group))
                 for node, group in by_node.items()]
        blocks: list[Block] = [
            OpcUaBlock(tags=[name for it in chunk for name, _ in it.tags], items=chunk, rate_ms=rate)
            for chunk in _batched(items, p.max_items)
        ]
        return Plan(blocks=blocks, invalid=invalid)

    def _wanted(self) -> list[_Wanted]:
        """Every node of every rate group, once, with its fastest sampling interval."""
        if self._wanted_cache is None:
            merged: dict[str, _Wanted] = {}
            for group in self.device.rate_groups().values():
                for block in self.plan(group).blocks:
                    assert isinstance(block, OpcUaBlock)
                    for item in block.items:
                        w = merged.get(item.node)
                        if w is None:
                            merged[item.node] = _Wanted(parse_node_id(item.node), item.sampling_ms)
                        else:
                            w.sampling_ms = min(w.sampling_ms, item.sampling_ms)
            self._wanted_cache = list(merged.values())
        return self._wanted_cache

    def _publishing_interval_ms(self) -> int:
        if self.params.publishing_interval_ms is not None:
            return self.params.publishing_interval_ms
        rates = list(self.device.rate_groups())
        return min(rates) if rates else self.device.scan_ms

    # --- I/O ---

    @property
    def timeout_s(self) -> float:
        return max(self.device.timeout_ms, 1) / 1000.0

    @property
    def _where(self) -> str:
        return f"{self.device.id} ({self.params.endpoint})"

    def connect_timeout_s(self) -> float | None:
        """Session set-up (TCP, Hello, OpenSecureChannel, CreateSession, ActivateSession; plus a
        GetEndpoints channel when secure), namespace array, CreateSubscription, then one
        CreateMonitoredItems and one Read per `max_items` nodes. Each is bounded by timeout_ms."""
        chunks = math.ceil(len(self._wanted()) / self.params.max_items)
        requests = 7 + (4 if self.params.secure else 0) + 2 * chunks
        return requests * self.timeout_s + 1.0

    @property
    def connected(self) -> bool:
        s = self._session
        if s is None:
            return False
        reason = s.dead_reason()
        if reason is not None:
            self._report_lost(s, reason)
            return False
        return True

    def _report_lost(self, s: _Session, reason: str) -> None:
        if not s.reported:
            s.reported = True
            if reason != "closed":
                _log.info("opcua.session_lost", device=self.device.id, reason=reason)

    async def connect(self) -> None:
        """Open a session, subscribe every node, read them once. Raises ConnectionLost."""
        await self.close()
        from asyncua import Client
        from asyncua.client.ua_client import UaClientState

        _install_log_filter()
        p = self.params
        started = time.monotonic()
        client = Client(p.endpoint, timeout=self.timeout_s, watchdog_intervall=max(1.0, self.timeout_s),
                        auto_reconnect=False)
        _guard_read_only(client)
        client.application_uri = p.application_uri
        client.name = client.description = f"opencap {self.device.id}"
        client.product_uri = "urn:opencap"
        client.session_timeout = p.session_timeout_ms
        session = _Session(client, UaClientState.CONNECTED, self.timeout_s)
        try:
            await self._apply_security(client)
            user, password = self._credentials()
            if user:
                client.set_user(user)
            if password:
                client.set_password(password)
            await client.connect()
            wanted = await self._resolve(client, session)
            await self._subscribe(client, session, wanted)
            await self._initial_read(client, session, wanted)
        except asyncio.CancelledError:
            session.closed = True
            _abort_client(client)
            raise
        except Exception as exc:
            session.closed = True
            await _disconnect(client, self.timeout_s)
            reason = _describe(exc)
            _log.debug("opcua connect", device=self.device.id, result="failed", reason=reason,
                       ms=round((time.monotonic() - started) * 1000, 1))
            if isinstance(exc, ConnectionLost):
                raise
            raise ConnectionLost(f"{self._where}: connect failed: {reason}") from exc
        self._session = session
        refused = {k: v for k, v in session.failed.items() if k not in self._warned}
        if refused:
            self._warned.update(refused)
            _log.warning("opcua.items_refused", device=self.device.id, count=len(refused),
                         nodes={k: status_name(v) for k, v in list(refused.items())[:20]})
        _log.debug("opcua connect", device=self.device.id, result="ok", items=len(session.subscribed),
                   refused=len(session.failed), publishing_interval_ms=session.revised_interval_ms,
                   keepalive_count=session.keepalive_count, ms=round((time.monotonic() - started) * 1000, 1))

    async def close(self) -> None:
        """Close the session (the server deletes the subscription). Idempotent, never raises."""
        s, self._session = self._session, None
        if s is None:
            return
        s.closed = True
        await _disconnect(s.client, self.timeout_s)

    async def read(self, block: Block) -> dict[str, TagValue]:
        """Latest notification (or connect-time Read) of every tag in the block. No request.
        Raises ConnectionLost when the session or subscription is gone."""
        if not isinstance(block, OpcUaBlock):
            raise TypeError(f"opcua_client cannot read {type(block).__name__}")
        s = self._session
        if s is None:
            raise ConnectionLost(f"{self._where}: not connected")
        reason = s.dead_reason()
        if reason is not None:
            self._report_lost(s, reason)
            raise ConnectionLost(f"{self._where}: {reason}")
        out: dict[str, TagValue] = {}
        for item in block.items:
            sample = s.samples.get(item.node)
            for name, dtype in item.tags:
                out[name] = sample_to_tag(sample, dtype)
        return out

    async def identity(self) -> dict[str, Any]:
        """Server BuildInfo and state, endpoint security, namespaces and the subscription as
        revised by the server. {} when not connected."""
        s = self._session
        if s is None or s.dead_reason() is not None:
            return {}
        from asyncua import ua

        ids = ua.ObjectIds
        fields_ = {"product_uri": ids.Server_ServerStatus_BuildInfo_ProductUri,
                   "manufacturer": ids.Server_ServerStatus_BuildInfo_ManufacturerName,
                   "product_name": ids.Server_ServerStatus_BuildInfo_ProductName,
                   "software_version": ids.Server_ServerStatus_BuildInfo_SoftwareVersion,
                   "build_number": ids.Server_ServerStatus_BuildInfo_BuildNumber,
                   "server_state": ids.Server_ServerStatus_State}
        rp = ua.ReadParameters()
        rp.NodesToRead = [ua.ReadValueId(NodeId=ua.NodeId(i), AttributeId=ua.AttributeIds.Value)
                          for i in (*fields_.values(), ids.Server_NamespaceArray)]
        dvs = await asyncio.wait_for(s.client.uaclient.read(rp), self.timeout_s * 2)
        out: dict[str, Any] = {}
        for (name, _), dv in zip(fields_.items(), dvs):
            v = dv.Value.Value if dv.Value is not None else None
            if name == "server_state" and isinstance(v, int):
                with contextlib.suppress(ValueError):
                    v = ua.ServerState(v).name
            out[name] = v
        p = self.params
        out.update({
            "endpoint": p.endpoint, "security_policy": p.security_policy, "message_mode": p.message_mode,
            "namespaces": dvs[-1].Value.Value if dvs[-1].Value is not None else None,
            "publishing_interval_ms": s.revised_interval_ms, "keepalive_count": s.keepalive_count,
            "monitored_items": len(s.subscribed), "refused_items": len(s.failed),
            "refused": {k: status_name(v) for k, v in list(s.failed.items())[:20]},
        })
        return out

    # --- connect steps ---

    def _credentials(self) -> tuple[str | None, str | None]:
        """User name and password from the environment variables named in params. The error
        names the variable, never a value."""
        p = self.params
        if p.password_env and not p.secure and not p.allow_insecure_password:
            # refused before any connection is opened, so the password never leaves this computer
            raise ConnectionLost(f"{self._where}: refusing to send a password over security_policy None (it may "
                                 f"travel unencrypted): use Basic256Sha256 with Sign or SignAndEncrypt, or set "
                                 f"params.allow_insecure_password: true")
        user = password = None
        if p.username_env:
            user = os.environ.get(p.username_env)
            if not user:
                raise ConnectionLost(f"{self._where}: environment variable {p.username_env} "
                                     f"(OPC UA user name) is not set")
        if p.password_env:
            password = os.environ.get(p.password_env)
            if not password:
                raise ConnectionLost(f"{self._where}: environment variable {p.password_env} "
                                     f"(OPC UA password) is not set")
            if not user:
                raise ConnectionLost(f"{self._where}: password_env is set but username_env is not")
        return user, password

    async def _apply_security(self, client: Any) -> None:
        p = self.params
        if not p.secure:
            return
        from asyncua import ua
        from asyncua.crypto import security_policies

        policy = getattr(security_policies, SECURITY_POLICIES[p.security_policy] or "", None)
        if policy is None:
            raise ConnectionLost(f"{self._where}: security policy {p.security_policy} is not available "
                                 f"in this asyncua version")
        cert, key = Path(p.certificate or ""), Path(p.private_key or "")
        if not cert.exists() and not key.exists():
            await client.setup_self_signed_certificate(key, cert)
            with contextlib.suppress(OSError):
                key.chmod(0o600)
            _log.info("opcua.client_certificate_created", device=self.device.id, certificate=str(cert),
                      application_uri=p.application_uri,
                      hint="trust this certificate on the OPC UA server")
        elif not cert.exists() or not key.exists():
            missing = cert if not cert.exists() else key
            raise ConnectionLost(f"{self._where}: {missing} does not exist (certificate and private_key "
                                 f"must both exist, or both be missing to generate a pair)")
        key_password = os.environ.get(p.private_key_password_env) if p.private_key_password_env else None
        if p.private_key_password_env and not key_password:
            raise ConnectionLost(f"{self._where}: environment variable {p.private_key_password_env} "
                                 f"(private key password) is not set")
        await client.set_security(policy, str(cert), str(key), key_password, p.server_certificate,
                                  getattr(ua.MessageSecurityMode, p.message_mode))

    async def _resolve(self, client: Any, session: _Session) -> list[tuple[_Wanted, Any]]:
        """asyncua NodeIds for the wanted nodes; nsu= URIs are looked up in the NamespaceArray.
        A URI the server does not have makes that node BadNodeIdUnknown."""
        from asyncua import ua

        wanted = self._wanted()
        ns_index: dict[str, int] = {}
        if any(w.address.namespace_uri is not None for w in wanted):
            ns_index = {uri: i for i, uri in enumerate(await client.get_namespace_array())}
        out: list[tuple[_Wanted, Any]] = []
        for w in wanted:
            a = w.address
            index = a.namespace if a.namespace_uri is None else ns_index.get(a.namespace_uri)
            if index is None:
                session.failed[a.key] = BAD_NODE_ID_UNKNOWN
                session.samples[a.key] = Sample(BAD_NODE_ID_UNKNOWN, None, None)
                continue
            out.append((w, ua.NodeId.from_string(a.with_index(index))))
        return out

    async def _subscribe(self, client: Any, session: _Session, wanted: list[tuple[_Wanted, Any]]) -> None:
        from asyncua import ua
        from asyncua.common.subscription import Subscription

        interval = float(self._publishing_interval_ms())
        keepalive = max(1, math.ceil(KEEPALIVE_TARGET_MS / interval))
        params = ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=interval, RequestedLifetimeCount=keepalive * LIFETIME_FACTOR,
            RequestedMaxKeepAliveCount=keepalive, MaxNotificationsPerPublish=0, PublishingEnabled=True,
            Priority=0,
        )
        # Built directly (not Client.create_subscription) to get the server's revised values,
        # and kept out of asyncua's own stale-subscription watchdog: recovery is the worker's.
        sub = Subscription(client.uaclient.session, params, _Handler(session, self.device.id))
        result = await sub.init()
        session.sub = sub
        session.revised_interval_ms = float(result.RevisedPublishingInterval)
        session.keepalive_count = int(result.RevisedMaxKeepAliveCount)
        period_s = session.revised_interval_ms / 1000 * max(session.keepalive_count, 1)
        session.silence_s = period_s * SILENCE_FACTOR + 2 * self.timeout_s
        handle = 0
        for chunk in _batched(wanted, self.params.max_items):
            requests = []
            for w, node in chunk:
                handle += 1
                session.handles[handle] = w.address.key
                requests.append(ua.MonitoredItemCreateRequest(
                    ItemToMonitor=ua.ReadValueId(NodeId=node, AttributeId=ua.AttributeIds.Value),
                    MonitoringMode=ua.MonitoringMode.Reporting,
                    RequestedParameters=ua.MonitoringParameters(
                        ClientHandle=handle, SamplingInterval=float(w.sampling_ms), QueueSize=1,
                        DiscardOldest=True),
                ))
            results = await sub.create_monitored_items(requests)
            for (w, _node), res in zip(chunk, results):
                key = w.address.key
                if isinstance(res, ua.StatusCode):
                    session.failed[key] = res.value
                    session.samples[key] = Sample(res.value, None, None)
                else:
                    session.subscribed.add(key)

    async def _initial_read(self, client: Any, session: _Session, wanted: list[tuple[_Wanted, Any]]) -> None:
        from asyncua import ua

        nodes = [(w, n) for w, n in wanted if w.address.key in session.subscribed]
        for chunk in _batched(nodes, self.params.max_items):
            rp = ua.ReadParameters()
            rp.MaxAge = float(self._publishing_interval_ms())
            rp.TimestampsToReturn = ua.TimestampsToReturn.Both
            rp.NodesToRead = [ua.ReadValueId(NodeId=n, AttributeId=ua.AttributeIds.Value) for _w, n in chunk]
            dvs = await client.uaclient.read(rp)
            for (w, _n), dv in zip(chunk, dvs):
                session.store(w.address.key, _sample(dv, from_read=True))

