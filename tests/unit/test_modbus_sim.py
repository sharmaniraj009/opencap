"""The Modbus test simulator's encoder against hand-computed register images (no sockets).

These anchor `tests/helpers/modbus_sim.py` to Kepware semantics independently of the
driver's decoder, so the integration round trip (sim -> wire -> driver) means something.
"""

from __future__ import annotations

import math
import struct

import pytest

from opencap.config.models import Device, Tag
from tests.helpers.modbus_sim import SIM_PARAMS, SimImage, make_sim_devices, sim_slot


def device(tags: list[Tag], **params) -> Device:
    return Device(id="T/PLC", channel="T", name="PLC", driver="modbus_tcp", host="127.0.0.1", port=0,
                  params=params, tags=tags)


def words_after(address: str, dtype: str, value, **params) -> list[int]:
    img = SimImage(device([Tag(name="X", address=address, dtype=dtype)], **params))
    img.set_value("X", value)
    slot = img.slots["X"]
    assert img.value("X") == value or (isinstance(value, float) and math.isnan(value))
    return img.words[slot.area][slot.start:slot.start + slot.size]


# --- addresses ---------------------------------------------------------------------------------

def test_zero_based_and_five_digit_addresses():
    assert sim_slot("400010", "uint16", {"zero_based": True}).start == 9
    assert sim_slot("400010", "uint16", {"zero_based": False}).start == 10
    assert sim_slot("40010", "uint16", {}).start == 9
    assert sim_slot("300001", "float32", {}).area == "input"
    assert sim_slot("000005", "bool", {}).area == "coil"
    assert sim_slot("100005", "bool", {}).start == 4
    assert sim_slot("400001", "bool", {}) is None              # register bool needs .b
    assert sim_slot("400001.0", "bool", {"zero_based_bit": False}) is None


def test_bit_numbering():
    assert sim_slot("400001.3", "bool", {}).bit == 3
    assert sim_slot("400001.3", "bool", {"modicon_bit_order": True}).bit == 12   # bit 0 = MSB
    assert sim_slot("400001.4", "bool", {"zero_based_bit": False}).bit == 3
    assert sim_slot("400001.16", "bool", {"zero_based_bit": False, "modicon_bit_order": True}).bit == 0


# --- 16/32/64-bit word order ---------------------------------------------------------------------

def test_16_bit():
    assert words_after("400001", "uint16", 0x1234) == [0x1234]
    assert words_after("400001", "int16", -1) == [0xFFFF]
    assert words_after("400001", "uint16", 0x1234, byte_order_big=False) == [0x3412]


def test_float32_word_orders():
    # 1.0f = 0x3F800000
    assert words_after("400001", "float32", 1.0) == [0x0000, 0x3F80]                      # first word low (default)
    assert words_after("400001", "float32", 1.0, first_word_low=False) == [0x3F80, 0x0000]
    assert words_after("400001", "float32", 1.0, byte_order_big=False) == [0x0000, 0x803F]
    assert words_after("400001", "float32", 1.0, first_word_low=False, byte_order_big=False) == [0x803F, 0x0000]


def test_int32_and_uint32():
    assert words_after("400001", "uint32", 0x12345678) == [0x5678, 0x1234]
    assert words_after("400001", "uint32", 0x12345678, first_word_low=False) == [0x1234, 0x5678]
    assert words_after("400001", "int32", -2) == [0xFFFE, 0xFFFF]


@pytest.mark.parametrize(("fwl", "fdl", "expected"), [
    (True, True, [0x7788, 0x5566, 0x3344, 0x1122]),
    (False, False, [0x1122, 0x3344, 0x5566, 0x7788]),
    (True, False, [0x3344, 0x1122, 0x7788, 0x5566]),
    (False, True, [0x5566, 0x7788, 0x1122, 0x3344]),
])
def test_64_bit_word_and_dword_order(fwl, fdl, expected):
    assert words_after("400001", "uint64", 0x1122334455667788,
                       first_word_low=fwl, first_dword_low=fdl) == expected


def test_float64():
    assert words_after("400001", "float64", 1.0) == [0, 0, 0, 0x3FF0]
    assert words_after("400001", "float64", 1.0, first_word_low=False, first_dword_low=False) == [0x3FF0, 0, 0, 0]


# --- bits, strings, arrays, coils ------------------------------------------------------------------

def test_bits_in_register_share_the_word():
    img = SimImage(device([
        Tag(name="B0", address="400001.0", dtype="bool"),
        Tag(name="B15", address="400001.15", dtype="bool"),
        Tag(name="W", address="400001", dtype="uint16", enabled=False),
    ]))
    img.set_value("W", 0)                             # the initial fill wrote W last
    img.set_value("B0", True)
    img.set_value("B15", True)
    assert img.words["holding"][0] == 0x8001
    img.set_value("B0", False)
    assert img.words["holding"][0] == 0x8000
    assert img.value("W") == 0x8000
    assert "W" not in img.expected()                  # disabled tags are not expected


