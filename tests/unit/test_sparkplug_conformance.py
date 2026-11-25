"""The Sparkplug B conformance vectors (tests/conformance/sparkplug/, #51).

- They are exactly what the Python reference (tests/helpers/sparkplug_ref.py) produces from the
  vendored proto, so nobody hand-edits them; the Rust codec replays them byte for byte
  (engine/north/sparkplug/tests/vectors.rs).
- Independently of both implementations, every payload in them obeys the Sparkplug 3.0 rules
  the edge node relies on (decoded here with Google's protobuf runtime).
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

pytest.importorskip("google.protobuf")

from tests.helpers import sparkplug_ref as ref  # noqa: E402


def _load(name: str) -> dict:
    return json.loads((ref.DIR / name).read_text(encoding="utf-8"))


def _payload(step: dict):
    return ref.payload_class().FromString(base64.b64decode(step["payload_b64"]))


def test_vectors_are_the_reference_output():
    rendered = ref.render()
    assert set(rendered) == {p.name for p in ref.DIR.glob("*.json")}
    for name, text in rendered.items():
        assert (ref.DIR / name).read_text(encoding="utf-8") == text, f"{name} is stale: python -m tests.helpers.sparkplug_ref"


def test_vectors_name_the_vendored_proto():
    sha = hashlib.sha256(ref.PROTO.read_bytes()).hexdigest()
    assert {_load(n)["proto_sha256"] for n in ref.FILES} == {sha}
    # unmodified Eclipse Tahu v1.0.19 (proto/third_party/README.md)
    assert sha == "4432c5c483b7fb9732d0594c98a2e97dca5e517e39c5374a8b918d837f0b4a19"


def test_array_vectors_match_the_spec_examples():
    """Sparkplug 3.0 §6.4.16 'Array Types' gives the exact bytes for these inputs."""
    cases = {c["name"]: c for c in _load("metrics.json")["cases"]}
    spec = {
        "int16 array (spec example)": "d08a3075",
        "uint16 array (spec example)": "1e0088cc",
        "int32 array (spec example)": "fffffffffaafcb12",
        "uint32 array (spec example)": "3400000049fb55c4",
        "uint64 array (spec example)": "3400000000000000d99e02d1b27637e4",
        "bool array (spec example, padding bits zero)": "0c00000034d0",
        "string array (spec example)": "41424300" + "68656c6c6f00",
    }
    for name, hexbytes in spec.items():
        assert cases[name]["decoded"]["bytes_value"] == hexbytes, name


def test_metric_vectors_follow_adr_0006():
    for c in _load("metrics.json")["cases"]:
        if "error" in c:
            continue
        d, q = c["decoded"], c["sample"]["quality"]
        props = {k: v for k, _t, v in d.get("properties", [])}
        if c["kind"] == "birth" or c.get("with_quality"):
            assert props["Quality"] == ref.QUALITY[q], c["name"]
        null = c["sample"]["value"] is None or q in ("bad_config", "bad_decode")
        assert d.get("is_null", False) is null, c["name"]
        assert (len({"int_value", "long_value", "float_value", "double_value", "boolean_value", "string_value",
                     "bytes_value"} & set(d)) == 1) is (not null), c["name"]


def test_message_vectors_obey_the_spec():
    doc = _load("messages.json")
    seq = None
    bd_seq = None
    declared: dict[str, set[int]] = {}
    node_aliases: set[int] = set()
    for step in doc["steps"]:
        p, kind = _payload(step), step["kind"]
        assert p.HasField("timestamp"), step["op"]
        if kind == "NDEATH":
            assert not p.HasField("seq") and [m.name for m in p.metrics] == [ref.BDSEQ]     # tck-id-topics-ndeath-*
            bd_seq = p.metrics[0].long_value
            assert (step["qos"], step["retain"]) == (1, False)
            continue
        assert (step["qos"], step["retain"]) == (0, False)
        if kind == "NBIRTH":
            assert p.seq == 0
            by_name = {m.name: m for m in p.metrics}
            assert by_name[ref.BDSEQ].datatype == ref.dt("Int64") and by_name[ref.BDSEQ].long_value == bd_seq
            assert by_name[ref.REBIRTH].boolean_value is False
            seq = 0
            node_aliases = {m.alias for m in p.metrics}
            declared = {}
        else:
            seq = (seq + 1) % 256
            assert p.seq == seq, step["op"]                                  # seq follows the NBIRTH
        if kind in ("NBIRTH", "DBIRTH"):
            for m in p.metrics:                                              # tck-id-payloads-alias-birth-requirement
                assert m.HasField("name") and m.HasField("alias") and m.HasField("timestamp") and m.HasField("datatype")
            if kind == "DBIRTH":
                aliases = {m.alias for m in p.metrics}
                others = set().union(node_aliases, *(a for d, a in declared.items() if d != step["device"]))
                assert not aliases & others, "aliases unique across the edge node"
                declared[step["device"]] = aliases
        if kind == "DDATA":
            for m in p.metrics:                                              # tck-id-payloads-alias-data-cmd-requirement
                assert not m.HasField("name") and not m.HasField("datatype") and m.HasField("timestamp")
                assert m.alias in declared[step["device"]], "alias from this device's DBIRTH"
        if kind == "DDEATH":
            assert len(p.metrics) == 0
    assert doc["seq_after_birth"][254:257] == [255, 0, 1] and doc["bd_seq_from_254"] == [254, 255, 0]


def test_command_vectors_never_execute_a_write():
    for c in _load("commands.json")["cases"]:
        if "error" in c:
            continue
        if "/DCMD/" in c["topic"]:
            assert c["rebirth"] is False and all(r[1] == ref.READ_ONLY for r in c["rejected"]), c["name"]
        names = [r[0] for r in c["rejected"]]
        payload = ref.payload_class().FromString(base64.b64decode(c["payload_b64"]))
        assert len(names) + int(c["rebirth"]) == len(payload.metrics), c["name"]   # every metric accounted for
