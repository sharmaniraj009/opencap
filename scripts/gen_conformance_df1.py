"""Allen-Bradley DF1 conformance vector inputs: link frames, the receiver, typed-read requests and reply
values (#68, ADR-0014). The directed cases are the ones `tests/unit/test_df1_frame.py` checks.

Case data only: `scripts/gen_conformance.py` imports `SPECS` from here, computes every `expected` with
`tests/conformance/reference.py` and writes the files. Run that script, not this one. References:
docs/decisions.md D-037 (DF1 driver), docs/driver_params.md (ab_df1).
"""

from __future__ import annotations

import random
import struct
from typing import Any

from opencap.drivers.ab_df1 import encode_frame

Case = tuple[str, str, dict[str, Any]]          # (id, description, input)
FileSpec = tuple[str, str, list[Case]]          # (file stem, file description, cases)

READ_N7 = "01 00 0f 00 34 12 a2 14 07 89 00 00"        # typed read of N7:0{10} from station 1, TNS 0x1234
DLE_HEAVY = "01 00 0f 00 10 10 a2 04 10 8a 10 00"      # DLE everywhere: TNS 0x1010, file 16 (F), element 16


def hx(data: bytes) -> str:
    return data.hex(" ")


def frame(check: str, app: str) -> str:
    return hx(encode_frame(bytes.fromhex(app), check))


def T(name: str, address: str, dtype: str) -> dict[str, Any]:
    return {"name": name, "address": address, "dtype": dtype}


# --- ab_df1/frame ---------------------------------------------------------------------------------

def frames() -> list[Case]:
    def f(cid: str, desc: str, check: str, app: str) -> Case:
        return cid, desc, {"check": check, "app": app}
    rng = random.Random(6801)
    out = [
        f("read-n7-crc", "A typed read, CRC-16 (low byte first).", "crc", READ_N7),
        f("read-n7-bcc", "The same, BCC (two's complement of the byte sum).", "bcc", READ_N7),
        f("dle-heavy-crc", "Every 0x10 in the application bytes is doubled; the check is over the unstuffed bytes.",
          "crc", DLE_HEAVY),
        f("dle-heavy-bcc", "The same with BCC.", "bcc", DLE_HEAVY),
        f("check-byte-is-dle", "A BCC that is 0x10 is sent once, not stuffed.", "bcc", "01 00 06 00 e9 00"),
        f("crc-covers-etx", "CRC-16 runs over the data and the ETX byte.", "crc", "01 00 06 00 00 00"),
        f("min-header", "A six-byte header only (diagnostic-size message).", "crc", "05 00 06 00 ff ff"),
        f("all-zero", "All-zero application bytes.", "bcc", "00 00 00 00 00 00"),
        f("all-ff", "All-0xFF application bytes.", "crc", "ff ff ff ff ff ff ff ff"),
        f("symbols-as-data", "Data bytes equal to STX, ETX, ACK, NAK and ENQ are not special.", "crc",
          "02 03 06 15 05 10 02 10 03"),
        f("unknown-check", "An unknown error-check mode is refused.", "lrc", READ_N7),
    ]
    for k in range(10):
        n = rng.randrange(6, 90)
        app = bytes(rng.choice((0x10, 0x10, 0x02, 0x03, 0x06, 0x15, 0x05, rng.randrange(256))) for _ in range(n))
        out.append(f(f"random-{k:02d}", "Seeded random application bytes, many DLEs.", rng.choice(("crc", "bcc")), hx(app)))
    return out


# --- ab_df1/receive -------------------------------------------------------------------------------