def test_bit_with_modicon_order_and_swapped_bytes():
    img = SimImage(device([Tag(name="B", address="400001.3", dtype="bool")], modicon_bit_order=True))
    img.set_value("B", False)
    img.set_value("B", True)
    assert img.words["holding"][0] == 0x1000
    img = SimImage(device([Tag(name="B", address="400001.3", dtype="bool")], byte_order_big=False))
    img.set_value("B", True)
    assert img.words["holding"][0] == 0x0800          # logical 0x0008, bytes swapped on the wire


def test_strings_take_byte_order_from_suffix():
    assert words_after("400001.004H", "string", "ABC") == [0x4142, 0x4300]
    assert words_after("400001.004L", "string", "ABC") == [0x4241, 0x0043]
    assert words_after("400001.004H", "string", "ABC", byte_order_big=False) == [0x4142, 0x4300]
    with pytest.raises(ValueError):
        words_after("400001.002H", "string", "ABC")


def test_arrays_and_coils():
    assert words_after("400001[3]", "uint16", [1, 2, 3]) == [1, 2, 3]
    assert words_after("400001[2][2]", "int16", [[1, -1], [2, -2]]) == [1, 0xFFFF, 2, 0xFFFE]
    img = SimImage(device([Tag(name="C", address="000005[3]", dtype="bool"),
                           Tag(name="D", address="100001", dtype="bool")]))
    img.set_value("C", [True, False, True])
    img.set_value("D", True)
    assert img.bits["coil"][4:7] == [True, False, True]
    assert img.bits["discrete"][0] is True


def test_set_value_rejects_bad_values():
    img = SimImage(device([Tag(name="U", address="400001", dtype="uint16"),
                           Tag(name="B", address="000001", dtype="bool"),
                           Tag(name="A", address="400010[2]", dtype="uint16")]))
    for name, value in [("U", 70000), ("U", -1), ("U", 1.5), ("U", "x"), ("U", True),
                        ("B", 2), ("A", [1]), ("A", 5)]:
        with pytest.raises(ValueError):
            img.set_value(name, value)
    with pytest.raises(KeyError):
        img.set_value("NOPE", 1)


def test_float_rounding_and_nonnormal():
    img = SimImage(device([Tag(name="F", address="400001", dtype="float32")]))
    img.set_value("F", 0.1)
    assert img.value("F") == struct.unpack(">f", struct.pack(">f", 0.1))[0]
    img.set_value("F", float("nan"))
    assert img.value("F") == 0.0                      # Kepware "replace with zero" (D-006)
    img = SimImage(device([Tag(name="F", address="400001", dtype="float32")], nonnormal_float="unmodified"))
    img.set_value("F", float("inf"))
    assert img.value("F") == math.inf


def test_read_beyond_image_is_illegal():
    img = SimImage(device([Tag(name="U", address="400100", dtype="uint16")]))
    assert img.read("holding", 99, 1) == [img.value("U")]
    assert img.read("holding", 0, 100) is not None    # gaps read as zeros
    assert img.read("holding", len(img.words["holding"]), 1) is None
    assert img.read("input", 0, 1) is not None        # margin exists even for unused areas


# --- values and generator ----------------------------------------------------------------------------

def test_tick_changes_every_value_deterministically():
    dev = make_sim_devices(1, 53)[0]
    a, b = SimImage(dev), SimImage(dev)
    first = a.expected()
    assert first == b.expected()
    a.tick()
    b.tick()
    second = a.expected()
    assert second == b.expected()
    assert all(second[name] != first[name] for name in first)


def test_generator_is_deterministic_and_synthetic():
    devs = make_sim_devices(30, 53)
    assert devs == make_sim_devices(30, 53)
    assert devs != make_sim_devices(30, 53, seed=1)
    assert len({d.id for d in devs}) == 30
    assert sum(len(d.tags) for d in devs) == 30 * 53
    for d in devs:
        assert d.host == "127.0.0.1" and d.port == 0 and d.driver == "modbus_tcp"
        assert d.params == SIM_PARAMS
        assert d.source == {"simulated": True}
        assert any(t.name.endswith("_ALIAS") for t in d.tags)


def test_generator_places_every_tag_without_overlaps():
    for dev in make_sim_devices(30, 53):
        img = SimImage(dev)
        assert img.unplaced == []
        owner: dict[tuple[str, int], tuple] = {}       # (area, register/bit) -> slot key
        for slot in img.slots.values():
            for addr in range(slot.start, slot.start + slot.size):
                key = (slot.area, addr)
                # the same register is shared only by aliases or by .b bits of one register
                prev = owner.setdefault(key, slot.key)
                assert prev == slot.key or (prev[4] is not None and slot.bit is not None), (dev.id, key)


def test_generator_params_and_channels():
    devs = make_sim_devices(4, 10, params={"first_word_low": False}, per_channel=2, scan_ms=100)
    assert [d.channel for d in devs] == ["SIM_SITE_01", "SIM_SITE_01", "SIM_SITE_02", "SIM_SITE_02"]
    assert all(d.params["first_word_low"] is False and d.params["zero_based"] is True for d in devs)
    assert all(d.scan_ms == 100 and d.min_scan_ms == 100 for d in devs)
    assert all(len(d.tags) == 10 for d in devs)
