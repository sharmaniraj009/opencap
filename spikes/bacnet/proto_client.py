"""Throwaway BACnet/IP client prototype for the BACnet spike (#15). NOT production code; never
imported by `src/`. It shows the read subset a native driver needs is small: BVLC (unicast,
broadcast, foreign-device registration, distribute-broadcast), NPDU, confirmed and unconfirmed
APDUs, BACnet tag encoding, Who-Is/I-Am, ReadProperty, ReadPropertyMultiple, segmented ComplexACK
reassembly with SegmentACK, SubscribeCOV and UnconfirmedCOVNotification; plus the KEPServerEX-style
address parser and the status-flags/reliability -> opencap quality mapping of docs/spikes/bacnet.md.
Written from the ASHRAE 135 encodings as summarised there; no other implementation's source used.

Read-only by construction: `CONFIRMED_ALLOWED` is the only set of confirmed services this module can
send (ReadProperty, ReadPropertyMultiple, SubscribeCOV); there is no WriteProperty,
WritePropertyMultiple, DeviceCommunicationControl, ReinitializeDevice or object create/delete code.
"""

from __future__ import annotations

import asyncio
import re
import struct
from dataclasses import dataclass, field
from typing import Any

# --- enumerations (ASHRAE 135 clause 21) -------------------------------------------------------------

OBJECT_TYPES = {
    "AnalogInput": 0, "AnalogOutput": 1, "AnalogValue": 2, "BinaryInput": 3, "BinaryOutput": 4,
    "BinaryValue": 5, "Device": 8, "MultiStateInput": 13, "MultiStateOutput": 14, "MultiStateValue": 19,
    "NetworkPort": 56,
}
OBJECT_NAMES = {v: k for k, v in OBJECT_TYPES.items()}
PROPERTIES = {
    "Description": 28, "EventState": 36, "NumberOfStates": 74, "ObjectIdentifier": 75, "ObjectList": 76,
    "ObjectName": 77, "ObjectType": 79, "OutOfService": 81, "PresentValue": 85, "PriorityArray": 87,
    "Reliability": 103, "StateText": 110, "StatusFlags": 111, "Units": 117, "COVIncrement": 22,
}
PROPERTY_NAMES = {v: k for k, v in PROPERTIES.items()}

# confirmed service choices
SVC_SUBSCRIBE_COV, SVC_READ_PROPERTY, SVC_READ_PROPERTY_MULTIPLE = 5, 12, 14
CONFIRMED_ALLOWED = frozenset({SVC_SUBSCRIBE_COV, SVC_READ_PROPERTY, SVC_READ_PROPERTY_MULTIPLE})
# unconfirmed service choices
USVC_I_AM, USVC_COV_NOTIFICATION, USVC_WHO_IS = 0, 2, 8

# --- BVLC / NPDU ----------------------------------------------------------------------------------------

BVLC_RESULT, BVLC_REGISTER_FD, BVLC_DISTRIBUTE_BROADCAST = 0x00, 0x05, 0x09
BVLC_FORWARDED, BVLC_UNICAST, BVLC_BROADCAST = 0x04, 0x0A, 0x0B


def bvlc(function: int, payload: bytes) -> bytes:
    return bytes((0x81, function)) + (4 + len(payload)).to_bytes(2, "big") + payload


def npdu(apdu: bytes, *, expecting_reply: bool) -> bytes:
    return bytes((0x01, 0x04 if expecting_reply else 0x00)) + apdu


def parse_bvlc(data: bytes) -> tuple[int, bytes]:
    if len(data) < 4 or data[0] != 0x81 or int.from_bytes(data[2:4], "big") != len(data):
        raise ValueError("bad BVLC")
    fn, body = data[1], data[4:]
    if fn == BVLC_FORWARDED:
        body = body[6:]                               # original source B/IP address
    return fn, body


def parse_npdu(data: bytes) -> bytes | None:
    """NPDU -> APDU (None for network-layer messages)."""
    if data[0] != 0x01:
        raise ValueError("bad NPDU version")
    control, pos = data[1], 2
    if control & 0x20:                                # DNET, DLEN, DADR
        pos += 3 + data[pos + 2]
    if control & 0x08:                                # SNET, SLEN, SADR
        pos += 3 + data[pos + 2]
    if control & 0x20:                                # hop count
        pos += 1
    if control & 0x80:
        return None
    return data[pos:]