def receive() -> list[Case]:
    def r(cid: str, desc: str, check: str, *chunks: str) -> Case:
        return cid, desc, {"check": check, "chunks": list(chunks)}
    crc_n7 = frame("crc", READ_N7)
    bcc_n7 = frame("bcc", READ_N7)
    crc_bytes = bytes.fromhex(crc_n7)
    bad_crc = bytearray(crc_bytes)
    bad_crc[-1] ^= 0xFF
    bad_bcc = bytearray(bytes.fromhex(bcc_n7))
    bad_bcc[5] ^= 0x01
    embedded = crc_bytes[:7] + b"\x10\x06" + crc_bytes[7:]
    rng = random.Random(6802)
    out = [
        r("one-frame", "A good frame gives one frame event with the unstuffed application bytes.", "crc", crc_n7),
        r("dle-heavy", "Doubled DLEs are undone.", "bcc", frame("bcc", DLE_HEAVY)),
        r("symbols", "DLE ACK, DLE NAK and DLE ENQ outside a frame.", "crc", "10 06 10 15 10 05"),
        r("embedded-ack", "A DLE ACK inside a frame is reported and the frame goes on.", "crc", hx(embedded)),
        r("noise-before", "Bytes outside a frame are skipped and counted as noise.", "crc",
          hx(b"\x00\xff garbage \x03") + " " + crc_n7),
        r("bad-crc", "A CRC mismatch is a bad event with both values.", "crc", hx(bytes(bad_crc))),
        r("bad-bcc", "A changed data byte fails the BCC.", "bcc", hx(bytes(bad_bcc))),
        r("wrong-check-mode", "A BCC frame read as CRC never passes.", "crc", bcc_n7 + " 00"),
        r("stx-restarts", "DLE STX inside a frame restarts it; the partial one is bad.", "bcc",
          "10 02 01 02 03 " + bcc_n7),
        r("invalid-dle-pair", "Any other DLE pair inside a frame ends it as bad.", "bcc", "10 02 01 02 10 41"),
        r("too-short", "A frame shorter than the 6-byte header is bad.", "bcc", frame("bcc", "01 00 06")),
        r("too-long", "A frame longer than the maximum is bad, then the receiver recovers.", "bcc",
          "10 02 " + hx(b"\x00" * 600), bcc_n7),
        r("split-one-byte-at-a-time", "A frame fed one byte at a time.", "crc",
          *[f"{b:02x}" for b in bytes.fromhex(frame("crc", DLE_HEAVY))]),
        r("split-inside-dle-pair", "A chunk boundary between a DLE and its pair.", "crc",
          crc_n7[:5], crc_n7[5:]),
        r("two-frames-one-chunk", "Two frames back to back in one chunk.", "crc", crc_n7 + " " + frame("crc", DLE_HEAVY)),
        r("dle-dle-outside", "DLE DLE outside a frame is noise and keeps the DLE state.", "crc", "10 10 06"),
        r("unknown-check", "An unknown error-check mode is refused.", "lrc", crc_n7),
    ]
    for k in range(6):
        n = rng.randrange(6, 60)
        app = bytes(rng.choice((0x10, 0x02, 0x03, 0x06, rng.randrange(256))) for _ in range(n))
        check = rng.choice(("crc", "bcc"))
        data = encode_frame(app, check)
        cuts = sorted(rng.sample(range(1, len(data)), min(3, len(data) - 1)))
        chunks = [hx(data[a:b]) for a, b in zip([0, *cuts], [*cuts, len(data)])]
        out.append(r(f"random-split-{k:02d}", "A seeded random frame in random chunks.", check, *chunks))
    return out


# --- ab_df1/request -------------------------------------------------------------------------------

PLAN_TAGS = [
    T("F", "F34:3", "float32"), T("F2", "F34:5", "float32"), T("N", "N54:17", "uint16"), T("NF", "N66:10", "float32"),
    T("BB", "B3:17/14", "bool"), T("BN", "B3/0", "bool"), T("IO", "I:0/0", "bool"), T("IO2", "I:1.2", "int16"),
    T("OUT", "O:0/3", "bool"), T("ST", "ST56:10", "string"), T("TP", "T47:0.PRE", "uint16"),
    T("TA", "T4:1.ACC", "uint16"), T("TD", "T4:1.DN", "bool"), T("CC", "C5:2.CU", "bool"), T("L", "L9:4", "int32"),
    T("S", "S:5/0", "bool"),
]


def requests() -> list[Case]:
    def q(cid: str, desc: str, tags: list[dict[str, Any]], **params: Any) -> Case:
        return cid, desc, {"params": params, "tags": tags}
    out = [
        q("every-file-type", "One typed read (FNC A2) per planned block for every supported file type.", PLAN_TAGS),
        q("two-field-variant", "read_fnc a1: FNC A1 without the sub-element field where it is 0.", PLAN_TAGS,
          read_fnc="a1"),
        q("large-file-number", "File number 255 needs the extended address field (FF + UINT); the planner refuses "
          "file numbers above 255.",
          [T("A", "N255:0", "int16"), T("B", "N300:2", "int16")]),
        q("large-element", "The planner refuses element numbers above 255.", [T("A", "N7:300", "int16")]),
        q("merged-block", "Neighbouring elements of one file merge into one read.",
          [T("A", "N7:0", "int16"), T("B", "N7:1", "int16"), T("C", "N7:3", "int16")]),
        q("request-size-limit", "request_size caps the bytes per read: the block is split.",
          [T(f"N{i}", f"N7:{i}", "int16") for i in range(0, 60, 3)], request_size=40),
        q("floats", "F files: 4-byte elements.", [T("A", "F8:0", "float32"), T("B", "F8:1", "float32")]),
        q("strings", "ST files: 84-byte elements.", [T("A", "ST9:0", "string")]),
        q("timer-sub-elements", "T/C sub-elements read the whole 6-byte element.",
          [T("A", "T4:0.ACC", "uint16"), T("B", "T4:0.DN", "bool"), T("C", "C5:0.PRE", "uint16")]),
        q("io", "Input and output words address slot and word.", [T("A", "I:2.1", "int16"), T("B", "O:3/7", "bool")]),
        q("status", "The S file is file 2.", [T("A", "S:4", "int16"), T("B", "S:1/5", "bool")]),
        q("invalid-addresses", "Addresses the planner rejects go to invalid.",
          [T("A", "N7:0", "int16"), T("B", "X9:0", "int16"), T("C", "N7:0/16", "bool")]),
        q("bad-read-fnc", "An unknown read_fnc is refused.", [T("A", "N7:0", "int16")], read_fnc="a9"),
        q("bad-error-check", "An unknown error_check is refused.", [T("A", "N7:0", "int16")], error_check="lrc"),
    ]
    rng = random.Random(6803)
    for k in range(6):
        tags = []
        for i in range(rng.randrange(2, 12)):
            kind = rng.choice(["N", "F", "B", "L", "T", "C"])
            fn = {"N": 7, "F": 8, "B": 3, "L": 9, "T": 4, "C": 5}[kind] + rng.choice([0, 10, 200])
            el = rng.randrange(0, 40)
            if kind in ("T", "C"):
                tags.append(T(f"T{i}", f"{kind}{fn}:{el}.{rng.choice(['ACC', 'PRE', 'DN'])}", "uint16"))
            elif kind == "B":
                tags.append(T(f"T{i}", f"B{fn}:{el}/{rng.randrange(16)}", "bool"))
            else:
                tags.append(T(f"T{i}", f"{kind}{fn}:{el}", {"N": "int16", "F": "float32", "L": "int32"}[kind]))
        out.append(q(f"random-{k:02d}", "Seeded random mix of file types, numbers and elements.", tags))
    return out


