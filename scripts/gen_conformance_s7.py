"""Siemens S7 conformance vector inputs (planning, decoding, request frames and reply parsing, ADR-0014).

Case data only: `scripts/gen_conformance.py` imports `SPECS` from here, computes every
`expected` with `tests/conformance/reference.py` and writes the files. Run that script, not
this one. References: docs/decisions.md D-048 to D-052, D-085.
"""

from __future__ import annotations

import random
import struct
from typing import Any

from gen_conformance_ab import Case, FileSpec, T, plan_case

from tests.unit.test_s7_address import FORMS, REJECTED

_DTYPES = {"bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64", "string"}


def decode_case(cid: str, desc: str, tags: list[dict[str, Any]], data: bytes, **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags, "reply": {"bytes": data.hex()}}


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in text.strip())
    return "-".join(p for p in out.split("-") if p)


# --- s7/plan ------------------------------------------------------------------------------------

def s7_addresses() -> list[Case]:
    cases: list[Case] = []
    seen: set[str] = set()
    for address, dtype, _want in FORMS:
        cid = f"form-{_slug(address)}-{dtype}"
        if cid in seen:
            continue
        seen.add(cid)
        cases.append(plan_case(cid, f"{address.strip()} as {dtype} plans to one block.", [T("a", address, dtype)]))
    cases.append(plan_case("spellings-one-item", "Six spellings of one DBD are one item serving six names.",
                           [T(f"n{i}", a, "float32") for i, a in enumerate(
                               ("DB100,D12", "DB100,D012", "DB100,DBD12", "DB100.DBD12", "DB100DBD12", "db100,dword12"))]))
    cases.append(plan_case("arrays", "Arrays: one and two dimensional, bytes, REAL.",
                           [T("a", "DB1,W0[4]", "uint16"), T("b", "MD0[2][3]", "float32"), T("c", "DB1,B10[5]", "uint16"),
                            T("d", "DB2,REAL4[3]", "float32")]))
    cases.append(plan_case("string-default-length", "STRING without a length is 254 characters at PDU 480 and up.",
                           [T("a", "DB1,STRING0", "string")]))
    cases.append(plan_case("string-default-length-small-pdu", "STRING without a length is 210 characters at PDU 240.",
                           [T("a", "DB1,STRING0", "string")], max_pdu=240))
    return cases


def s7_rejected() -> list[Case]:
    cases = []
    seen: set[str] = set()
    for address, dtype, reason in REJECTED:
        if dtype not in _DTYPES:
            continue
        cid = f"reject-{_slug(address) or 'empty'}-{dtype}"
        if cid in seen:
            continue
        seen.add(cid)
        cases.append(plan_case(cid, f"{address!r} as {dtype} is rejected ({reason}); the good tag still plans.",
                               [T("BAD", address, dtype), T("GOOD", "MW0", "uint16")]))
    cases.append(plan_case("reject-disabled", "A disabled tag with a bad address is neither planned nor invalid.",
                           [T("OFF", "NOPE", "uint16", enabled=False)]))
    return cases


