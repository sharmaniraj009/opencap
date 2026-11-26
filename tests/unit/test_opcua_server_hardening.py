"""Pure parts of the OPC UA server hardening (#28, D-088): array dimensions from addresses,
engineering units, the deadband rule, browse paging, interval revision, config validation."""

from __future__ import annotations

import pytest

pytest.importorskip("asyncua")

from asyncua import ua  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from opencap.config.models import Device, OpcUaServerCfg, Scale, Tag  # noqa: E402
from opencap.north.opcua_server import (  # noqa: E402
    array_dims,
    device_fingerprint,
    eu_information,
    eu_range,
    unece_unit_id,
)
from opencap.north.opcua_services import deadband_report, page_references, revise_sampling  # noqa: E402

ABS, PCT, NONE = int(ua.DeadbandType.Absolute), int(ua.DeadbandType.Percent), int(ua.DeadbandType.None_)


def dev(driver: str, *tags: Tag, params: dict | None = None) -> Device:
    return Device(id="S/P", channel="S", name="P", driver=driver, host="192.0.2.5", port=502,
                  params=params or {}, tags=list(tags))


@pytest.mark.parametrize(("driver", "address", "dtype", "dims"), [
    ("modbus_tcp", "400001", "int16", ()),
    ("modbus_tcp", "400001[4]", "int16", (4,)),
    ("modbus_tcp", "400001[2][3]", "float32", (2, 3)),
    ("modbus_rtu", "300010[8]", "uint16", (8,)),
    ("s7", "DB1.DBW0[5]", "int16", (5,)),
    ("melsec_mc", "D100[3]", "int16", (3,)),
    ("ab_logix", "Motor[3]", "float32", ()),          # Logix: element 3 of an array, a scalar
    ("modbus_tcp", "400001[0]", "int16", ()),        # refused by the driver: bad_config, scalar node
])
def test_array_dims_come_from_each_drivers_own_parser(driver, address, dtype, dims):
    tag = Tag(name="T", address=address, dtype=dtype)
    assert array_dims(dev(driver, tag), tag) == dims


def test_engineering_units_use_unece_codes():
    assert unece_unit_id("P1") == (ord("P") << 8) | ord("1")
    assert unece_unit_id("CEL") == (ord("C") << 16) | (ord("E") << 8) | ord("L")
    uri, unit, display = eu_information("°C")
    assert uri == "http://www.opcfoundation.org/UA/units/un/cefact" and unit == unece_unit_id("CEL")
    assert display == "°C"
    assert eu_information("furlongs/fortnight")[1] == -1          # unknown: -1, display text kept


def test_eu_range_is_sorted_and_only_for_scaled_tags():
    assert eu_range(Tag(name="A", address="1", dtype="int16")) is None
    reverse = Tag(name="B", address="1", dtype="int16", scale=Scale(raw_lo=0, raw_hi=10, eng_lo=50, eng_hi=-50))
    assert eu_range(reverse) == (-50.0, 50.0)


def test_fingerprint_changes_with_what_the_nodes_show():
    plain = Tag(name="T", address="400001", dtype="int16")
    base = device_fingerprint(dev("modbus_tcp", plain))
    for changed in (Tag(name="T", address="400001[4]", dtype="int16"),
                    Tag(name="T", address="400001", dtype="int16", description="Pump speed"),
                    Tag(name="T", address="400001", dtype="int16", scale=Scale(eng_hi=100, units="%"))):
        assert device_fingerprint(dev("modbus_tcp", changed)) != base


def dv(value, status=0):
    return ua.DataValue(ua.Variant(value), StatusCode=ua.StatusCode(status))


