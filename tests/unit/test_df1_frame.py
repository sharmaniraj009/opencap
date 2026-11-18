"""DF1 full-duplex link layer and PCCC message layer (`opencap.drivers.ab_df1`, pure parts).

Check vectors: the CRC is CRC-16/ARC (reflected polynomial 0xA001, initial value 0), whose
catalogue check value for b"123456789" is 0xBB3D. The frame vectors below were computed with
an independent bitwise CRC (`_crc_bitwise`) and checked by hand for the BCC (two's complement
of the 8-bit sum); see docs/decisions.md D-035 for why they are not the DF1 manual's own.
Requests and reply decoding are cross-checked against pycomm3's SLC driver, which sends the
same PCCC reads over EtherNet/IP.
"""

from __future__ import annotations

import math
import random
import struct

import pytest

from opencap.config.models import Tag
from opencap.drivers.ab_df1 import (
    DLE,
    FNC_TYPED_READ_2,
    FNC_TYPED_READ_3,
    Df1Message,
    Df1Params,
    FrameDecoder,
    LinkEvent,
    address_field,
    bcc,
    check_bytes,
    crc16,
    encode_frame,
    parse_message,
    pccc_values,
    stuff,
    typed_read_function,
)
from opencap.drivers.ab_pccc import PcccBlock, decode_block, plan_pccc