def s7_blocks() -> list[Case]:
    d = lambda n, a, t="uint16": T(n, a, t)  # noqa: E731
    return [
        plan_case("merge-gap", "Items within max_gap (16) merge; the gap is read and ignored.",
                  [d("a", "DB1,W0"), d("b", "DB1,W18")]),
        plan_case("split-gap", "A gap above max_gap splits.", [d("a", "DB1,W0"), d("b", "DB1,W20")]),
        plan_case("max-gap-0", "max_gap 0: only adjacent items merge.",
                  [d("a", "DB1,W0"), d("b", "DB1,W2"), d("c", "DB1,W5")], max_gap=0),
        plan_case("areas-never-merge", "Areas and DBs never share a block; blocks sorted by area then DB.",
                  [d("m", "MW0"), d("i", "IW0"), d("q", "QW0"), d("a", "DB2,W0"), d("b", "DB1,W0"), d("c", "C0"), d("t", "T0", "uint32")]),
        plan_case("overlap-and-bits", "Overlapping items and bits share a block.",
                  [d("a", "DB1,D0", "uint32"), d("b", "DB1,W2"), d("c", "DB1,X3.1", "bool"), d("e", "DB1,LWORD0", "uint64")]),
        plan_case("span-pdu-480", "A PDU of 480 carries 462 bytes: the tags split at the span.",
                  [d(f"t{i}", f"DB1,D{4 * i}", "float32") for i in range(0, 125)]),
        plan_case("span-pdu-240", "A PDU of 240 carries 222 bytes.",
                  [d(f"t{i}", f"DB1,D{4 * i}", "float32") for i in range(0, 60)], max_pdu=240),
        plan_case("span-pdu-960", "A PDU of 960 carries 942 bytes.",
                  [d(f"t{i}", f"DB1,D{4 * i}", "float32") for i in range(0, 250)], max_pdu=960),
        plan_case("too-big", "An item larger than the span is invalid.",
                  [d("big", "DB1,B0.470", "string"), d("ok", "DB1,W0")]),
        plan_case("exactly-the-span", "An item of exactly 462 bytes fits.", [d("a", "DB1,B0[462]", "uint16")]),
        plan_case("timers-counters", "Timers and counters merge in elements (2 bytes each); max_gap halves.",
                  [d("t0", "T0", "uint32"), d("t1", "T1", "uint32"), d("t9", "T9", "uint32"), d("t30", "T30", "uint32"),
                   d("c0", "C0"), d("c8", "C8")]),
        plan_case("timer-span", "Timer blocks span (pdu - 18) / 2 elements.",
                  [d(f"t{i}", f"T{i}", "uint32") for i in range(0, 240)]),
        plan_case("same-address", "One address under many names is read once.",
                  [d("a", "MW10"), d("b", "MW10"), d("c", "MW10", "int16")]),
        plan_case("order-independent", "The plan does not depend on the order of the tags.",
                  [d("z", "MW50"), d("a", "DB3,W2"), d("m", "MW10"), d("b", "DB3,W0")]),
        plan_case("strings", "S7 and raw strings.",
                  [d("s", "DB1,STRING0.20", "string"), d("r", "DB1,B30.10", "string")]),
    ]


def s7_params() -> list[Case]:
    tags = [T("a", "DB1,REAL0", "float32")]
    ok = [
        ("defaults", {}), ("full", {"rack": 0, "slot": 2, "max_pdu": 960, "link_type": 3, "byte_order_big": False}),
        ("link-name", {"link_type": "pg"}), ("link-spaced", {"link_type": " S7-Basic "}),
        ("null-is-default", {"slot": None, "link_type": None}), ("unknown-ignored", {"unknown": 1}),
        ("zero-one-bool", {"byte_order_big": 1}), ("pdu-240", {"max_pdu": 240}), ("pdu-960", {"max_pdu": 960}),
    ]
    bad = [
        ("rack-8", {"rack": 8}), ("rack-negative", {"rack": -1}), ("slot-32", {"slot": 32}), ("slot-bool", {"slot": True}),
        ("slot-text", {"slot": "1"}), ("pdu-low", {"max_pdu": 200}), ("pdu-high", {"max_pdu": 1920}),
        ("gap-negative", {"max_gap": -1}), ("link-4", {"link_type": 4}), ("link-name", {"link_type": "XX"}),
        ("link-bool", {"link_type": True}), ("byte-order-text", {"byte_order_big": "yes"}),
        ("byte-order-2", {"byte_order_big": 2}), ("nonnormal", {"nonnormal_float": "nan"}),
        ("first-error", {"rack": 9, "slot": 40}),
    ]
    return ([plan_case(f"ok-{cid}", f"Accepted params {p}.", tags, **p) for cid, p in ok]
            + [plan_case(f"bad-{cid}", f"Refused params {p}.", tags, **p) for cid, p in bad])


