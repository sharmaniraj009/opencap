"""Sparkplug B reference for the conformance vectors in tests/conformance/sparkplug/ (#51).

An independent second implementation of docs/spec/payload-sparkplug.md, on Google's protobuf
runtime (the `protobuf` package) with the message types loaded from the descriptor set of the
vendored proto (proto/third_party/sparkplug_b.proto, compiled by protox in the Rust build and
kept identical by engine/north/sparkplug/tests/vectors.rs). The Rust codec
(engine/north/sparkplug) must produce the same bytes for every case (ADR-0014 §2).

    python -m tests.helpers.sparkplug_ref          # rewrite the vectors
    python -m tests.helpers.sparkplug_ref --check  # exit 1 if they are stale

Written from the spec page, not from the Rust code: where the two disagree, the vectors say so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "tests" / "conformance" / "sparkplug"
PROTO = ROOT / "proto" / "third_party" / "sparkplug_b.proto"
PACKAGE = "org.eclipse.tahu.protobuf"
NAMESPACE = "spBv1.0"
BDSEQ, REBIRTH = "bdSeq", "Node Control/Rebirth"
READ_ONLY, NOT_TRUE = "read_only", "not_true"      # rejection reasons (Rust: command::READ_ONLY / NOT_TRUE)

# §3.2: opencap dtype -> Sparkplug datatype name; scaled tags are Double
SCALAR = {"bool": "Boolean", "int16": "Int16", "uint16": "UInt16", "int32": "Int32", "uint32": "UInt32",
          "int64": "Int64", "uint64": "UInt64", "float32": "Float", "float64": "Double", "string": "String"}
# §3.1: integer datatypes -> (field, (lo, hi), stored modulo 2**bits when negative)
INTS = {
    "Int8": ("int_value", (-2**7, 2**7 - 1), 8), "Int16": ("int_value", (-2**15, 2**15 - 1), 16),
    "Int32": ("int_value", (-2**31, 2**31 - 1), 32), "Int64": ("long_value", (-2**63, 2**63 - 1), 64),
    "UInt8": ("int_value", (0, 2**8 - 1), None), "UInt16": ("int_value", (0, 2**16 - 1), None),
    "UInt32": ("int_value", (0, 2**32 - 1), None), "UInt64": ("long_value", (0, 2**64 - 1), None),
    "DateTime": ("long_value", (0, 2**64 - 1), None),
}
PACK = {"Int8": "b", "Int16": "h", "Int32": "i", "Int64": "q", "UInt8": "B", "UInt16": "H", "UInt32": "I",
        "UInt64": "Q", "Float": "f", "Double": "d", "DateTime": "Q"}
# §3.2 / ADR-0006: the Python engine's one quality table (tests/conformance/quality/map.json)
from opencap.core.quality import to_sparkplug  # noqa: E402
QOS = {"NDEATH": 1, "NBIRTH": 0, "NDATA": 0, "DBIRTH": 0, "DDATA": 0, "DDEATH": 0}
NODE_TYPES = ("NBIRTH", "NDEATH", "NDATA", "NCMD")
DEVICE_TYPES = ("DBIRTH", "DDEATH", "DDATA", "DCMD")


# --- protobuf -----------------------------------------------------------------------------------

@cache
def _pool():
    from google.protobuf import descriptor_pb2, descriptor_pool

    fds = descriptor_pb2.FileDescriptorSet.FromString((DIR / "sparkplug_b.desc").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for f in fds.file:
        pool.Add(f)
    return pool


def payload_class():
    from google.protobuf import message_factory

    return message_factory.GetMessageClass(_pool().FindMessageTypeByName(f"{PACKAGE}.Payload"))


def metric_class():
    from google.protobuf import message_factory

    return message_factory.GetMessageClass(_pool().FindMessageTypeByName(f"{PACKAGE}.Payload.Metric"))


def dt(name: str) -> int:
    return _pool().FindEnumTypeByName(f"{PACKAGE}.DataType").values_by_name[name].number


def dt_name(number: int) -> str:
    return _pool().FindEnumTypeByName(f"{PACKAGE}.DataType").values_by_number[number].name


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- values (§3) --------------------------------------------------------------------------------

def datatype_for(dtype: str, scaled: bool, array: bool) -> str:
    if dtype not in SCALAR:
        raise ValueError(f"unknown dtype {dtype!r}")
    if scaled and dtype in ("bool", "string"):
        raise ValueError(f"a {dtype} tag cannot be scaled")
    name = "Double" if scaled else SCALAR[dtype]
    return name + "Array" if array else name


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def encode_value(datatype: str, value: Any) -> tuple[str, Any]:
    """(protobuf field, raw field value) of a metric value."""
    if datatype in INTS:
        field, (lo, hi), bits = INTS[datatype]
        if not _is_int(value):
            raise ValueError(f"{datatype} needs an integer, got {value!r}")
        if not lo <= value <= hi:
            raise ValueError(f"{value} is out of range for {datatype}")
        return field, value % 2**bits if bits else value
    if datatype in ("Float", "Double"):
        if not (_is_int(value) or isinstance(value, float)):
            raise ValueError(f"{datatype} needs a number, got {value!r}")
        return ("float_value" if datatype == "Float" else "double_value"), float(value)
    if datatype == "Boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Boolean needs a bool, got {value!r}")
        return "boolean_value", value
    if datatype in ("String", "Text", "UUID"):
        if not isinstance(value, str):
            raise ValueError(f"{datatype} needs a string, got {value!r}")
        return "string_value", value
    if datatype.endswith("Array"):
        return "bytes_value", _pack_array(datatype[:-len("Array")], value)
    raise ValueError(f"opencap does not publish {datatype} metrics")


def _pack_array(element: str, values: Any) -> bytes:
    if not isinstance(values, list):
        raise ValueError(f"{element}Array needs a list, got {values!r}")
    if element == "Boolean":
        if not all(isinstance(v, bool) for v in values):
            raise ValueError("BooleanArray needs bools")
        bits = bytearray((len(values) + 7) // 8)
        for i, v in enumerate(values):
            if v:
                bits[i // 8] |= 0x80 >> (i % 8)             # spec example: first value = MSB
        return struct.pack("<I", len(values)) + bytes(bits)
    if element == "String":
        out = b""
        for v in values:
            _, s = encode_value("String", v)
            if "\0" in s:
                raise ValueError("a StringArray element cannot contain NUL")
            out += s.encode() + b"\0"
        return out
    out = b""
    for v in values:
        encode_value(element, v)                            # type and range check
        out += struct.pack("<" + PACK[element], float(v) if element in ("Float", "Double") else v)
    return out


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


# --- metrics and payloads (§4, §5) --------------------------------------------------------------

def metric_name(tag_name: str) -> str:
    return tag_name.replace(".", "/")


def _properties(metric, quality: str | None, ever_good: bool = True, units: str | None = None,
                eng_range=None) -> None:
    entries = []
    if quality is not None:
        entries.append(("Quality", "Int32", "int_value", to_sparkplug(quality, ever_good)["Quality"]))
    if units is not None:
        entries.append(("engUnit", "String", "string_value", units))
    if eng_range is not None:
        entries += [("engLow", "Double", "double_value", float(eng_range[0])),
                    ("engHigh", "Double", "double_value", float(eng_range[1]))]
    for key, typ, field, raw in entries:
        metric.properties.keys.append(key)
        pv = metric.properties.values.add()
        pv.type = dt(typ)
        setattr(pv, field, raw)


def _value(metric, datatype: str, sample: dict) -> None:
    if sample["value"] is None or to_sparkplug(sample["quality"], True)["is_null"]:
        metric.is_null = True
        return
    field, raw = encode_value(datatype, sample["value"])
    setattr(metric, field, raw)


def birth_metric(tag: dict, alias: int, sample: dict):
    datatype = datatype_for(tag["dtype"], tag.get("scaled", False), tag.get("array", False))
    m = metric_class()(name=metric_name(tag["name"]), alias=alias, timestamp=sample["ts_ms"], datatype=dt(datatype))
    _properties(m, sample["quality"], sample["value"] is not None, tag.get("units"), tag.get("eng_range"))
    _value(m, datatype, sample)
    return m


def data_metric(alias: int, datatype: str, sample: dict, with_quality: bool):
    m = metric_class()(alias=alias, timestamp=sample["ts_ms"])
    _properties(m, sample["quality"] if with_quality else None, sample["value"] is not None)
    _value(m, datatype, sample)
    return m


def _payload(ts_ms: int, seq: int | None, metrics: list):
    p = payload_class()(timestamp=ts_ms)
    p.metrics.extend(metrics)
    if seq is not None:
        p.seq = seq
    return p


def nbirth(ts_ms: int, bd_seq: int, aliases: dict):
    metrics = [metric_class()(name=BDSEQ, alias=aliases["bd_seq"], timestamp=ts_ms, datatype=dt("Int64"), long_value=bd_seq),
               metric_class()(name=REBIRTH, alias=aliases["rebirth"], timestamp=ts_ms, datatype=dt("Boolean"),
                              boolean_value=False)]
    return _payload(ts_ms, 0, metrics)


def ndeath(ts_ms: int, bd_seq: int):
    return _payload(ts_ms, None, [metric_class()(name=BDSEQ, timestamp=ts_ms, datatype=dt("Int64"), long_value=bd_seq)])


def topic(group: str, kind: str, edge: str, device: str | None = None) -> str:
    for what, ident in (("group id", group), ("edge node id", edge), ("device id", device)):
        if ident is not None:
            validate_id(what, ident)
    if (kind in DEVICE_TYPES) != (device is not None):
        raise ValueError(f"{kind} {'needs' if kind in DEVICE_TYPES else 'takes no'} device id")
    return "/".join([NAMESPACE, group, kind, edge] + ([device] if device is not None else []))


def validate_id(what: str, ident: str) -> None:
    if not ident:
        raise ValueError(f"{what} must not be empty")
    bad = [c for c in ident if c in "+/#\0"]
    if bad:
        raise ValueError(f"{what} {ident!r} contains the reserved character {bad[0]!r}")


def parse_topic(t: str) -> dict:
    parts = t.split("/")
    if parts[0] != NAMESPACE:
        raise ValueError("namespace")
    if len(parts) == 3 and parts[1] == "STATE":
        validate_id("host id", parts[2])
        return {"state": parts[2]}
    if len(parts) < 4 or parts[2] not in NODE_TYPES + DEVICE_TYPES:
        raise ValueError("message type or levels")
    group, kind, edge, *rest = parts[1:]
    if len(rest) != (1 if kind in DEVICE_TYPES else 0):
        raise ValueError("number of levels")
    topic(group, kind, edge, rest[0] if rest else None)      # validates the ids
    out = {"group": group, "kind": kind, "edge_node": edge}
    if rest:
        out["device"] = rest[0]
    return out


def classify(t: str, payload: bytes, rebirth_alias: int | None) -> dict:
    kind = parse_topic(t).get("kind")
    if kind not in ("NCMD", "DCMD"):
        raise ValueError(f"{t} is not a command topic")
    p = payload_class().FromString(payload)
    rebirth, rejected = False, []
    for m in p.metrics:
        named = m.HasField("name")
        is_rebirth = (named and m.name == REBIRTH) or (not named and m.HasField("alias") and m.alias == rebirth_alias)
        label = m.name if named else (f"alias {m.alias}" if m.HasField("alias") else "(unnamed)")
        if kind == "NCMD" and is_rebirth:
            if m.WhichOneof("value") == "boolean_value" and m.boolean_value:
                rebirth = True
            else:
                rejected.append([label, NOT_TRUE])
        else:
            rejected.append([label, READ_ONLY])
    return {"rebirth": rebirth, "rejected": rejected}


def parse_state(payload: str) -> dict:
    v = json.loads(payload)
    if not isinstance(v, dict) or not isinstance(v.get("online"), bool):
        raise ValueError("online")
    ts = v.get("timestamp")
    if not _is_int(ts) or ts < 0:
        raise ValueError("timestamp")
    return {"online": v["online"], "timestamp": ts}


class Session:
    """seq and bdSeq of one edge node (§5.1)."""

    def __init__(self, bd_seq: int = 0) -> None:
        self._bd_seq, self._seq = bd_seq, 0

    def connect(self) -> int:
        v, self._bd_seq = self._bd_seq, (self._bd_seq + 1) % 256
        return v

    def birth(self) -> int:
        self._seq = 1
        return 0

    def next_seq(self) -> int:
        v, self._seq = self._seq, (self._seq + 1) % 256
        return v


# --- the vectors --------------------------------------------------------------------------------

TS = 1_700_000_000_123


def _decoded(m) -> dict:
    """A readable form of a metric, for people and for the Python check."""
    out: dict[str, Any] = {}
    for f in ("name", "alias", "timestamp", "datatype", "is_null"):
        if m.HasField(f):
            out[f] = getattr(m, f)
    if "datatype" in out:
        out["datatype"] = dt_name(out["datatype"])
    field = m.WhichOneof("value")
    if field:
        raw = getattr(m, field)
        out[field] = raw.hex() if isinstance(raw, bytes) else raw
    if m.HasField("properties"):
        props = []
        for k, pv in zip(m.properties.keys, m.properties.values):
            f = pv.WhichOneof("value")
            props.append([k, dt_name(pv.type), getattr(pv, f)])
        out["properties"] = props
    return out


def _tag(name, dtype, *, scaled=False, array=False, units=None, eng_range=None) -> dict:
    t = {"name": name, "dtype": dtype, "scaled": scaled, "array": array}
    if units is not None:
        t["units"] = units
    if eng_range is not None:
        t["eng_range"] = list(eng_range)
    return t


def _sample(value, quality="good", ts_ms=TS) -> dict:
    return {"value": value, "quality": quality, "ts_ms": ts_ms}


METRIC_CASES: list[tuple[str, str, dict, dict, bool]] = [
    # (name, kind, tag, sample, with_quality) -- kind "birth" or "data"
    ("bool true", "birth", _tag("RUN", "bool"), _sample(True), True),
    ("int16 negative is stored modulo 2^16", "birth", _tag("LEVEL.Pv", "int16"), _sample(-2), True),
    ("int16 max", "birth", _tag("LEVEL", "int16"), _sample(32767), True),
    ("int16 min", "birth", _tag("LEVEL", "int16"), _sample(-32768), True),
    ("uint16 max", "birth", _tag("COUNT", "uint16"), _sample(65535), True),
    ("int32 -1", "birth", _tag("I32", "int32"), _sample(-1), True),
    ("int32 min", "birth", _tag("I32", "int32"), _sample(-2**31), True),
    ("uint32 in int_value", "birth", _tag("U32", "uint32"), _sample(4_000_000_000), True),
    ("int64 -1", "birth", _tag("I64", "int64"), _sample(-1), True),
    ("int64 min", "birth", _tag("I64", "int64"), _sample(-2**63), True),
    ("uint64 max", "birth", _tag("U64", "uint64"), _sample(2**64 - 1), True),
    ("float32 rounds to single precision", "birth", _tag("FLOW", "float32"), _sample(1.1), True),
    ("float32 from an integer value", "birth", _tag("FLOW", "float32"), _sample(3), True),
    ("float64", "birth", _tag("T1", "float64"), _sample(-0.5), True),
    ("string, UTF-8", "birth", _tag("MODE", "string"), _sample("Pumpe Süd ✓"), True),
    ("empty string", "birth", _tag("MODE", "string"), _sample(""), True),
    ("scaled int16 is Double, with units and range", "birth",
     _tag("FLOW_SCALED", "int16", scaled=True, units="m³/h", eng_range=(0, 100)), _sample(12.75), True),
    ("int16 array (spec example)", "birth", _tag("ARR16", "int16", array=True), _sample([-30000, 30000]), True),
    ("uint16 array (spec example)", "birth", _tag("UARR16", "uint16", array=True), _sample([30, 52360]), True),
    ("int32 array (spec example)", "birth", _tag("ARR32", "int32", array=True), _sample([-1, 315338746]), True),
    ("uint32 array (spec example)", "birth", _tag("UARR32", "uint32", array=True), _sample([52, 3293969225]), True),
    ("int64 array", "birth", _tag("ARR64", "int64", array=True), _sample([-4270929666821191986, 7]), True),
    ("uint64 array (spec example)", "birth", _tag("UARR64", "uint64", array=True),
     _sample([52, 16444743074749521625]), True),
    ("float32 array", "birth", _tag("FARR", "float32", array=True), _sample([1.5, -0.25]), True),
    ("float64 array", "birth", _tag("DARR", "float64", array=True), _sample([12.354213, 1022.9123213]), True),
    ("bool array (spec example, padding bits zero)", "birth", _tag("BITS", "bool", array=True),
     _sample([False, False, True, True, False, True, False, False, True, True, False, True]), True),
    ("empty bool array", "birth", _tag("BITS", "bool", array=True), _sample([]), True),
    ("string array (spec example)", "birth", _tag("NAMES", "string", array=True), _sample(["ABC", "hello"]), True),
    ("scaled array is DoubleArray", "birth", _tag("SARR", "uint16", scaled=True, array=True), _sample([0.5, 2.0]), True),
    ("bad_comm keeps the last value", "birth", _tag("LEVEL", "int16"), _sample(17, "bad_comm"), True),
    ("bad_config is null even with a last value", "birth", _tag("LEVEL", "int16"), _sample(17, "bad_config"), True),
    ("bad_decode is null", "birth", _tag("LEVEL", "int16"), _sample(17, "bad_decode"), True),
    ("stale keeps the last value", "birth", _tag("LEVEL", "int16"), _sample(17, "stale"), True),
    ("never read: stale and null", "birth", _tag("LEVEL", "int16"), _sample(None, "stale"), True),
    ("data: alias and value only", "data", _tag("FLOW", "float32"), _sample(2.5), False),
    ("data: quality change carries the Quality property", "data", _tag("LEVEL", "int16"), _sample(-7, "bad_comm"), True),
    ("data: bad_config is null", "data", _tag("T1", "float64"), _sample(1.0, "bad_config"), True),
    ("data: back to good", "data", _tag("LEVEL", "int16"), _sample(5), True),
    ("data: bool array", "data", _tag("BITS", "bool", array=True), _sample([True]), False),
    # errors: the edge node turns these into bad_decode (null) before publishing
    ("error: int16 out of range", "birth", _tag("LEVEL", "int16"), _sample(40000), True),
    ("error: uint16 negative", "birth", _tag("COUNT", "uint16"), _sample(-1), True),
    ("error: uint64 above range", "birth", _tag("U64", "uint64"), _sample(2**64), True),
    ("error: bool given an integer", "birth", _tag("RUN", "bool"), _sample(1), True),
    ("error: string given a number", "birth", _tag("MODE", "string"), _sample(5), True),
    ("error: float32 given a string", "birth", _tag("FLOW", "float32"), _sample("1.0"), True),
    ("error: int16 given a float", "data", _tag("LEVEL", "int16"), _sample(1.5), True),
    ("error: a bool tag cannot be scaled", "birth", _tag("RUN", "bool", scaled=True), _sample(True), True),
    ("error: unknown dtype", "birth", _tag("X", "int8"), _sample(1), True),
    ("error: NUL in a string array element", "birth", _tag("NAMES", "string", array=True), _sample(["a\0b"]), True),
    ("error: int16 array element out of range", "birth", _tag("ARR16", "int16", array=True), _sample([1, 70000]), True),
    ("error: array tag given a scalar", "birth", _tag("ARR16", "int16", array=True), _sample(1), True),
]


def metric_vectors() -> dict:
    cases = []
    for name, kind, tag, sample, with_quality in METRIC_CASES:
        case = {"name": name, "kind": kind, "tag": tag, "alias": 7, "sample": sample}
        if kind == "data":
            case["with_quality"] = with_quality
        try:
            if kind == "birth":
                m = birth_metric(tag, 7, sample)
            else:
                m = data_metric(7, datatype_for(tag["dtype"], tag["scaled"], tag["array"]), sample, with_quality)
        except ValueError as exc:
            case["error"] = str(exc)
        else:
            case["metric_b64"] = b64(m.SerializeToString())
            case["decoded"] = _decoded(m)
        cases.append(case)
    return {"cases": cases}


DEVICES = {
    "Line 1.PLC1": [_tag("FLOW", "float32"), _tag("RUN", "bool"), _tag("LEVEL.Pv", "int16"), _tag("MODE", "string")],
    "Line 2.PLC2": [_tag("T1", "float64", scaled=True, units="degC", eng_range=(-50, 150)),
                    _tag("ALARMS", "bool", array=True)],
}
BIRTH_VALUES = {
    "Line 1.PLC1": [_sample(12.5), _sample(True), _sample(-3), _sample("AUTO")],
    "Line 2.PLC2": [_sample(21.25), _sample(None, "stale")],
}


def message_vectors() -> dict:
    """One edge node session, op by op: what the Rust replay must reproduce byte for byte."""
    group, edge = "Plant A", "opencap-1"
    s = Session()
    steps: list[dict] = []
    aliases: dict[tuple[str, str], int] = {}
    datatypes: dict[tuple[str, str], str] = {}
    next_alias = 1
    node = {"bd_seq": next_alias, "rebirth": next_alias + 1}
    next_alias += 2
    for dev, tags in DEVICES.items():
        for t in tags:
            aliases[(dev, t["name"])] = next_alias
            datatypes[(dev, t["name"])] = datatype_for(t["dtype"], t["scaled"], t["array"])
            next_alias += 1

    def emit(op: dict, kind: str, payload, device: str | None = None) -> None:
        op.update(kind=kind, topic=topic(group, kind, edge, device), qos=QOS[kind], retain=False,
                  payload_b64=b64(payload.SerializeToString()))
        steps.append(op)

    def connect(ts: int) -> int:
        bd = s.connect()
        emit({"op": "ndeath", "ts_ms": ts, "bd_seq": bd}, "NDEATH", ndeath(ts, bd))
        return bd

    def birth(ts: int, bd: int) -> None:
        emit({"op": "nbirth", "ts_ms": ts, "bd_seq": bd, "aliases": node, "seq": s.birth()}, "NBIRTH",
             nbirth(ts, bd, node))
        for dev, tags in DEVICES.items():
            metrics = [{"tag": t, "alias": aliases[(dev, t["name"])], "sample": v}
                       for t, v in zip(tags, BIRTH_VALUES[dev])]
            seq = s.next_seq()
            emit({"op": "dbirth", "ts_ms": ts, "seq": seq, "device": dev, "metrics": metrics}, "DBIRTH",
                 _payload(ts, seq, [birth_metric(m["tag"], m["alias"], m["sample"]) for m in metrics]), dev)

    def ddata(ts: int, dev: str, changes: list[tuple[str, dict, bool]]) -> None:
        metrics = [{"alias": aliases[(dev, n)], "datatype": datatypes[(dev, n)], "sample": smp, "with_quality": wq}
                   for n, smp, wq in changes]
        seq = s.next_seq()
        emit({"op": "ddata", "ts_ms": ts, "seq": seq, "device": dev, "metrics": metrics}, "DDATA",
             _payload(ts, seq, [data_metric(m["alias"], m["datatype"], m["sample"], m["with_quality"])
                                for m in metrics]), dev)

    def ddeath(ts: int, dev: str) -> None:
        seq = s.next_seq()
        emit({"op": "ddeath", "ts_ms": ts, "seq": seq, "device": dev}, "DDEATH", _payload(ts, seq, []), dev)

    t0 = 1_700_000_000_000
    bd = connect(t0)
    birth(t0 + 10, bd)
    ddata(t0 + 1000, "Line 1.PLC1", [("FLOW", _sample(13.0, ts_ms=t0 + 990), False),
                                     ("LEVEL.Pv", _sample(-3, "bad_comm", ts_ms=t0 + 990), True)])
    ddata(t0 + 2000, "Line 2.PLC2", [("T1", _sample(21.5, "bad_config", ts_ms=t0 + 1990), True)])
    ddeath(t0 + 3000, "Line 2.PLC2")
    ddata(t0 + 4000, "Line 1.PLC1", [("RUN", _sample(False, ts_ms=t0 + 3990), False),
                                     ("LEVEL.Pv", _sample(4, ts_ms=t0 + 3990), True)])
    birth(t0 + 5000, bd)                                    # after an NCMD Node Control/Rebirth
    bd = connect(t0 + 9000)                                 # a new MQTT session
    birth(t0 + 9010, bd)

    seq = Session()
    seq.birth()
    wrap = [seq.next_seq() for _ in range(258)]
    bd_wrap = Session(254)
    return {"group": group, "edge_node": edge, "steps": steps,
            "seq_after_birth": wrap, "bd_seq_from_254": [bd_wrap.connect() for _ in range(3)]}


def _cmd(metrics: list[dict]) -> bytes:
    p = payload_class()(timestamp=TS)
    for m in metrics:
        p.metrics.add(**m)
    return p.SerializeToString()


def command_vectors() -> dict:
    ncmd, dcmd = "spBv1.0/Plant A/NCMD/opencap-1", "spBv1.0/Plant A/DCMD/opencap-1/Line 1.PLC1"
    rebirth_true = {"name": REBIRTH, "datatype": dt("Boolean"), "boolean_value": True}
    cases = [
        ("rebirth by name", ncmd, [rebirth_true]),
        ("rebirth by alias, no datatype", ncmd, [{"alias": 2, "boolean_value": True}]),
        ("rebirth false is not a request", ncmd, [{"name": REBIRTH, "boolean_value": False}]),
        ("rebirth with a non-boolean value", ncmd, [{"name": REBIRTH, "int_value": 1}]),
        ("another node control is rejected", ncmd, [{"name": "Node Control/Reboot", "boolean_value": True}]),
        ("a write next to a rebirth: rebirth honoured, write rejected", ncmd,
         [{"name": "Node Control/Next Server", "boolean_value": True}, rebirth_true]),
        ("unknown alias", ncmd, [{"alias": 9, "double_value": 1.0}]),
        ("no metrics", ncmd, []),
        ("device write by alias", dcmd, [{"alias": 3, "double_value": 50.0}]),
        ("device write by name", dcmd, [{"name": "FLOW", "datatype": dt("Float"), "float_value": 50.0}]),
        ("a DCMD named like the node control is still rejected", dcmd, [rebirth_true]),
        ("rebirth alias in a DCMD", dcmd, [{"alias": 2, "boolean_value": True}]),
    ]
    out = []
    for name, t, metrics in cases:
        payload = _cmd(metrics)
        out.append({"name": name, "topic": t, "rebirth_alias": 2, "payload_b64": b64(payload),
                    **classify(t, payload, 2)})
    for name, t, payload in (("not a command topic", "spBv1.0/Plant A/NDATA/opencap-1", _cmd([rebirth_true])),
                             ("STATE is not a command", "spBv1.0/STATE/host", _cmd([rebirth_true])),
                             ("not protobuf", ncmd, b"\x0a\xff")):
        out.append({"name": name, "topic": t, "rebirth_alias": 2, "payload_b64": b64(payload), "error": True})
    return {"cases": out}


def topic_vectors() -> dict:
    valid = ["spBv1.0/Plant A/NBIRTH/opencap-1", "spBv1.0/Plant A/NDEATH/opencap-1", "spBv1.0/G/NDATA/E",
             "spBv1.0/G/NCMD/E", "spBv1.0/Plant A/DBIRTH/opencap-1/Line 1.PLC1", "spBv1.0/G/DDEATH/E/D",
             "spBv1.0/G/DDATA/E/Pumpe Süd", "spBv1.0/G/DCMD/E/D", "spBv1.0/STATE/ignition-primary"]
    invalid = ["spAv1.0/G/NBIRTH/E", "spBv1.0/G/NBIRTH", "spBv1.0/G/NBIRTH/E/D", "spBv1.0/G/DDATA/E",
               "spBv1.0/G/NOPE/E", "spBv1.0/STATE", "spBv1.0/STATE/a/b", "spBv1.0//NBIRTH/E", "spBv1.0/G/NCMD/",
               "spBv1.0/G/NCMD/E+", "spBv1.0/G/DDATA/E/D#", "STATE/legacy-host"]
    build = [("Plant A", "NBIRTH", "opencap-1", None), ("Plant A", "DDATA", "opencap-1", "Line 1.PLC1"),
             ("G", "DDATA", "E", None), ("G", "NDATA", "E", "D"), ("G/H", "NDATA", "E", None),
             ("G", "NDATA", "E#", None), ("G", "DCMD", "E", ""), ("", "NBIRTH", "E", None)]
    built = []
    for group, kind, edge, device in build:
        case: dict[str, Any] = {"group": group, "kind": kind, "edge_node": edge, "device": device}
        try:
            case["topic"] = topic(group, kind, edge, device)
        except ValueError as exc:
            case["error"] = str(exc)
        built.append(case)
    return {"valid": [{"topic": t, "parsed": parse_topic(t)} for t in valid],
            "invalid": invalid, "build": built}


def state_vectors() -> dict:
    payloads = ['{"online": true, "timestamp": 1700000000123}', '{"online":false,"timestamp":0,"extra":1}',
                '{"timestamp": 5, "online": false}', "ONLINE", "OFFLINE", '{"online": "true", "timestamp": 1}',
                '{"online": true}', '{"online": true, "timestamp": -1}', '{"online": true, "timestamp": 1.5}',
                "[]", "null"]
    cases = []
    for p in payloads:
        try:
            cases.append({"payload": p, **parse_state(p)})
        except ValueError:
            cases.append({"payload": p, "error": True})
    return {"cases": cases}


FILES = {"metrics.json": metric_vectors, "messages.json": message_vectors, "commands.json": command_vectors,
         "topics.json": topic_vectors, "state.json": state_vectors}


def render() -> dict[str, str]:
    """{file name: content} of every vector file."""
    sha = hashlib.sha256(PROTO.read_bytes()).hexdigest()
    out = {}
    for name, build in FILES.items():
        doc = {"about": "Generated by tests/helpers/sparkplug_ref.py (python -m tests.helpers.sparkplug_ref); "
                        "do not edit. Spec: docs/spec/payload-sparkplug.md.",
               "proto_sha256": sha, **build()}
        out[name] = json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
    return out


def main(argv: list[str]) -> int:
    stale = []
    for name, text in render().items():
        path = DIR / name
        if "--check" in argv:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8")
    if stale:
        print(f"stale Sparkplug vectors: {', '.join(stale)} (run python -m tests.helpers.sparkplug_ref)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