# --- tags ------------------------------------------------------------------------------------------------

def _tag(number: int, context: bool, data: bytes) -> bytes:
    first = (number << 4 if number <= 14 else 0xF0) | (0x08 if context else 0)
    ext = b"" if number <= 14 else bytes((number,))
    n = len(data)
    if n <= 4:
        return bytes((first | n,)) + ext + data
    if n <= 253:
        return bytes((first | 5,)) + ext + bytes((n,)) + data
    return bytes((first | 5,)) + ext + b"\xfe" + n.to_bytes(2, "big") + data


def _unsigned(v: int) -> bytes:
    return v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")


def ctx_unsigned(tag: int, v: int) -> bytes:
    return _tag(tag, True, _unsigned(v))


def ctx_object_id(tag: int, obj_type: int, instance: int) -> bytes:
    return _tag(tag, True, ((obj_type << 22) | instance).to_bytes(4, "big"))


def ctx_bool(tag: int, v: bool) -> bytes:
    return _tag(tag, True, b"\x01" if v else b"\x00")


def opening(tag: int) -> bytes:
    return bytes(((tag << 4) | 0x0E,))


def closing(tag: int) -> bytes:
    return bytes(((tag << 4) | 0x0F,))


@dataclass(frozen=True)
class Tag:
    number: int
    context: bool
    kind: str            # "data" | "open" | "close"
    data: bytes = b""
    lvt: int = 0


def read_tag(buf: bytes, pos: int) -> tuple[Tag, int]:
    b = buf[pos]
    pos += 1
    number, context, lvt = b >> 4, bool(b & 0x08), b & 0x07
    if number == 0x0F:
        number, pos = buf[pos], pos + 1
    if context and lvt == 6:
        return Tag(number, True, "open"), pos
    if context and lvt == 7:
        return Tag(number, True, "close"), pos
    if not context and number == 1:                   # application boolean: value in LVT
        return Tag(1, False, "data", b"", lvt), pos
    length = lvt
    if lvt == 5:
        length, pos = buf[pos], pos + 1
        if length == 254:
            length, pos = int.from_bytes(buf[pos:pos + 2], "big"), pos + 2
        elif length == 255:
            length, pos = int.from_bytes(buf[pos:pos + 4], "big"), pos + 4
    return Tag(number, context, "data", buf[pos:pos + length], lvt), pos + length


@dataclass(frozen=True)
class BitString:
    bits: tuple[bool, ...]


def decode_application(tag: Tag) -> Any:
    d = tag.data
    match tag.number:
        case 0:
            return None
        case 1:
            return bool(tag.lvt)
        case 2 | 9:                                   # unsigned, enumerated
            return int.from_bytes(d, "big")
        case 3:
            return int.from_bytes(d, "big", signed=True)
        case 4:
            return struct.unpack(">f", d)[0]
        case 5:
            return struct.unpack(">d", d)[0]
        case 6:
            return bytes(d)
        case 7:
            if d[:1] != b"\x00":
                raise ValueError("only UTF-8 character strings in the prototype")
            return d[1:].decode("utf-8")
        case 8:
            unused, raw = d[0], d[1:]
            bits = [bool(byte >> (7 - i) & 1) for byte in raw for i in range(8)]
            return BitString(tuple(bits[:len(bits) - unused] if unused else bits))
        case 12:
            v = int.from_bytes(d, "big")
            return (v >> 22, v & 0x3FFFFF)
        case 10 | 11:
            return tuple(d)
    raise ValueError(f"application tag {tag.number}")


def read_values(buf: bytes, pos: int, close: int) -> tuple[list[Any], int]:
    """Application-tagged values up to the closing tag `close` (nested constructed values flattened)."""
    out: list[Any] = []
    depth = 0
    while True:
        tag, nxt = read_tag(buf, pos)
        if tag.kind == "close" and depth == 0 and tag.number == close:
            return out, nxt
        if tag.kind == "open":
            depth += 1
        elif tag.kind == "close":
            depth -= 1
        elif not tag.context:
            out.append(decode_application(tag))
        pos = nxt


# --- APDUs ---------------------------------------------------------------------------------------------

MAX_APDU_CODE = {50: 0, 128: 1, 206: 2, 480: 3, 1024: 4, 1476: 5}