def s7_random() -> list[Case]:
    rng = random.Random(44)
    cases: list[Case] = []
    forms = [("DB{db},D{o}", "float32"), ("DB{db},W{o}", "uint16"), ("DB{db},B{o}", "uint16"), ("DB{db},REAL{o}", "float32"),
             ("DB{db},X{o}.{b}", "bool"), ("M{a}{o}", None), ("I{a}{o}", None), ("DB{db},STRING{o}.{n}", "string"),
             ("DB{db},LREAL{o}", "float64"), ("DB{db},DI{o}", "int32"), ("T{o}", "uint32"), ("C{o}", "uint16")]
    for n in range(30):
        tags = []
        for i in range(rng.randint(4, 40)):
            fmt, dtype = rng.choice(forms)
            o = rng.randrange(0, 600)
            if fmt.startswith(("M{a}", "I{a}")):
                code, dt = rng.choice([("W", "uint16"), ("D", "float32"), ("B", "uint16")])
                addr, dtype = fmt.format(a=code, o=o), dt
            else:
                addr = fmt.format(db=rng.choice([1, 2, 100]), o=o, b=rng.randrange(0, 8), n=rng.choice([4, 10, 20]))
            tags.append(T(f"t{i}", addr, dtype))
            if rng.random() < 0.08:
                tags.append(T(f"dup{i}", addr, dtype))
        params = {"max_gap": rng.choice([0, 4, 16, 100]), "max_pdu": rng.choice([240, 480, 960])}
        cases.append(plan_case(f"mix-{n:02d}", f"Seeded random mix {n}: {len(tags)} tags.", tags, **params))
    return cases


# --- s7/decode ------------------------------------------------------------------------------------

def s7_numbers() -> list[Case]:
    cases: list[Case] = []
    specs = [
        ("float32", "DB1,REAL0", ">f", 12.5), ("float32-neg", "DB1,D0", ">f", -0.15625),
        ("float64", "DB1,LREAL0", ">d", 1234.5678), ("int16-neg", "MW0", ">h", -2), ("uint16-max", "MW0", ">H", 65535),
        ("int32-neg", "MD0", ">i", -123456), ("uint32-max", "MD0", ">I", 0xDEADBEEF), ("int64-neg", "DB1,LINT0", ">q", -(2 ** 40) - 5),
        ("uint64-max", "DB1,LWORD0", ">Q", 2 ** 64 - 1), ("int8-neg", "MB0", ">b", -3), ("uint8", "MB0", ">B", 200),
    ]
    dtypes = {"float32": "float32", "float32-neg": "float32", "float64": "float64", "int16-neg": "int16", "uint16-max": "uint16",
              "int32-neg": "int32", "uint32-max": "uint32", "int64-neg": "int64", "uint64-max": "uint64", "int8-neg": "int16",
              "uint8": "uint16"}
    for slug, address, fmt, value in specs:
        raw = struct.pack(fmt, value)
        cases.append(decode_case(f"big-{slug}", f"{address} = {value!r}, big endian (S7 native).",
                                 [T("a", address, dtypes[slug])], raw))
        cases.append(decode_case(f"little-{slug}", f"{address} = {value!r} with byte_order_big off: the bytes are reversed.",
                                 [T("a", address, dtypes[slug])], raw[::-1], byte_order_big=False))
    for slug, raw in (("nan", 0x7FC00000), ("inf", 0x7F800000), ("neg-inf", 0xFF800000), ("denormal", 1), ("neg-denormal", 0x80000001)):
        data = raw.to_bytes(4, "big")
        cases.append(decode_case(f"nonnormal-zero-{slug}", f"REAL {slug}: replaced with zero.", [T("a", "MD0", "float32")], data))
        cases.append(decode_case(f"nonnormal-unmodified-{slug}", f"REAL {slug}, nonnormal_float unmodified.",
                                 [T("a", "MD0", "float32")], data, nonnormal_float="unmodified"))
    for slug, raw in (("nan", 0x7FF8000000000000), ("inf", 0x7FF0000000000000), ("denormal", 1)):
        data = raw.to_bytes(8, "big")
        cases.append(decode_case(f"nonnormal-lreal-{slug}", f"LREAL {slug}: replaced with zero.", [T("a", "DB1,LREAL0", "float64")], data))
        cases.append(decode_case(f"nonnormal-lreal-unmodified-{slug}", f"LREAL {slug} unmodified.",
                                 [T("a", "DB1,LREAL0", "float64")], data, nonnormal_float="unmodified"))
    return cases


