"""opcua_client pure part: NodeId parser, params, block plan, StatusCode -> quality, value coercion.

None of this imports asyncua (D-002); the cross-checks against asyncua's own constants and
parser are skipped when asyncua is not installed.
"""

from __future__ import annotations

import math

import pytest

from opencap.config.models import Device, Tag
from opencap.drivers import get_driver_class
from opencap.drivers.base import DecodeError
from opencap.drivers.opcua_client import (
    CONFIG_STATUS,
    OpcUaBlock,
    OpcUaClientDriver,
    OpcUaParams,
    Sample,
    decode_value,
    parse_node_id,
    sample_to_tag,
    status_quality,
)

ENDPOINT = "opc.tcp://192.0.2.70:4841/Server"


def dev(tags: list[Tag], **params) -> Device:
    p = {"endpoint": ENDPOINT, **params}
    return Device(id="UA/E+H", channel="UA", name="E+H", driver="opcua_client", host="192.0.2.70",
                  port=4841, params=p, scan_ms=1000, min_scan_ms=1000, tags=tags)


def tag(name: str, address: str, dtype: str = "float64", **kw) -> Tag:
    return Tag(name=name, address=address, dtype=dtype, **kw)


# --- NodeId parser ----------------------------------------------------------------------------

@pytest.mark.parametrize("text, ns, nsu, kind, ident, key", [
    ("ns=7;s=LTI002_PV", 7, None, "s", "LTI002_PV", "ns=7;s=LTI002_PV"),
    ("  ns=7;s=LTI002_PV  ", 7, None, "s", "LTI002_PV", "ns=7;s=LTI002_PV"),
    ("ns = 7 ; s=A B", 7, None, "s", "A B", "ns=7;s=A B"),            # spaces inside s= are kept
    ("ns=2;s=Channel1.Device1.Tag;x=1", 2, None, "s", "Channel1.Device1.Tag;x=1", "ns=2;s=Channel1.Device1.Tag;x=1"),
    ("i=2258", 0, None, "i", 2258, "i=2258"),
    ("ns=0;i=2258", 0, None, "i", 2258, "i=2258"),
    ("ns=3;i=4294967295", 3, None, "i", 4294967295, "ns=3;i=4294967295"),
    ("s=Plain", 0, None, "s", "Plain", "s=Plain"),
    ("ns=4;g=09087E75-8E5E-499B-954F-F2A9603DB28A", 4, None, "g", "09087e75-8e5e-499b-954f-f2a9603db28a",
     "ns=4;g=09087e75-8e5e-499b-954f-f2a9603db28a"),
    ("ns=5;b=AQID", 5, None, "b", "AQID", "ns=5;b=AQID"),
    ("nsu=urn:vendor:plc;s=Motor.Speed", 0, "urn:vendor:plc", "s", "Motor.Speed", "nsu=urn:vendor:plc;s=Motor.Speed"),
    ("ns=7;;s=X", 7, None, "s", "X", "ns=7;s=X"),
])
def test_parse_node_id(text, ns, nsu, kind, ident, key):
    a = parse_node_id(text)
    assert (a.namespace, a.namespace_uri, a.kind, a.identifier, a.key) == (ns, nsu, kind, ident, key)
    assert parse_node_id(a.key) == a                     # the canonical form parses to the same address


@pytest.mark.parametrize("text, why", [
    ("", "empty"),
    ("   ", "empty"),
    ("LTI002_PV", "key=value"),
    ("ns=7", "no identifier"),
    ("ns=x;s=A", "namespace"),
    ("ns=65536;i=1", "namespace"),
    ("ns=-1;i=1", "namespace"),
    ("i=abc", "numeric"),
    ("i=-1", "numeric"),
    ("i=4294967296", "numeric"),
    ("s=", "empty"),
    ("g=not-a-guid", "GUID"),
    ("b=!!!", "base64"),
    ("b=", "empty"),
    ("ns=1;q=5", "unknown"),
    ("srv=1;ns=1;i=5", "unknown"),
    ("ns=1;nsu=urn:x;i=5", "both"),
    ("nsu=;i=5", "namespace URI"),
    ("ns=1;i=5;s=X", "after the identifier"),
    ("ns=1;ns=2;i=5", "twice"),
])
def test_parse_node_id_rejects(text, why):
    with pytest.raises(ValueError, match=why):
        parse_node_id(text)


def test_parser_agrees_with_asyncua():
    ua = pytest.importorskip("asyncua").ua
    for text in ("ns=7;s=LTI002_PV", "i=2258", "ns=3;i=70000", "ns=2;s=A;B=C", "ns=4;g=09087e75-8e5e-499b-954f-f2a9603db28a",
                 "ns=5;b=AQID"):
        ours = parse_node_id(text)
        theirs = ua.NodeId.from_string(text)
        assert ua.NodeId.from_string(ours.key) == theirs, text


