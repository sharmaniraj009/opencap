"""Northbound OPC UA server, pure part: types, quality -> StatusCode, value coercion, config."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from opencap.config.models import OpcUaServerCfg, PollerConfig, Scale, Tag
from opencap.core.cache import TagValue
from opencap.core.quality import STATUS_CODES
from opencap.north.opcua_server import (
    SYSTEM_VARS,
    _security_policy_type,
    coerce,
    status_code,
    variant_type,
)

TS = datetime(2026, 11, 1, tzinfo=UTC)


def test_variant_types():
    assert variant_type(Tag(name="a", address="x", dtype="float32")) == "Float"
    assert variant_type(Tag(name="a", address="x", dtype="int16")) == "Int16"
    assert variant_type(Tag(name="a", address="x", dtype="bool")) == "Boolean"
    assert variant_type(Tag(name="a", address="x", dtype="string")) == "String"
    assert variant_type(Tag(name="a", address="x", dtype="int16", scale=Scale(eng_hi=10))) == "Double"


@pytest.mark.parametrize("tv, name", [
    (TagValue(1.0, "good", TS), "Good"),
    (TagValue(1.0, "stale", TS), "UncertainLastUsableValue"),
    (TagValue(None, "stale", None), "BadWaitingForInitialData"),       # registered, never read
    (TagValue(1.0, "bad_comm", TS), "BadCommunicationError"),
    (TagValue(None, "bad_comm", None), "BadCommunicationError"),
    (TagValue(None, "bad_config", None), "BadConfigurationError"),
    (TagValue(None, "bad_decode", None), "BadDataEncodingInvalid"),
])
def test_status_code(tv, name):
    """The server maps through opencap.core.quality (ADR-0006; codes checked in test_quality.py)."""
    assert status_code(tv) == STATUS_CODES[name]


@pytest.mark.parametrize("vtype, value, want", [
    ("Double", 3, 3.0), ("Float", True, 1.0), ("Int16", 7.0, 7), ("UInt32", 4, 4), ("Boolean", 1, True),
    ("String", 12, "12"), ("Double", [1, 2], [1.0, 2.0]), ("Double", math.nan, math.nan), ("Int16", None, None),
])
def test_coerce(vtype, value, want):
    got = coerce(vtype, value)
    assert got == want or (isinstance(want, float) and math.isnan(want) and math.isnan(got))


@pytest.mark.parametrize("vtype, value", [("Int16", 40000), ("UInt16", -1), ("Int32", math.inf),
                                          ("Double", "1.5"), ("Boolean", "yes"), ("Int16", "3")])
def test_coerce_rejects(vtype, value):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        coerce(vtype, value)


def test_policy_names_map_to_asyncua():
    ua = pytest.importorskip("asyncua").ua
    from typing import get_args

    from opencap.config.models import OpcUaServerPolicy
    for name in get_args(OpcUaServerPolicy):
        assert hasattr(ua.SecurityPolicyType, _security_policy_type(name)), name


def test_system_vars_from_a_device_status():
    from opencap.config.models import Device

    d = Device(id="S/P", channel="S", name="P", driver="modbus_tcp", host="192.0.2.5", port=502, tags=[])
    st = {"state": "demoted", "last_good": "2026-11-16T10:15:00.123Z", "errors": 4, "cycle_ms": 12.5,
          "last_error": "timeout", "connected": False}
    values = {name: get(st, d) for name, _t, get in SYSTEM_VARS}
    assert values == {
        "_State": "demoted", "_Error": True, "_NoError": False, "_AutoDemoted": True, "_Connected": False,
        "_LastGood": datetime(2026, 11, 16, 10, 15, 0, 123000, tzinfo=UTC), "_ErrorCount": 4,
        "_LastError": "timeout", "_CycleMs": 12.5, "_DeviceId": "192.0.2.5:502", "_Driver": "modbus_tcp",
        "_Enabled": True,
    }


def test_config_is_off_by_default_and_validated():
    cfg = PollerConfig().opcua_server
    assert cfg.enabled is False and cfg.security_policies == ["None"] and cfg.anonymous is False
    assert cfg.allow_insecure_remote is False
    assert cfg.endpoint.startswith("opc.tcp://127.0.0.1:")
    with pytest.raises(ValidationError, match="certificate"):
        OpcUaServerCfg(security_policies=["Basic256Sha256_SignAndEncrypt"])
    with pytest.raises(ValidationError, match="username_env"):
        OpcUaServerCfg(username_env="OPENCAP_UA_USER")
    with pytest.raises(ValidationError):
        OpcUaServerCfg(security_policies=["Rot13_Sign"])
    with pytest.raises(ValidationError, match="opc.tcp"):
        OpcUaServerCfg(endpoint="http://127.0.0.1/ua")


@pytest.mark.parametrize("endpoint", ["opc.tcp://0.0.0.0:4840/opencap", "opc.tcp://192.0.2.10:4840",
                                      "opc.tcp://opencap.example:4840/ua", "opc.tcp://[2001:db8::1]:4840"])
def test_plaintext_beyond_this_computer_is_refused_unless_allowed(endpoint):
    with pytest.raises(ValidationError, match="allow_insecure_remote"):
        OpcUaServerCfg(endpoint=endpoint)
    with pytest.raises(ValidationError, match="allow_insecure_remote"):
        OpcUaServerCfg(endpoint=endpoint, security_policies=["None", "Basic256Sha256_SignAndEncrypt"],
                       certificate="c.der", private_key="k.pem")
    assert OpcUaServerCfg(endpoint=endpoint, allow_insecure_remote=True).security_policies == ["None"]
    secure = OpcUaServerCfg(endpoint=endpoint, security_policies=["Basic256Sha256_SignAndEncrypt"],
                            certificate="c.der", private_key="k.pem")
    assert secure.security_policies == ["Basic256Sha256_SignAndEncrypt"]


@pytest.mark.parametrize("endpoint", ["opc.tcp://127.0.0.1:4840/opencap", "opc.tcp://localhost:4840",
                                      "opc.tcp://[::1]:4840/opencap", "opc.tcp://127.0.0.5:4840"])
def test_plaintext_on_loopback_is_allowed(endpoint):
    assert OpcUaServerCfg(endpoint=endpoint).security_policies == ["None"]


def test_an_enabled_server_needs_a_user_or_anonymous():
    with pytest.raises(ValidationError, match="anonymous is off and no user"):
        OpcUaServerCfg(enabled=True)
    assert OpcUaServerCfg(enabled=True, anonymous=True).anonymous is True
    assert OpcUaServerCfg(enabled=True, username_env="U", password_env="P").username_env == "U"


@pytest.mark.parametrize("name", ["_System", "_system._Error", "_SYSTEM.X"])
def test_system_is_a_reserved_tag_group(name):
    from opencap.config.models import Device

    with pytest.raises(ValidationError, match="reserved group name '_System'"):
        Device(id="S/P", channel="S", name="P", driver="modbus_tcp", host="192.0.2.5", port=502,
               tags=[{"name": name, "address": "400001", "dtype": "int16"}])
    # a name that only starts with the same letters is fine
    Device(id="S/P", channel="S", name="P", driver="modbus_tcp", host="192.0.2.5", port=502,
           tags=[{"name": "_SystemTime", "address": "400001", "dtype": "int16"}])