def s7_bits_strings_arrays() -> list[Case]:
    return [
        decode_case("bit-set", "Bit 3 of a byte.", [T("a", "M0.3", "bool")], bytes([0b1000])),
        decode_case("bit-clear", "Bit 3 clear.", [T("a", "M0.3", "bool")], bytes([0b0111])),
        decode_case("bits-of-one-byte", "Several bits of one byte.", [T("a", "M0.0", "bool"), T("b", "M0.7", "bool"), T("c", "M0.1", "bool")], bytes([0x81])),
        decode_case("bit-of-word-big", "Bit 15 of a big-endian word is in the first byte.", [T("a", "MW0.15", "bool")], bytes([0x80, 0x00])),
        decode_case("bit-of-word-little", "Bit 15 of a little-endian word follows the byte order.",
                    [T("a", "MW0.15", "bool")], bytes([0x00, 0x80]), byte_order_big=False),
        decode_case("bit-of-dword", "Bit 31 of a dword.", [T("a", "MD0.31", "bool")], bytes([0x80, 0, 0, 0])),
        decode_case("bit-of-lword", "Bit 63 of an lword.", [T("a", "DB1,LWORD0.63", "bool")], bytes([0x80]) + bytes(7)),
        decode_case("string-s7", "An S7 STRING: max length, actual length, characters.", [T("a", "DB1,STRING0.10", "string")],
                    bytes([10, 3]) + b"abc" + bytes(7)),
        decode_case("string-actual-above-max", "An actual length above the max is clipped.", [T("a", "DB1,STRING0.4", "string")],
                    bytes([4, 9]) + b"abcd"),
        decode_case("string-nul", "A NUL ends the text.", [T("a", "DB1,STRING0.6", "string")], bytes([6, 5]) + b"ab\x00cd\x00"),
        decode_case("string-latin1", "Latin-1 characters.", [T("a", "DB1,STRING0.2", "string")], bytes([2, 2, 0xE9, 0xFC])),
        decode_case("string-raw", "A raw string ends at the first NUL.", [T("a", "DB1,B0.8", "string")], b"PUMP\x00\x00\x00\x00"),
        decode_case("string-raw-full", "A raw string using every byte.", [T("a", "DB1,B0.4", "string")], b"ABCD"),
        decode_case("array-1d", "A one dimensional array.", [T("a", "DB1,W0[3]", "int16")], bytes([0, 1, 0xFF, 0xFE, 0, 3])),
        decode_case("array-2d", "A two dimensional array, row by row.", [T("a", "MD0[2][2]", "float32")],
                    struct.pack(">4f", 1.5, -2.0, 3.0, 0.25)),
        decode_case("array-little", "An array with byte_order_big off.", [T("a", "DB1,W0[2]", "uint16")], bytes([1, 0, 2, 0]), byte_order_big=False),
    ]


def s7_timers_counters() -> list[Case]:
    cases: list[Case] = []
    for word in (0x0000, 0x0001, 0x0999, 0x1123, 0x2050, 0x3999, 0x3001):
        cases.append(decode_case(f"timer-{word:04x}", f"S5TIME 0x{word:04X} in milliseconds.", [T("a", "T0", "uint32")], word.to_bytes(2, "big")))
    for word in (0x00A0, 0x0F00, 0x01FA):
        cases.append(decode_case(f"timer-bad-bcd-{word:04x}", f"S5TIME 0x{word:04X} has a non-BCD digit.", [T("a", "T0", "uint32")], word.to_bytes(2, "big")))
    for word in (0x0000, 0x0007, 0x0123, 0x0999):
        cases.append(decode_case(f"counter-{word:04x}", f"Counter BCD 0x{word:04X}.", [T("a", "C0", "uint16")], word.to_bytes(2, "big")))
    for word in (0x00A0, 0x1000 | 0x000F):
        cases.append(decode_case(f"counter-bad-bcd-{word:04x}", f"Counter 0x{word:04X} is not BCD.", [T("a", "C0", "uint16")], word.to_bytes(2, "big")))
    cases.append(decode_case("timer-ignores-byte-order", "Timers are not byte-swapped by byte_order_big.", [T("a", "T0", "uint32")],
                             (0x1123).to_bytes(2, "big"), byte_order_big=False))
    cases.append(decode_case("timers-two", "Two timers and a gap.", [T("a", "T0", "uint32"), T("b", "T2", "uint32")],
                             bytes.fromhex("0123") + bytes(2) + bytes.fromhex("2999")))
    return cases