def confirmed_request(invoke_id: int, service: int, data: bytes, *, max_apdu: int = 1476) -> bytes:
    if service not in CONFIRMED_ALLOWED:
        raise ValueError(f"confirmed service {service} is not part of the read-only subset")
    # PDU type 0, SA (segmented response accepted); max segments: unspecified (0); max APDU code
    return bytes((0x02, MAX_APDU_CODE[max_apdu], invoke_id, service)) + data


def who_is(low: int | None = None, high: int | None = None) -> bytes:
    data = ctx_unsigned(0, low) + ctx_unsigned(1, high) if low is not None and high is not None else b""
    return bytes((0x10, USVC_WHO_IS)) + data


def read_property_request(obj_type: int, instance: int, prop: int, index: int | None = None) -> bytes:
    out = ctx_object_id(0, obj_type, instance) + ctx_unsigned(1, prop)
    return out + (ctx_unsigned(2, index) if index is not None else b"")


def rpm_request(specs: list[tuple[int, int, list[int]]]) -> bytes:
    out = b""
    for obj_type, instance, props in specs:
        out += ctx_object_id(0, obj_type, instance) + opening(1)
        out += b"".join(ctx_unsigned(0, p) for p in props) + closing(1)
    return out


def subscribe_cov_request(process_id: int, obj_type: int, instance: int, lifetime: int) -> bytes:
    return (ctx_unsigned(0, process_id) + ctx_object_id(1, obj_type, instance)
            + ctx_bool(2, False) + ctx_unsigned(3, lifetime))


class BACnetError(Exception):
    def __init__(self, kind: str, detail: Any) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


@dataclass
class IAm:
    device: tuple[int, int]
    max_apdu: int
    segmentation: int
    vendor: int
    source: tuple[str, int]


@dataclass
class CovNotification:
    process_id: int
    device: tuple[int, int]
    obj: tuple[int, int]
    values: dict[int, list[Any]]


def parse_i_am(data: bytes, source: tuple[str, int]) -> IAm:
    vals, pos = [], 0
    while pos < len(data):
        tag, pos = read_tag(data, pos)
        vals.append(decode_application(tag))
    return IAm(vals[0], vals[1], vals[2], vals[3], source)


def parse_read_property_ack(data: bytes) -> list[Any]:
    pos = 0
    while True:
        tag, pos = read_tag(data, pos)
        if tag.kind == "open" and tag.number == 3:
            values, _ = read_values(data, pos, 3)
            return values


def parse_rpm_ack(data: bytes) -> dict[tuple[int, int], dict[int, Any]]:
    """-> {(type, instance): {property: [values] | BACnetError}}"""
    out: dict[tuple[int, int], dict[int, Any]] = {}
    pos = 0
    obj = None
    prop = None
    while pos < len(data):
        tag, pos = read_tag(data, pos)
        if tag.context and tag.kind == "data" and tag.number == 0:
            v = int.from_bytes(tag.data, "big")
            obj = (v >> 22, v & 0x3FFFFF)
            out[obj] = {}
        elif tag.context and tag.kind == "data" and tag.number == 2:
            prop = int.from_bytes(tag.data, "big")
        elif tag.kind == "open" and tag.number == 4:
            values, pos = read_values(data, pos, 4)
            out[obj][prop] = values
        elif tag.kind == "open" and tag.number == 5:
            values, pos = read_values(data, pos, 5)
            out[obj][prop] = BACnetError("property", tuple(values))
    return out


def parse_cov_notification(data: bytes) -> CovNotification:
    pos = 0
    fields: dict[int, Any] = {}
    values: dict[int, list[Any]] = {}
    while pos < len(data):
        tag, pos = read_tag(data, pos)
        if tag.kind == "open" and tag.number == 4:
            prop = None
            while True:
                t, pos = read_tag(data, pos)
                if t.kind == "close" and t.number == 4:
                    break
                if t.kind == "data" and t.number == 0:
                    prop = int.from_bytes(t.data, "big")
                elif t.kind == "open" and t.number == 2:
                    values[prop], pos = read_values(data, pos, 2)
        elif tag.kind == "data":
            fields[tag.number] = int.from_bytes(tag.data, "big")
    dev, obj = fields[1], fields[2]
    return CovNotification(fields[0], (dev >> 22, dev & 0x3FFFFF), (obj >> 22, obj & 0x3FFFFF), values)


# --- quality (docs/spikes/bacnet.md section 5) --------------------------------------------------------------

RELIABILITY_COMM_FAILURE, RELIABILITY_CONFIG_ERROR = 12, 10


