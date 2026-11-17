import pytest
from pydantic import ValidationError

from opencap.config.models import Device, DevicesFile, Channel, Scale, Tag


def _dev(tags, **kw):
    return Device(id="S/D", channel="S", name="D", driver="modbus_tcp", host="192.0.2.1", port=502, tags=tags, **kw)


def test_rate_groups_clamp_to_min_scan():
    d = _dev([
        Tag(name="A", address="400001", dtype="uint16", scan_ms=10),
        Tag(name="B", address="400002", dtype="uint16", scan_ms=100),
        Tag(name="C", address="400003", dtype="uint16", scan_ms=5000),
        Tag(name="D", address="400004", dtype="uint16"),
        Tag(name="E", address="400005", dtype="uint16", enabled=False),
    ])
    groups = d.rate_groups()
    assert list(groups) == [1000, 5000]
    assert [t.name for t in groups[1000]] == ["A", "B", "D"]
    assert [t.name for t in groups[5000]] == ["C"]


def test_duplicate_tag_names_rejected():
    with pytest.raises(ValidationError):
        _dev([Tag(name="A", address="400001", dtype="uint16"), Tag(name="A", address="400002", dtype="uint16")])


def test_duplicate_device_ids_rejected():
    d = _dev([])
    with pytest.raises(ValidationError):
        DevicesFile(channels=[Channel(name="S", devices=[d, d])])


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        Tag(name="A", address="1", dtype="uint16", bogus=1)


@pytest.mark.parametrize(
    "scale,raw,expected",
    [
        (Scale(raw_lo=0, raw_hi=10000, eng_lo=0, eng_hi=10), 5000, 5.0),
        (Scale(raw_lo=0, raw_hi=1, eng_lo=0, eng_hi=1000), 0.25, 250.0),
        (Scale(raw_lo=4000, raw_hi=20000, eng_lo=0, eng_hi=100), 12000, 50.0),
        (Scale(type="sqrt", raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10), 25, 5.0),
        (Scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, clamp_hi=True), 200, 10.0),
        (Scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, clamp_lo=True), -50, 0.0),
        (Scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, negate=True), 50, -5.0),
    ],
)
def test_scale_apply(scale, raw, expected):
    assert scale.apply(raw) == pytest.approx(expected)


def test_scale_zero_span():
    with pytest.raises(ValueError):
        Scale(raw_lo=1, raw_hi=1).apply(1)