def s7_block_items() -> list[Case]:
    d = lambda n, a, t="uint16": T(n, a, t)  # noqa: E731
    return [
        decode_case("offsets", "Items at their offsets inside a merged block.",
                    [d("a", "DB1,W0"), d("b", "DB1,D4", "uint32"), d("c", "DB1,W10")], bytes.fromhex("0001ffff0000002a0000ffff0007")),
        decode_case("aliases", "Two names, one address.", [d("a", "DB1,W0"), d("b", "DB1,W0"), d("c", "DB1,X0.0", "bool")], bytes([0, 5])),
        decode_case("short-reply", "A reply shorter than the block fails only the items it cuts.",
                    [d("a", "DB1,W0"), d("b", "DB1,D2", "uint32"), d("c", "DB1,W8")], bytes([0, 1, 0, 2])),
        decode_case("empty-reply", "No bytes: every item fails.", [d("a", "DB1,W0"), d("b", "DB1,W2")], b""),
        decode_case("long-reply", "Extra bytes are ignored.", [d("a", "DB1,W0")], bytes([0, 9, 1, 2, 3])),
    ]


def s7_decode_random() -> list[Case]:
    rng = random.Random(45)
    cases: list[Case] = []
    for n in range(20):
        tags, off = [], 0
        for i in range(rng.randint(2, 8)):
            fmt, dtype, size = rng.choice([("DB1,W{o}", "int16", 2), ("DB1,D{o}", "uint32", 4), ("DB1,REAL{o}", "float32", 4),
                                           ("DB1,B{o}", "uint16", 1), ("DB1,LREAL{o}", "float64", 8), ("DB1,X{o}.{b}", "bool", 1)])
            tags.append(T(f"t{i}", fmt.format(o=off, b=rng.randrange(0, 8)), dtype))
            off += size + rng.randrange(0, 3)
        data = bytes(rng.randrange(0, 256) for _ in range(off + 4))
        params = {"byte_order_big": rng.random() < 0.5, "nonnormal_float": rng.choice(["zero", "unmodified"])}
        cases.append(decode_case(f"mix-{n:02d}", f"Seeded random mix {n}: {len(tags)} tags, random bytes.", tags, data, **params))
    return cases


# --- s7/frame and s7/reply ----------------------------------------------------------------------------

def frame_case(cid: str, desc: str, tags: list[dict[str, Any]], pdu: int | None = None, **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags, "pdu": pdu}