# --- params -----------------------------------------------------------------------------------

def test_params_defaults_and_importer_keys():
    d = dev([], security_policy="None", message_mode="None", username_env=None, password_env=None,
            publishing_interval_ms=1000, sampling_interval_ms=500)
    p = OpcUaParams.from_device(d)
    assert (p.endpoint, p.security_policy, p.message_mode) == (ENDPOINT, "None", "None")
    assert (p.publishing_interval_ms, p.sampling_interval_ms, p.max_items) == (1000, 500, 100)
    assert p.username_env is None and p.certificate is None


def test_params_endpoint_falls_back_to_host_and_port():
    d = Device(id="UA/X", channel="UA", name="X", driver="opcua_client", host="192.0.2.71", port=4840, tags=[])
    assert OpcUaParams.from_device(d).endpoint == "opc.tcp://192.0.2.71:4840"


@pytest.mark.parametrize("spelling, canonical", [
    ("Basic256Sha256", "Basic256Sha256"), ("basic256sha256", "Basic256Sha256"),
    ("Aes128_Sha256_RsaOaep", "Aes128_Sha256_RsaOaep"), ("Aes128Sha256RsaOaep", "Aes128_Sha256_RsaOaep"),
    ("Aes256_Sha256_RsaPss", "Aes256_Sha256_RsaPss"),
])
def test_params_policy_spellings(spelling, canonical):
    p = OpcUaParams.from_device(dev([], security_policy=spelling, message_mode="signandencrypt",
                                    certificate="pki/c.der", private_key="pki/k.pem"))
    assert (p.security_policy, p.message_mode) == (canonical, "SignAndEncrypt")


@pytest.mark.parametrize("params, why", [
    ({"endpoint": "http://192.0.2.70/ua"}, "opc.tcp://"),
    ({"security_policy": "Rot13"}, "security_policy"),
    ({"message_mode": "Shout"}, "message_mode"),
    ({"security_policy": "None", "message_mode": "Sign"}, "needs a security_policy"),
    ({"security_policy": "Basic256Sha256", "message_mode": "None", "certificate": "c", "private_key": "k"},
     "Sign or SignAndEncrypt"),
    ({"security_policy": "Basic256Sha256", "message_mode": "Sign"}, "certificate"),
    ({"max_items": 0}, "max_items"),
    ({"max_items": True}, "max_items"),
    ({"publishing_interval_ms": 0}, "publishing_interval_ms"),
    ({"sampling_interval_ms": -5}, "sampling_interval_ms"),
    ({"session_timeout_ms": 10}, "session_timeout_ms"),
    ({"username_env": 5}, "username_env"),
    ({"password": "hunter2"}, "environment variable"),
    ({"username": "op"}, "environment variable"),
])
def test_params_rejected(params, why):
    with pytest.raises(ValueError, match=why):
        OpcUaParams.from_device(dev([], **params))


def test_secret_values_never_appear_in_param_errors():
    with pytest.raises(ValueError) as exc:
        OpcUaParams.from_device(dev([], password="s3cr3t-value"))
    assert "s3cr3t-value" not in str(exc.value)


# --- plan -------------------------------------------------------------------------------------

def test_registered_and_plan_is_pure():
    assert get_driver_class("opcua_client") is OpcUaClientDriver
    tags = [tag("LTI002.Pv", "ns=7;s=LTI002_PV"), tag("LTI002.Qv", "ns=7;s=LTI002_QV"),
            tag("ALIAS", "ns=7;s=LTI002_PV"), tag("BAD", "LTI002_PV"), tag("OFF", "ns=7;s=X", enabled=False)]
    plan = OpcUaClientDriver(dev(tags)).plan(tags)
    assert plan.invalid.keys() == {"BAD"} and "key=value" in plan.invalid["BAD"]
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert isinstance(block, OpcUaBlock)
    assert block.tags == ["LTI002.Pv", "ALIAS", "LTI002.Qv"]
    assert [it.node for it in block.items] == ["ns=7;s=LTI002_PV", "ns=7;s=LTI002_QV"]   # one item per node
    assert block.items[0].tags == (("LTI002.Pv", "float64"), ("ALIAS", "float64"))
    assert block.rate_ms == 1000 and block.items[0].sampling_ms == 1000
    assert block.wire_requests() == 0 and plan.wire_requests == 0     # subscriptions: nothing polled per cycle
    assert "2 items" in block.describe()


def test_plan_chunks_by_max_items_and_uses_sampling_param():
    tags = [tag(f"T{i}", f"ns=2;i={i}") for i in range(250)]
    plan = OpcUaClientDriver(dev(tags, max_items=100, sampling_interval_ms=500)).plan(tags)
    assert [len(b.items) for b in plan.blocks] == [100, 100, 50]
    assert {it.sampling_ms for b in plan.blocks for it in b.items} == {500}