def _crc_bitwise(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def H(text: str) -> bytes:
    return bytes.fromhex(text)


# --- checks ---------------------------------------------------------------------------------------------

def test_crc16_catalogue_check_value():
    assert crc16(b"123456789") == 0xBB3D          # CRC-16/ARC check value
    assert crc16(b"") == 0


def test_crc16_table_matches_bitwise_reference():
    rng = random.Random(7)
    for n in (1, 2, 5, 64, 300):
        data = bytes(rng.randrange(256) for _ in range(n))
        assert crc16(data) == _crc_bitwise(data)


def test_bcc_is_twos_complement_of_the_sum():
    assert bcc(b"") == 0
    assert bcc(H("01 00 0f 00 34 12 a2 14 07 89 00 00")) == 0x64     # sum 0x19C -> 0x9C -> 0x64
    rng = random.Random(3)
    for _ in range(50):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
        assert (sum(data) + bcc(data)) & 0xFF == 0


def test_crc_covers_the_etx_byte():
    app = H("01 00 0f 00 34 12 a2 14 07 89 00 00")
    assert check_bytes(app, "crc") == _crc_bitwise(app + b"\x03").to_bytes(2, "little")
    assert check_bytes(app, "crc") != _crc_bitwise(app).to_bytes(2, "little")


# --- frames ---------------------------------------------------------------------------------------------

# Typed read of N7:0{10} (20 bytes) from station 1, TNS 0x1234.
READ_N7 = H("01 00 0f 00 34 12 a2 14 07 89 00 00")
# DLE (0x10) everywhere: TNS 0x1010, file 16 (F), element 16.
DLE_HEAVY = H("01 00 0f 00 10 10 a2 04 10 8a 10 00")


@pytest.mark.parametrize(("app", "check", "frame"), [
    (READ_N7, "crc", "10 02 01 00 0f 00 34 12 a2 14 07 89 00 00 10 03 8b c2"),
    (READ_N7, "bcc", "10 02 01 00 0f 00 34 12 a2 14 07 89 00 00 10 03 64"),
    (DLE_HEAVY, "crc", "10 02 01 00 0f 00 10 10 10 10 a2 04 10 10 8a 10 10 00 10 03 d6 08"),
    (DLE_HEAVY, "bcc", "10 02 01 00 0f 00 10 10 10 10 a2 04 10 10 8a 10 10 00 10 03 80"),
    # a check byte that is 0x10 is sent once, not stuffed
    (H("01 00 06 00 e9 00"), "bcc", "10 02 01 00 06 00 e9 00 10 03 10"),
])
def test_frame_vectors(app, check, frame):
    assert encode_frame(app, check) == H(frame)
    events = FrameDecoder(check).feed(H(frame))
    assert events == [LinkEvent("frame", app)]


def test_stuffing_doubles_every_dle():
    assert stuff(b"\x10") == b"\x10\x10"
    assert stuff(b"a\x10\x10b") == b"a\x10\x10\x10\x10b"
    assert stuff(b"plain") == b"plain"


@pytest.mark.parametrize("check", ["crc", "bcc"])
def test_round_trip_random_frames_in_every_split(check):
    rng = random.Random(11)
    for trial in range(60):
        n = rng.randrange(6, 120)
        app = bytes(rng.choice((DLE, DLE, 0x02, 0x03, 0x06, 0x15, 0x05, rng.randrange(256))) for _ in range(n))
        frame = encode_frame(app, check)
        decoder = FrameDecoder(check)
        events: list[LinkEvent] = []
        cuts = sorted(rng.sample(range(1, len(frame)), min(4, len(frame) - 1)))
        for a, b in zip([0, *cuts], [*cuts, len(frame)]):
            events += decoder.feed(frame[a:b])
        assert events == [LinkEvent("frame", app)], trial
    # one byte at a time
    decoder = FrameDecoder(check)
    out = []
    for byte in encode_frame(DLE_HEAVY, check):
        out += decoder.feed(bytes((byte,)))
    assert out == [LinkEvent("frame", DLE_HEAVY)]


def test_response_symbols_outside_and_inside_frames():
    d = FrameDecoder("crc")
    assert d.feed(H("10 06 10 15 10 05")) == [LinkEvent("ack"), LinkEvent("nak"), LinkEvent("enq")]
    frame = encode_frame(READ_N7, "crc")
    embedded = frame[:7] + H("10 06") + frame[7:]           # a DLE ACK in the middle of a frame
    assert d.feed(embedded) == [LinkEvent("ack"), LinkEvent("frame", READ_N7)]


def test_noise_before_a_frame_is_skipped():
    d = FrameDecoder("crc")
    assert d.feed(b"\x00\xff garbage \x03" + encode_frame(READ_N7, "crc")) == [LinkEvent("frame", READ_N7)]
    assert d.noise > 0


def test_bad_check_is_reported_with_the_reason():
    frame = bytearray(encode_frame(READ_N7, "crc"))
    frame[-1] ^= 0xFF
    (ev,) = FrameDecoder("crc").feed(bytes(frame))
    assert ev.kind == "bad" and "CRC mismatch" in ev.reason
    frame = bytearray(encode_frame(READ_N7, "bcc"))
    frame[5] ^= 0x01                                         # a data byte changed on the line
    (ev,) = FrameDecoder("bcc").feed(bytes(frame))
    assert ev.kind == "bad" and "BCC mismatch" in ev.reason


def test_wrong_check_mode_never_passes():
    (ev,) = FrameDecoder("crc").feed(encode_frame(READ_N7, "bcc") + b"\x00")
    assert ev.kind == "bad"


def test_malformed_frames():
    d = FrameDecoder("bcc")
    # DLE STX inside a frame restarts it; the restarted frame is fine
    assert d.feed(H("10 02 01 02 03") + encode_frame(READ_N7, "bcc")) == [
        LinkEvent("bad", reason="DLE STX inside a frame (frame restarted)"), LinkEvent("frame", READ_N7)]
    (ev,) = d.feed(H("10 02 01 02 10 41"))
    assert ev.kind == "bad" and "invalid DLE sequence" in ev.reason
    (ev,) = d.feed(encode_frame(H("01 00 06"), "bcc"))
    assert ev.kind == "bad" and "shorter" in ev.reason
    (ev,) = d.feed(b"\x10\x02" + b"\x00" * 600)
    assert ev.kind == "bad" and "longer" in ev.reason
    assert d.feed(encode_frame(READ_N7, "bcc")) == [LinkEvent("frame", READ_N7)]   # recovers


def test_unknown_check_mode():
    with pytest.raises(ValueError, match="crc or bcc"):
        FrameDecoder("lrc")
    with pytest.raises(ValueError, match="crc or bcc"):
        encode_frame(b"123456", "lrc")


# --- messages --------------------------------------------------------------------------------------------

def test_message_round_trip_and_ext_sts():
    msg = Df1Message(1, 0, 0x0F, 0, 0xBEEF, b"\xa2\x02\x07\x89\x00\x00")
    assert parse_message(msg.encode()) == msg
    assert msg.encode()[:6] == H("01 00 0f 00 ef be")
    reply = parse_message(H("00 01 4f f0 ef be 06"))
    assert (reply.sts, reply.ext_sts, reply.reply_data) == (0xF0, 0x06, b"")
    ok = parse_message(H("00 01 4f 00 ef be 2a 00"))
    assert ok.ext_sts is None and ok.reply_data == b"\x2a\x00"
    with pytest.raises(ValueError, match="shorter"):
        parse_message(b"\x00\x01")


def test_address_field_encoding():
    assert address_field(0) == b"\x00"
    assert address_field(254) == b"\xfe"
    assert address_field(255) == b"\xff\xff\x00"
    assert address_field(300) == b"\xff\x2c\x01"
    with pytest.raises(ValueError):
        address_field(70000)


# --- typed reads vs pycomm3 -------------------------------------------------------------------------------

TAGS = [
    Tag(name="F", address="F34:3", dtype="float32"),
    Tag(name="F2", address="F34:5", dtype="float32"),
    Tag(name="N", address="N54:17", dtype="uint16"),
    Tag(name="NF", address="N66:10", dtype="float32"),
    Tag(name="BB", address="B3:17/14", dtype="bool"),
    Tag(name="BN", address="B3/0", dtype="bool"),
    Tag(name="IO", address="I:0/0", dtype="bool"),
    Tag(name="IO2", address="I:1.2", dtype="int16"),
    Tag(name="OUT", address="O:0/3", dtype="bool"),
    Tag(name="ST", address="ST56:10", dtype="string"),
    Tag(name="TP", address="T47:0.PRE", dtype="uint16"),
    Tag(name="TA", address="T4:1.ACC", dtype="uint16"),
    Tag(name="TD", address="T4:1.DN", dtype="bool"),
    Tag(name="CC", address="C5:2.CU", dtype="bool"),
    Tag(name="L", address="L9:4", dtype="int32"),
    Tag(name="S", address="S:5/0", dtype="bool"),
]


def _blocks() -> list[PcccBlock]:
    plan = plan_pccc(TAGS, {})
    assert not plan.invalid
    return plan.blocks          # type: ignore[return-value]


def _pycomm3_fields(address: str) -> bytes:
    """The function data pycomm3 1.2.16 `SLCDriver._read_tag` sends for `address` (after FNC)."""
    from pycomm3.cip.pccc import PCCC_DATA_SIZE, PCCC_DATA_TYPE
    from pycomm3.slc_driver import parse_tag

    t = parse_tag(address)
    assert t is not None, address
    return (bytes((PCCC_DATA_SIZE[t["file_type"]] * t["element_count"], int(t["file_number"])))
            + PCCC_DATA_TYPE[t["file_type"]] + bytes((int(t["element_number"]), int(t.get("pos_number", 0)))))


def test_typed_read_matches_pycomm3_for_every_block():
    for block in _blocks():
        fn = typed_read_function(block)
        assert fn[0] == FNC_TYPED_READ_3
        assert fn[1:] == _pycomm3_fields(block.address), block.address


def test_typed_read_two_field_variant():
    by_addr = {b.address: b for b in _blocks()}
    n = next(b for a, b in by_addr.items() if a.startswith("N54"))
    assert typed_read_function(n, FNC_TYPED_READ_2) == bytes((FNC_TYPED_READ_2,)) + _pycomm3_fields(n.address)[:-1]
    io = next(b for a, b in by_addr.items() if a.startswith("I:1.2"))
    assert typed_read_function(io, FNC_TYPED_READ_2)[0] == FNC_TYPED_READ_3      # needs its sub-element


def test_typed_read_large_numbers_use_extended_fields():
    block = PcccBlock(file_type="N", file_number=255, start=0, count=1, elem_size=2, address="N255:0", tags=[])
    assert typed_read_function(block) == H("a2 02 ff ff 00 89 00 00")
    too_big = PcccBlock(file_type="N", file_number=7, start=0, count=200, elem_size=2, address="N7:0{200}", tags=[])
    with pytest.raises(ValueError, match="one-byte"):
        typed_read_function(too_big)


def _raw_for(block: PcccBlock, rng: random.Random) -> bytes:
    if block.file_type == "ST":
        out = b""
        for i in range(block.count):
            text = f"str{i} {rng.randrange(10**6)}".encode()
            body = bytearray(text.ljust(82, b"\x00"))
            body[0::2], body[1::2] = body[1::2], body[0::2]
            out += struct.pack("<H", len(text)) + bytes(body)
        return out
    if block.file_type == "F":
        return b"".join(struct.pack("<f", rng.uniform(-1e4, 1e4)) for _ in range(block.count))
    return bytes(rng.randrange(256) for _ in range(block.nbytes))


def _same(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or a == b
    return type(a) is type(b) and a == b


def test_reply_values_match_pycomm3_for_every_block():
    from pycomm3.slc_driver import _parse_read_reply, parse_tag

    rng = random.Random(5)
    for block in _blocks():
        for _ in range(20):
            raw = _raw_for(block, rng)
            ours = pccc_values(block, raw)
            theirs = _parse_read_reply(parse_tag(block.address), raw).value
            assert _same(ours, theirs), (block.address, ours, theirs)
            assert decode_block(block, ours) == decode_block(block, theirs)


def test_short_reply_decodes_what_is_there():
    block = next(b for b in _blocks() if b.file_type == "F")
    assert block.count == 3                                   # F34:3..5
    values = pccc_values(block, struct.pack("<ff", 1.5, 2.5))
    assert values == [1.5, 2.5]
    out = decode_block(block, values)
    assert out["F"] == 1.5 and "missing" in str(out["F2"])
    assert pccc_values(block, b"") is None


def test_params_defaults_and_errors():
    p = Df1Params.from_mapping({})
    assert (p.station, p.src_station, p.error_check, p.ack_timeout_ms, p.nak_retries, p.enq_retries) == \
        (1, 0, "crc", 1000, 3, 3)
    assert p.read_fnc == FNC_TYPED_READ_3 and p.duplicate_detect and not p.close_on_timeout
    q = Df1Params.from_mapping({"station": "7", "error_check": "BCC", "read_fnc": "a1", "close_on_timeout": "yes"})
    assert (q.station, q.error_check, q.read_fnc, q.close_on_timeout) == (7, "bcc", FNC_TYPED_READ_2, True)
    for bad, why in (({"station": 255}, "station"), ({"error_check": "lrc"}, "error_check"),
                     ({"read_fnc": "68"}, "read_fnc"), ({"nak_retries": -1}, "nak_retries"),
                     ({"station": True}, "station"), ({"duplicate_detect": "maybe"}, "duplicate_detect")):
        with pytest.raises(ValueError, match=why):
            Df1Params.from_mapping(bad)