def s7_requests() -> list[Case]:
    return [
        frame_case("db1-real", "One read var of 4 bytes at DB1.", [T("a", "DB1,REAL0", "float32")]),
        frame_case("every-area", "Read var per area: I, Q, M, DB, T, C (transport sizes and address units).",
                   [T("i", "IW0", "uint16"), T("q", "QW0", "uint16"), T("m", "MW0", "uint16"), T("d", "DB5,W0", "uint16"),
                    T("t", "T3", "uint32"), T("c", "C4", "uint16")]),
        frame_case("big-offsets", "A high byte offset needs the third address byte.", [T("a", "DB300,W65000", "uint16")]),
        frame_case("link-pg-slot-2", "TSAP of a PG connection to rack 0 slot 2.", [T("a", "MW0", "uint16")], link_type="PG", slot=2),
        frame_case("link-op-rack-1", "TSAP of an OP connection to rack 1 slot 3.", [T("a", "MW0", "uint16")], link_type=2, rack=1, slot=3),
        frame_case("pdu-960", "Setup communication asks for the configured PDU.", [T("a", "MW0", "uint16")], max_pdu=960),
        frame_case("split-480", "An 800-byte block planned for PDU 960 is read in two requests when the CPU negotiates 480.",
                   [T("a", "DB300,REAL0[200]", "float32")], pdu=480, max_pdu=960),
        frame_case("split-240", "The same block at PDU 240: four requests.", [T("a", "DB300,REAL0[200]", "float32")], pdu=240, max_pdu=960),
        frame_case("no-split", "The block fits the negotiated PDU: one request.", [T("a", "DB300,REAL0[200]", "float32")], pdu=960, max_pdu=960),
        frame_case("timers-split", "Timers split by element: 2 bytes each.", [T(f"t{i}", f"T{i}", "uint32") for i in range(0, 230)], pdu=240, max_pdu=480),
        frame_case("two-blocks", "Two blocks, two requests, one reference each in order.",
                   [T("a", "DB1,W0", "uint16"), T("b", "DB2,W0", "uint16")]),
        frame_case("db-high", "DB 65535.", [T("a", "DB65535,W0", "uint16")]),
        frame_case("rack-7-slot-31", "Highest rack and slot.", [T("a", "MW0", "uint16")], rack=7, slot=31),
        frame_case("no-tags", "No tag, no read request; setup, connection request and SZL requests are still built.", []),
        frame_case("byte-array", "A byte array.", [T("a", "MB10[100]", "uint16")]),
        frame_case("pdu-240", "A PDU of 240.", [T("a", "MW0", "uint16")], max_pdu=240),
        frame_case("big-array-960", "A block of 942 bytes at PDU 960.", [T("a", "DB1,B0[942]", "uint16")], max_pdu=960),
        frame_case("link-basic", "A basic (PC) connection.", [T("a", "MW0", "uint16")], link_type="basic"),
        frame_case("slot-0", "Slot 0.", [T("a", "MW0", "uint16")], slot=0),
        frame_case("many-dbs", "Many DBs.", [T(f"t{i}", f"DB{i + 1},W0", "uint16") for i in range(8)]),
    ]


def reply_case(cid: str, desc: str, pdu_hex: str, word_len: int, count: int) -> Case:
    return cid, desc, {"pdu": pdu_hex, "word_len": word_len, "count": count}


def _ack(seq: int, params: bytes, data: bytes, error: int = 0, pdu_type: int = 3) -> str:
    hdr = bytes([0x32, pdu_type, 0, 0]) + seq.to_bytes(2, "big") + len(params).to_bytes(2, "big") + len(data).to_bytes(2, "big")
    return (hdr + error.to_bytes(2, "big") + params + data).hex()


def _item(rc: int, tsize: int, length: int, data: bytes) -> bytes:
    return bytes([rc, tsize]) + length.to_bytes(2, "big") + data