# --- ab_df1/values --------------------------------------------------------------------------------

def values() -> list[Case]:
    def v(cid: str, desc: str, tags: list[dict[str, Any]], reply: bytes, **params: Any) -> Case:
        return cid, desc, {"params": params, "tags": tags, "reply": hx(reply)}

    def st(text: str) -> bytes:
        body = bytearray(text.encode("latin-1").ljust(82, b"\x00"))
        body[0::2], body[1::2] = body[1::2], body[0::2]
        return struct.pack("<H", len(text)) + bytes(body)

    rng = random.Random(6804)
    out = [
        v("int-word", "An N word is signed 16-bit, little-endian.", [T("A", "N7:0", "int16")], struct.pack("<h", -2)),
        v("uint-from-signed", "A uint16 tag takes the same word unsigned.", [T("A", "N7:0", "uint16")],
          struct.pack("<h", -2)),
        v("float", "F elements are float32.", [T("A", "F8:0", "float32")], struct.pack("<f", 12.5)),
        v("float-list", "Several elements come back as a list.", [T("A", "F8:0", "float32"), T("B", "F8:2", "float32")],
          struct.pack("<fff", 1.5, 0.0, -2.25)),
        v("long", "L elements are signed 32-bit.", [T("A", "L9:0", "int32")], struct.pack("<i", -70000)),
        v("bit", "A bit of a B word.", [T("A", "B3:0/14", "bool")], struct.pack("<H", 1 << 14)),
        v("bit-clear", "The other bits do not leak.", [T("A", "B3:0/14", "bool")], struct.pack("<H", 0xBFFF)),
        v("string", "ST: length word, then the text with each byte pair swapped.", [T("A", "ST9:0", "string")],
          st("Pump 1 ok")),
        v("string-odd-length", "An odd-length string keeps its last byte.", [T("A", "ST9:0", "string")], st("abc")),
        v("timer-acc", "T .ACC is word 2 of the element.", [T("A", "T4:0.ACC", "uint16")], struct.pack("<hhh", 0, 500, 1234)),
        v("timer-dn", "T .DN is bit 13 of word 0.", [T("A", "T4:0.DN", "bool")], struct.pack("<Hhh", 1 << 13, 0, 0)),
        v("counter-cu", "C .CU is bit 15 of word 0.", [T("A", "C5:0.CU", "bool")], struct.pack("<Hhh", 1 << 15, 0, 0)),
        v("short-reply", "A short reply decodes what is there; the rest is a decode error.",
          [T("A", "F8:0", "float32"), T("B", "F8:1", "float32"), T("C", "F8:2", "float32")], struct.pack("<ff", 1.5, 2.5)),
        v("empty-reply", "An empty reply gives no value.", [T("A", "N7:0", "int16")], b""),
        v("timer-short", "A T/C reply shorter than 6 bytes gives no value.", [T("A", "T4:0.ACC", "uint16")], b"\x00\x00"),
        v("io-word", "An input word.", [T("A", "I:1.2", "int16")], struct.pack("<h", 300)),
    ]
    for k in range(6):
        count = rng.randrange(2, 6)
        tags = [T(f"N{i}", f"N7:{i}", rng.choice(["int16", "uint16"])) for i in range(count)]
        reply = bytes(rng.randrange(256) for _ in range(2 * count))
        out.append(v(f"random-words-{k:02d}", "Seeded random words for a run of N elements.", tags, reply))
    return out


SPECS: dict[str, list[FileSpec]] = {
    "ab_df1/frame": [("frames", "DF1 full-duplex frames: DLE stuffing, BCC and CRC-16 (#68).", frames())],
    "ab_df1/receive": [("receiver", "The DF1 receiver: frames, response symbols, noise, bad and split frames (#68).",
                        receive())],
    "ab_df1/request": [("typed_reads", "PCCC protected typed logical reads (FNC A2/A1) for planned blocks (#68).",
                        requests())],
    "ab_df1/values": [("replies", "Reply data bytes to pycomm3-shaped values and decoded tags (#68).", values())],
}