def quality_of(status_flags: BitString | None, reliability: int | None = None) -> str:
    """Status_Flags (IN_ALARM, FAULT, OVERRIDDEN, OUT_OF_SERVICE) + Reliability -> opencap quality."""
    flags = status_flags.bits if status_flags else (False, False, False, False)
    in_alarm, fault, overridden, out_of_service = (list(flags) + [False] * 4)[:4]
    if reliability == RELIABILITY_COMM_FAILURE:
        return "bad_comm"
    if reliability == RELIABILITY_CONFIG_ERROR:
        return "bad_config"
    if fault or (reliability not in (None, 0)):
        return "bad_decode"            # no-sensor, over-range, open-loop, ...: not a trustworthy value
    if out_of_service:
        return "stale"                 # the present value is not connected to the field
    return "good"                      # in alarm and overridden values are still the current value


# --- KEPServerEX-style addresses (docs/spikes/bacnet.md section 4) ------------------------------------------

@dataclass(frozen=True)
class Address:
    obj_type: int
    instance: int
    prop: int
    index: int | None = None
    cov: bool = False


_ADDR = re.compile(r"(?P<type>[A-Za-z]+|\d+)\.(?P<inst>\d+)\.(?P<prop>[A-Za-z]+|\d+)"
                   r"(?:\s*\[(?P<idx>\d+)\])?(?:\s+(?P<mod>[A-Za-z]+))?$")


def parse_address(text: str) -> Address:
    m = _ADDR.match(text.strip())
    if not m:
        raise ValueError(f"bad BACnet address {text!r}")
    t, p = m["type"], m["prop"]
    obj_type = int(t) if t.isdigit() else OBJECT_TYPES.get(t)
    prop = int(p) if p.isdigit() else PROPERTIES.get(p)
    if obj_type is None or prop is None:
        raise ValueError(f"unknown object type or property in {text!r}")
    instance = int(m["inst"])
    if instance > 4194303:
        raise ValueError(f"instance out of range in {text!r}")
    mod = (m["mod"] or "").upper()
    if mod in ("PRIORITY", "RELINQUISH"):
        raise ValueError(f"{text!r}: write modifiers are not supported (read-only, ADR-0008)")
    if mod not in ("", "COV"):
        raise ValueError(f"unknown modifier in {text!r}")
    index = int(m["idx"]) if m["idx"] is not None else None
    return Address(obj_type, instance, prop, index, mod == "COV")


def kep_address(obj_type: int, instance: int, prop: str = "PresentValue") -> str:
    return f"{OBJECT_NAMES.get(obj_type, obj_type)}.{instance}.{prop}"


# --- the client --------------------------------------------------------------------------------------------

class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, client: Client) -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client._on_datagram(data, addr)


@dataclass
class _Pending:
    future: asyncio.Future
    segments: dict[int, bytes] = field(default_factory=dict)
    service: int = 0