def s7_replies() -> list[Case]:
    p1 = bytes([0x04, 0x01])
    ok2 = _item(0xFF, 0x04, 16, b"\x12\x34")
    cases = [
        reply_case("ok-byte-bits", "Two bytes, transport size 04 (byte), length in bits.", _ack(1, p1, ok2), 2, 2),
        reply_case("ok-octet", "Two bytes, transport size 09 (octet string), length in bytes.", _ack(1, p1, _item(0xFF, 0x09, 2, b"\x12\x34")), 2, 2),
        reply_case("ok-size-03", "Transport size 03, length in bytes.", _ack(1, p1, _item(0xFF, 0x03, 2, b"\x12\x34")), 2, 2),
        reply_case("ok-timer", "Two timers (4 bytes), octet string.", _ack(1, p1, _item(0xFF, 0x09, 4, b"\x01\x23\x02\x99")), 0x1D, 2),
        reply_case("ok-counter", "A counter.", _ack(1, p1, _item(0xFF, 0x09, 2, b"\x01\x23")), 0x1C, 1),
        reply_case("size-mismatch", "One byte for two.", _ack(1, p1, _item(0xFF, 0x04, 8, b"\x12")), 2, 2),
        reply_case("declared-length-wrong", "Declared length does not match the requested size.", _ack(1, p1, _item(0xFF, 0x04, 24, b"\x12\x34")), 2, 2),
        reply_case("bad-transport-size", "Transport size 0x01 is not a data transport size.", _ack(1, p1, _item(0xFF, 0x01, 2, b"\x12\x34")), 2, 2),
        reply_case("truncated-item", "The item claims more bytes than the data carries.", _ack(1, p1, _item(0xFF, 0x04, 32, b"\x12\x34")), 2, 2),
        reply_case("trailing-bytes", "Bytes after the item.", _ack(1, p1, ok2 + b"\x00"), 2, 2),
        reply_case("wrong-function", "The reply acknowledges another function.", _ack(1, bytes([0x05, 0x01]), ok2), 2, 2),
        reply_case("wrong-item-count", "Two items for one asked.", _ack(1, bytes([0x04, 0x02]), ok2), 2, 2),
        reply_case("no-data", "A reply without a data section.", _ack(1, p1, b""), 2, 2),
        reply_case("short-pdu", "A PDU shorter than a header.", "32030000", 2, 2),
        reply_case("short-ack-header", "An ACK_DATA header cut after 10 bytes.", "3203000000010002000600", 2, 2),
        reply_case("bad-protocol-id", "Protocol id 0x33.", "33" + _ack(1, p1, ok2)[2:], 2, 2),
        reply_case("job-not-reply", "A job PDU (type 01) is not a response.", _ack(1, p1, ok2, pdu_type=1), 2, 2),
        reply_case("params-beyond-pdu", "Parameter length beyond the PDU.", "32030000000100ff00000000" + "0401", 2, 2),
        reply_case("data-beyond-pdu", "Data length beyond the PDU.", "320300000001000200ff0000" + "0401", 2, 2),
    ]
    for rc in (0x01, 0x03, 0x05, 0x06, 0x07, 0x0A, 0x04, 0x00):
        cases.append(reply_case(f"item-error-{rc:02x}", f"Item return code 0x{rc:02X}.", _ack(1, p1, _item(rc, 0x00, 0, b"")), 2, 2))
    for err in (0x8104, 0x8500, 0x8001, 0xD201, 0x0000 | 0x8701, 0x1234):
        cases.append(reply_case(f"header-error-{err:04x}", f"Header error {err:#06x}.", _ack(1, p1, ok2, error=err), 2, 2))
    return cases


SPECS: dict[str, list[FileSpec]] = {
    "s7/plan": [
        ("addresses", "Every address form of the Python parser tests (D-049).", s7_addresses()),
        ("rejected", "Rejected addresses go to invalid with a reason (D-049).", s7_rejected()),
        ("blocks", "Block merging per area and DB, max_gap, the PDU span, timers and counters (D-051).", s7_blocks()),
        ("params", "Driver params: refused values and accepted forms.", s7_params()),
        ("random_mix", "Seeded random mixes of areas, types and limits.", s7_random()),
    ],
    "s7/decode": [
        ("numbers", "Integers and floats, big and little endian, non-normal floats (D-049).", s7_numbers()),
        ("bits_strings_arrays", "Bits, strings and arrays.", s7_bits_strings_arrays()),
        ("timers_counters", "S5TIME timers and BCD counters.", s7_timers_counters()),
        ("block_items", "Items at offsets inside a block; aliases; short, empty and long replies.", s7_block_items()),
        ("random_mix", "Seeded random bytes decoded as random tags.", s7_decode_random()),
    ],
    "s7/frame": [
        ("requests", "The frames python-snap7 builds: connection request, setup communication, read var per block and PDU split, SZL reads (D-050, D-051).",
         s7_requests()),
    ],
    "s7/reply": [
        ("replies", "Read var replies: data, item return codes, header errors and malformed replies.", s7_replies()),
    ],
}