def test_plan_rate_follows_the_rate_group():
    tags = [tag("FAST", "ns=2;i=1", scan_ms=2000), tag("SLOW", "ns=2;i=2", scan_ms=10_000)]
    d = dev(tags)
    drv = OpcUaClientDriver(d)
    rates = {rate: drv.plan(group).blocks[0].rate_ms for rate, group in d.rate_groups().items()}
    assert rates == {2000: 2000, 10_000: 10_000}


def test_plan_raises_on_bad_params():
    with pytest.raises(ValueError, match="max_items"):
        OpcUaClientDriver(dev([], max_items=-1))


# --- StatusCode -> quality ------------------------------------------------------------------------

@pytest.mark.parametrize("code, quality", [
    (0x00000000, "good"),
    (0x00300000, "good"),          # GoodClamped
    (0x00A70000, "good"),          # GoodLocalOverride
    (0x40930000, "stale"),         # UncertainSensorNotAccurate
    (0x40900000, "stale"),         # UncertainLastUsableValue
    (0x80340000, "bad_config"),    # BadNodeIdUnknown
    (0x80340400, "bad_config"),    # ... with info bits set
    (0x80330000, "bad_config"),    # BadNodeIdInvalid
    (0x80350000, "bad_config"),    # BadAttributeIdInvalid
    (0x803A0000, "bad_config"),    # BadNotReadable
    (0x801F0000, "bad_config"),    # BadUserAccessDenied
    (0x80050000, "bad_comm"),      # BadCommunicationError
    (0x80310000, "bad_comm"),      # BadNoCommunication
    (0x80320000, "bad_comm"),      # BadWaitingForInitialData
    (0x800A0000, "bad_comm"),      # BadTimeout
    (0x808B0000, "bad_comm"),      # BadDeviceFailure
    (0x80000000, "bad_comm"),      # plain Bad
    (0xC0000000, "bad_comm"),      # reserved severity: treated as Bad
])
def test_status_quality(code, quality):
    assert status_quality(code) == quality


def test_config_status_constants_match_asyncua():
    ua = pytest.importorskip("asyncua").ua
    assert {code: getattr(ua.StatusCodes, name) for code, name in CONFIG_STATUS.items()} == \
        {code: code for code in CONFIG_STATUS}


# --- values -------------------------------------------------------------------------------------

@pytest.mark.parametrize("dtype, value, want", [
    ("float64", 1.5, 1.5), ("float64", 7, 7.0), ("float32", True, 1.0), ("float64", math.inf, math.inf),
    ("int16", 7, 7), ("int16", -32768, -32768), ("int16", 3.6, 4), ("int16", True, 1),
    ("uint16", 65535, 65535), ("int32", -5, -5), ("uint32", 4294967295, 4294967295),
    ("int64", -2**63, -2**63), ("uint64", 2**64 - 1, 2**64 - 1),
    ("bool", True, True), ("bool", 0, False), ("bool", 2.0, True),
    ("string", "abc", "abc"), ("string", 12, "12"), ("string", b"ab", "ab"), ("string", False, "False"),
    ("float64", [1, 2.5], [1.0, 2.5]), ("int16", [1, 2], [1, 2]), ("bool", [[1, 0], [0, 1]], [[True, False], [False, True]]),
])
def test_decode_value(dtype, value, want):
    got = decode_value(dtype, value)
    assert got == want and type(got) is type(want)


@pytest.mark.parametrize("dtype, value", [
    ("float64", None), ("int16", 40000), ("uint16", -1), ("int16", math.nan), ("int32", math.inf),
    ("float64", "1.5"), ("bool", "true"), ("bool", math.nan), ("int16", [1, 40000]), ("float64", object()),
])
def test_decode_value_rejects(dtype, value):
    assert isinstance(decode_value(dtype, value), DecodeError)


def test_decode_localized_text_for_strings():
    class LocalizedText:          # duck-typed like asyncua's ua.LocalizedText
        Text = "Pump running"
        Locale = "en"
    assert decode_value("string", LocalizedText()) == "Pump running"


def test_sample_to_tag():
    from datetime import UTC, datetime
    ts = datetime(2026, 1, 1, tzinfo=UTC)

    def t(sample, dtype="float64"):
        tv = sample_to_tag(sample, dtype)
        return tv.quality, tv.value, tv.source_ts

    assert t(Sample(0, 2.5, ts)) == ("good", 2.5, ts)
    assert t(Sample(0x40930000, 2.5, ts)) == ("stale", None, ts)       # Uncertain: value not trusted
    assert t(Sample(0x80340000, None, None)) == ("bad_config", None, None)
    assert t(Sample(0, "x", ts)) == ("bad_decode", None, ts)
    assert t(None) == ("bad_comm", None, None)                          # nothing received for the node