class Client:
    """Minimal BACnet/IP client. One UDP socket; confirmed requests matched by (peer, invoke id);
    segmented ComplexACKs reassembled (window 1, one SegmentACK per segment)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, timeout: float = 3.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.transport: asyncio.DatagramTransport | None = None
        self._invoke = 0
        self._pending: dict[tuple[tuple[str, int], int], _Pending] = {}
        self.i_ams: list[IAm] = []
        self.cov: asyncio.Queue[CovNotification] = asyncio.Queue()
        self.bvlc_results: asyncio.Queue[int] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.segment_acks = 0

    async def __aenter__(self) -> Client:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self),
                                                                local_addr=(self.host, self.port))
        self.port = self.transport.get_extra_info("sockname")[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.transport is not None:
            self.transport.close()

    def _send(self, frame: bytes, addr: tuple[str, int]) -> None:
        assert self.transport is not None
        self.sent.append(frame)
        self.transport.sendto(frame, addr)

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            fn, body = parse_bvlc(data)
        except ValueError:
            return
        if fn == BVLC_RESULT:
            self.bvlc_results.put_nowait(int.from_bytes(body[:2], "big"))
            return
        apdu = parse_npdu(body)
        if not apdu:
            return
        pdu_type = apdu[0] >> 4
        if pdu_type == 1:                                         # unconfirmed request
            svc, data_ = apdu[1], apdu[2:]
            if svc == USVC_I_AM:
                self.i_ams.append(parse_i_am(data_, addr))
            elif svc == USVC_COV_NOTIFICATION:
                self.cov.put_nowait(parse_cov_notification(data_))
            return
        invoke = apdu[1]
        pending = self._pending.get((addr, invoke))
        if pending is None or pending.future.done():
            return
        if pdu_type == 2:                                         # SimpleACK
            pending.future.set_result(b"")
        elif pdu_type == 3:                                       # ComplexACK
            if apdu[0] & 0x08:                                    # segmented
                seq, more = apdu[2], bool(apdu[0] & 0x04)
                pending.segments[seq] = apdu[5:]
                self._send(bvlc(BVLC_UNICAST, npdu(bytes((0x40, invoke, seq, 1)), expecting_reply=False)), addr)
                self.segment_acks += 1
                if not more:
                    pending.future.set_result(b"".join(pending.segments[i] for i in sorted(pending.segments)))
            else:
                pending.future.set_result(apdu[3:])
        elif pdu_type == 5:                                       # Error
            klass, pos = read_tag(apdu, 3)
            code, _ = read_tag(apdu, pos)
            pending.future.set_exception(BACnetError("error", (decode_application(klass), decode_application(code))))
        elif pdu_type in (6, 7):                                  # Reject, Abort
            pending.future.set_exception(BACnetError("reject" if pdu_type == 6 else "abort", apdu[2]))

    async def request(self, addr: tuple[str, int], service: int, data: bytes, *, max_apdu: int = 1476) -> bytes:
        self._invoke = (self._invoke + 1) & 0xFF
        invoke = self._invoke
        pending = _Pending(asyncio.get_running_loop().create_future(), service=service)
        self._pending[(addr, invoke)] = pending
        try:
            self._send(bvlc(BVLC_UNICAST, npdu(confirmed_request(invoke, service, data, max_apdu=max_apdu),
                                               expecting_reply=True)), addr)
            return await asyncio.wait_for(pending.future, self.timeout)
        finally:
            self._pending.pop((addr, invoke), None)

    # --- services ---------------------------------------------------------------------------------------

    async def who_is(self, addr: tuple[str, int], low: int | None = None, high: int | None = None,
                     *, wait: float = 1.0, via_bbmd: bool = False) -> list[IAm]:
        before = len(self.i_ams)
        fn = BVLC_DISTRIBUTE_BROADCAST if via_bbmd else BVLC_UNICAST
        self._send(bvlc(fn, npdu(who_is(low, high), expecting_reply=False)), addr)
        deadline = asyncio.get_running_loop().time() + wait
        while len(self.i_ams) == before and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        return self.i_ams[before:]

    async def read_property(self, addr, obj_type: int, instance: int, prop: int, index: int | None = None,
                            *, max_apdu: int = 1476) -> list[Any]:
        ack = await self.request(addr, SVC_READ_PROPERTY, read_property_request(obj_type, instance, prop, index),
                                 max_apdu=max_apdu)
        return parse_read_property_ack(ack)

    async def read_property_multiple(self, addr, specs, *, max_apdu: int = 1476):
        return parse_rpm_ack(await self.request(addr, SVC_READ_PROPERTY_MULTIPLE, rpm_request(specs),
                                                max_apdu=max_apdu))

    async def subscribe_cov(self, addr, process_id: int, obj_type: int, instance: int, lifetime: int = 300) -> None:
        await self.request(addr, SVC_SUBSCRIBE_COV, subscribe_cov_request(process_id, obj_type, instance, lifetime))

    async def register_foreign_device(self, bbmd: tuple[str, int], ttl: int = 60) -> int:
        self._send(bvlc(BVLC_REGISTER_FD, ttl.to_bytes(2, "big")), bbmd)
        return await asyncio.wait_for(self.bvlc_results.get(), self.timeout)

    async def browse(self, addr, device_instance: int) -> list[tuple[str, str]]:
        """Device object list -> [(KEPServerEX-style address, object name)] for PresentValue tags."""
        objects = await self.read_property(addr, OBJECT_TYPES["Device"], device_instance, PROPERTIES["ObjectList"])
        wanted = [o for o in objects if o[0] in OBJECT_NAMES and o[0] not in (8, 56)]
        names = await self.read_property_multiple(addr, [(t, i, [PROPERTIES["ObjectName"]]) for t, i in wanted])
        return [(kep_address(t, i), names[(t, i)][PROPERTIES["ObjectName"]][0]) for t, i in wanted]