@pytest.mark.parametrize(("ref", "cur", "kind", "band", "span", "report"), [
    (None, 1.0, ABS, 5.0, None, True),                  # first value
    (10.0, 15.0, ABS, 5.0, None, False),                # exactly the deadband: not more than it
    (10.0, 15.5, ABS, 5.0, None, True),
    (10.0, 4.0, ABS, 5.0, None, True),
    (10, 13, ABS, 5.0, None, False),                    # integers
    ([1.0, 2.0], [1.0, 8.0], ABS, 5.0, None, True),     # arrays: any element
    ([1.0, 2.0], [3.0, 4.0], ABS, 5.0, None, False),
    ([1.0, 2.0], [1.0, 2.0, 3.0], ABS, 5.0, None, True),  # length changed
    (50.0, 54.0, PCT, 5.0, 100.0, False),               # 5 % of a 0..100 range = 5
    (50.0, 56.0, PCT, 5.0, 100.0, True),
    (1.0, 1.0001, NONE, 0.0, None, True),
    ("A", "A", ABS, 5.0, None, False),                  # non-numeric: any change
    ("A", "B", ABS, 5.0, None, True),
])
def test_deadband_against_the_last_reported_value(ref, cur, kind, band, span, report):
    assert deadband_report(dv(cur), None if ref is None else dv(ref), kind, band, span) is report


def test_a_status_change_is_always_reported_even_inside_the_deadband():
    assert deadband_report(dv(10.0, ua.StatusCodes.UncertainLastUsableValue), dv(10.0), ABS, 5.0) is True


def test_a_slow_ramp_crosses_the_deadband_in_the_end():
    """The bug asyncua has (D-088): +1 per sample under a deadband of 5 must report every 6th."""
    reported, last = [], None
    for value in range(20):
        current = dv(float(value))
        if deadband_report(current, last, ABS, 5.0):
            reported.append(value)
            last = current
    assert reported == [0, 6, 12, 18]


def test_browse_pages_and_interval_revision():
    refs = list(range(10))
    assert page_references(refs, 0, 4) == ([0, 1, 2, 3], [4, 5, 6, 7, 8, 9])
    assert page_references(refs, 3, 4) == ([0, 1, 2], [3, 4, 5, 6, 7, 8, 9])     # the client asks for fewer
    assert page_references(refs, 50, 4) == ([0, 1, 2, 3], [4, 5, 6, 7, 8, 9])    # the server's cap wins
    assert page_references(refs, 0, 100) == (refs, [])
    assert revise_sampling(-1, 500.0, 100.0) == 500.0          # -1: the publishing interval
    assert revise_sampling(0, 500.0, 100.0) == 100.0
    assert revise_sampling(1000, 500.0, 100.0) == 1000.0


def test_new_config_fields_are_validated():
    cfg = OpcUaServerCfg()
    assert cfg.kepserverex_namespace_index is True and cfg.max_sessions == 100 and cfg.users == []
    secure = {"security_policies": ["Basic256Sha256_SignAndEncrypt"]}
    assert OpcUaServerCfg(pki_dir="pki", **secure).pki_dir == "pki"        # pki_dir instead of cert paths
    with pytest.raises(ValidationError, match="not both"):
        OpcUaServerCfg(pki_dir="pki", trusted_certs_dir="t", **secure)
    with pytest.raises(ValidationError, match="x509_users needs pki_dir"):
        OpcUaServerCfg(x509_users=True, certificate="c.der", private_key="k.pem", **secure)
    with pytest.raises(ValidationError, match="x509_users needs pki_dir"):
        OpcUaServerCfg(x509_users=True, pki_dir="pki")                     # policy None only
    with pytest.raises(ValidationError, match="advertised_endpoint"):
        OpcUaServerCfg(advertised_endpoint="http://x")
    with pytest.raises(ValidationError, match="max_sessions must be >= 1"):
        OpcUaServerCfg(max_sessions=0)
    with pytest.raises(ValidationError, match="min_sampling_interval_ms must be >= 0"):
        OpcUaServerCfg(min_sampling_interval_ms=-1)
    many = OpcUaServerCfg(enabled=True, users=[{"username_env": "U1", "password_env": "P1"}])
    assert many.users[0].username_env == "U1"
    assert OpcUaServerCfg(enabled=True, x509_users=True, pki_dir="pki", **secure).x509_users
    with pytest.raises(ValidationError):
        OpcUaServerCfg(users=[{"username_env": "U1"}])                      # password_env missing
