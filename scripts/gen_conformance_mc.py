"""MELSEC MC conformance vector inputs (planning, decoding, request frames and reply checks, ADR-0014).

Case data only: `scripts/gen_conformance.py` imports `SPECS` from here, computes every
`expected` with `tests/conformance/reference.py` and writes the files. Run that script, not
this one. References: docs/decisions.md D-053 to D-057.
"""

from __future__ import annotations

import random
import struct
from typing import Any

from gen_conformance_ab import Case, FileSpec, T, plan_case


def words_le(fmt: str, value: Any) -> list[int]:
    """A value as the PLC stores it: little-endian words, the lowest device number first."""
    raw = struct.pack("<" + fmt, value)
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def decode_case(cid: str, desc: str, tags: list[dict[str, Any]], words: list[int], **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags, "reply": {"words": words}}


# --- melsec_mc/plan ------------------------------------------------------------------------------

def mc_addresses() -> list[Case]:
    return [
        plan_case("word-device", "The KEPServer sample: a float32 on D0000002 (two words).",
                  [T("FLOW_1", "D0000002")]),
        plan_case("dtype-widths", "1, 2 and 4 words by dtype.",
                  [T("a", "D0", "uint16"), T("b", "D1", "int16"), T("c", "D2", "int32"), T("d", "D4", "uint32"),
                   T("e", "D6", "float32"), T("f", "D8", "int64"), T("g", "D12", "uint64"), T("h", "D16", "float64")]),
        plan_case("hex-bit-devices", "X, Y, B, SB, DX, DY numbers are hexadecimal; bits sit 16 to a word.",
                  [T("x", "X1A", "bool"), T("y", "Y7", "bool"), T("b", "B001F", "bool"), T("sb", "SB10", "bool"),
                   T("dx", "DX3", "bool"), T("dy", "DY0", "bool")]),
        plan_case("decimal-bit-devices", "M, SM, L, F, V, TS, TC, SS, SC, CS, CC numbers are decimal.",
                  [T(n.lower(), f"{n}33", "bool") for n in ("M", "SM", "L", "F", "V", "TS", "TC", "SS", "SC", "CS", "CC")]),
        plan_case("hex-word-devices", "W, SW and ZR numbers are hexadecimal, D, SD, R, TN, SN and CN decimal.",
                  [T("w", "W1F", "uint16"), T("sw", "SW10", "uint16"), T("zr", "ZR10", "uint16"),
                   T("d", "D10", "uint16"), T("sd", "SD100", "int16"), T("r", "R100", "float32"),
                   T("tn", "TN5", "uint16"), T("sn", "SN5", "uint16"), T("cn", "CN5", "int16")]),
        plan_case("case-and-blanks", "Lower case and blanks are accepted.", [T("a", "  d0000002 ", "float32")]),
        plan_case("bit-in-word", "A bit of a word device: .00 to .15, .16 to .31 of the two-word value.",
                  [T("a", "D100.05", "bool"), T("b", "D100.5", "bool"), T("c", "D100.17", "bool"),
                   T("d", "W1F.15", "bool"), T("e", "R0.31", "bool")]),
        plan_case("bit-device-as-words", "16 or 32 points of a bit device read as words, on a 16-point boundary.",
                  [T("a", "M16", "uint16"), T("b", "X0010", "int32"), T("c", "B0", "uint32")]),
        plan_case("arrays", "One and two dimensional arrays on word devices.",
                  [T("a", "D100[4]", "int16"), T("b", "D200 [2][3]", "float32")]),
        plan_case("strings", "DSH (first char in the high byte) and DSL strings, '.' or ':' before the length.",
                  [T("a", "DSH100.020", "string"), T("b", "DSL200:006", "string"), T("c", "WSH1F.004", "string"),
                   T("d", "RSL0.002", "string"), T("e", "ZRSH10.128", "string")]),
        plan_case("last-points", "The last device number of Kepware's Q Series ranges.",
                  [T("a", "D4184063", "uint16"), T("b", "R32767", "uint16"), T("c", "M59999", "bool"),
                   T("d", "X3FFF", "bool"), T("e", "BEA60", "bool"), T("f", "W3FD7FF", "uint16")]),
    ]


def mc_rejected() -> list[Case]:
    bad = [
        ("empty", "", "uint16", "Empty address."),
        ("bool-on-word-device", "D100", "bool", "A bool on a word device needs .bit."),
        ("bit-32", "D100.32", "bool", "Bit 32 is out of range."),
        ("bit-with-uint16", "D100.5", "uint16", "A .bit address must be bool."),
        ("bit-on-bit-device", "M10.1", "bool", "A .bit suffix on a bit device."),
        ("bit-device-float", "M16", "float32", "A bit device read as words: 16/32-bit integers only."),
        ("bit-device-boundary", "M17", "uint16", "Word access to a bit device off a 16-point boundary."),
        ("bit-device-boundary-x", "X0011", "uint16", "Hex boundary."),
        ("narrow-device-float", "TN0", "float32", "TN holds 16-bit values only."),
        ("narrow-device-bit", "TN0.1", "bool", "No bit access on TN."),
        ("long-suffix", "CS0000 L", "int32", "Kepware long access is not supported."),
        ("unknown-device", "S0", "bool", "Unknown device."),
        ("unknown-device-k4m", "K4M0", "uint16", "Unknown device."),
        ("no-number", "D", "uint16", "A device without a number."),
        ("bad-hex-digit", "D1G", "uint16", "Malformed number."),
        ("past-d", "D4184064", "uint16", "Past the last D."),
        ("past-d-float", "D4184063", "float32", "Two words past the last D."),
        ("past-x", "X4000", "bool", "Past the last X."),
        ("past-m", "M60001", "bool", "Past the last M (M60000 is the last)."),
        ("colon-not-string", "D100:5", "bool", "':' is only valid in string addresses."),
        ("string-no-form", "D100", "string", "A string needs the DSH/DSL form."),
        ("string-no-form-m", "M0", "string", "Strings only on D, W, R and ZR."),
        ("string-no-length", "DSH100", "string", "A string needs a length."),
        ("string-odd", "DSH100.003", "string", "String length must be even."),
        ("string-long", "DSH100.130", "string", "String length at most 128."),
        ("string-dtype", "DSH100.020", "uint16", "A string address needs dtype string."),
        ("string-array", "DSH100.020[2]", "string", "Arrays of strings."),
        ("array-zero", "D100[0]", "int16", "Array dimension 0."),
        ("array-3d", "D0[2][2][2]", "int16", "Three dimensions."),
        ("array-bit-device", "M0[4]", "uint16", "Arrays on bit devices."),
        ("array-bool", "D100.1[4]", "bool", "Bool arrays."),
        ("array-past-end", "D4184060[8]", "uint16", "An array that ends past the last device."),
        ("sfx-string-past", "DSH4184063.004", "string", "A string that ends past the last D."),
    ]
    cases = [plan_case(f"reject-{cid}", f"{desc} The tag goes to invalid; the good tag still plans.",
                       [T("BAD", addr, dtype), T("GOOD", "D1", "uint16")]) for cid, addr, dtype, desc in bad]
    cases += [
        plan_case("reject-disabled-bad-address", "A disabled tag is not parsed: no invalid entry, no block.",
                  [T("OFF", "NOPE", "uint16", enabled=False)]),
        plan_case("reject-too-wide", "A tag needing more words than max_words goes to invalid.",
                  [T("BIG", "D0[8]", "uint16"), T("OK", "D100", "uint16")], max_words=4),
        plan_case("reject-960-fits", "960 words fit one request (the MC 3E limit).", [T("A", "D0[960]", "uint16")]),
        plan_case("reject-961-too-wide", "961 words do not.", [T("A", "D0[961]", "uint16")]),
    ]
    return cases


def mc_blocks() -> list[Case]:
    d = lambda n, a, t="uint16": T(n, a, t)  # noqa: E731
    return [
        plan_case("merge-contiguous", "Adjacent words merge into one block.",
                  [d("a", "D0"), d("b", "D1"), d("c", "D2", "float32")]),
        plan_case("merge-gap-16", "A gap of up to max_gap (16) words merges; the gap is read and ignored.",
                  [d("a", "D0"), d("b", "D17")]),
        plan_case("split-gap-17", "A gap of 17 words splits.", [d("a", "D0"), d("b", "D18")]),
        plan_case("max-gap-0", "max_gap 0: only adjacent words merge.",
                  [d("a", "D0"), d("b", "D1"), d("c", "D3")], max_gap=0),
        plan_case("max-gap-large", "A large max_gap merges far tags, up to max_words.",
                  [d("a", "D0"), d("b", "D400"), d("c", "D900")], max_gap=1000),
        plan_case("max-words-cap", "The merged span never exceeds max_words.",
                  [d("a", "D0"), d("b", "D9"), d("c", "D18")], max_gap=100, max_words=10),
        plan_case("devices-apart", "Different devices never share a block; blocks sorted by device then start.",
                  [d("m", "M0", "bool"), d("d", "D0"), d("x", "X0", "bool"), d("w", "W0"), d("b", "B0", "bool")]),
        plan_case("bit-device-words", "Bit devices are read 16 points per word: M0..M15 is one word.",
                  [d("a", "M0", "bool"), d("b", "M15", "bool"), d("c", "M16", "bool"), d("e", "M48", "bool")]),
        plan_case("bit-device-describe", "A hex bit device block is described in hex points.",
                  [d("a", "X1F", "bool"), d("b", "X0020", "bool")]),
        plan_case("same-data-twice", "The same data under two names and two spellings is read once.",
                  [d("a", "D100.05", "bool"), d("b", "D100.5", "bool"), d("c", "d100", "uint16"),
                   d("e", "D100", "uint16")]),
        plan_case("same-words-two-dtypes", "The same words as two dtypes are two items of one block.",
                  [d("a", "D100", "uint16"), d("b", "D100", "int16"), d("c", "D100", "uint32")]),
        plan_case("overlap", "Overlapping tags share one block.",
                  [d("a", "D100", "int64"), d("b", "D102", "uint16"), d("c", "D101", "uint32")]),
        plan_case("bit-high-word", "A bit above 15 needs the second word.",
                  [d("a", "D100.17", "bool"), d("b", "D101", "uint16")]),
        plan_case("strings-and-arrays", "Strings and arrays are items like any other.",
                  [d("s", "DSH10.010", "string"), d("a", "D20[4]", "int16"), d("g", "D30[2][2]", "float32")]),
        plan_case("block-960", "A block of exactly max_words words.",
                  [d("a", "D0[480]", "float32"), d("b", "D960", "uint16")]),
        plan_case("disabled-skipped", "Disabled tags are neither planned nor invalid.",
                  [d("a", "D0"), T("b", "D5", "uint16", enabled=False)]),
        plan_case("sorted-independent-of-input-order", "The plan does not depend on the order of the tags.",
                  [d("z", "D50"), d("a", "D10"), d("m", "M7", "bool"), d("b", "D12")]),
    ]


def mc_params() -> list[Case]:
    tags = [T("a", "D0", "float32")]
    ok = [
        ("defaults", {}, "Defaults."),
        ("explicit-defaults", {"plc_type": "Q", "protocol": "tcp", "first_word_low": True, "frame": "3E"},
         "The documented defaults, spelled out."),
        ("all-plc-types", {"plc_type": "iQ-R"}, "iQ-R is accepted (it changes the frame, not the plan)."),
        ("udp", {"protocol": "udp"}, "UDP is accepted."),
        ("zero-one-for-bool", {"first_word_low": 0}, "0 and 1 are accepted for first_word_low."),
        ("null-is-default", {"plc_type": None, "max_words": None, "first_word_low": None}, "null means default."),
        ("unknown-ignored", {"unknown_key": 1}, "Unknown keys are ignored."),
        ("route", {"network": 1, "pc": 2, "dest_moduleio": 0x3E1, "dest_modulesta": 3}, "Header route numbers."),
        ("max-words-1", {"max_words": 1}, "max_words at its minimum."),
    ]
    bad = [
        ("plc-type", {"plc_type": "A"}, "An unknown plc_type."),
        ("plc-type-number", {"plc_type": 5}, "A number for plc_type."),
        ("protocol", {"protocol": "serial"}, "An unknown protocol."),
        ("frame-4e", {"frame": "4E"}, "4E frames are not supported."),
        ("max-words-high", {"max_words": 961}, "max_words above the 3E limit."),
        ("max-words-zero", {"max_words": 0}, "max_words below 1."),
        ("max-words-bool", {"max_words": True}, "A bool is not an integer."),
        ("max-words-float", {"max_words": 10.5}, "A float is not an integer."),
        ("max-words-text", {"max_words": "10"}, "Text is not an integer."),
        ("max-gap-negative", {"max_gap": -1}, "A negative max_gap."),
        ("first-word-low-text", {"first_word_low": "yes"}, "Text for a boolean."),
        ("first-word-low-two", {"first_word_low": 2}, "Only 0 and 1 stand for a boolean."),
        ("nonnormal", {"nonnormal_float": "nan"}, "An unknown nonnormal_float."),
        ("network", {"network": 256}, "network above 255."),
        ("pc", {"pc": -1}, "pc below 0."),
        ("dest-moduleio", {"dest_moduleio": 0x10000}, "dest_moduleio above 0xFFFF."),
        ("dest-modulesta", {"dest_modulesta": 256}, "dest_modulesta above 255."),
        ("first-error-wins", {"frame": "4E", "plc_type": "A"}, "plc_type is checked before frame."),
    ]
    return ([plan_case(f"ok-{cid}", desc, tags, **p) for cid, p, desc in ok]
            + [plan_case(f"bad-{cid}", f"{desc} The driver is refused.", tags, **p) for cid, p, desc in bad])


def mc_random() -> list[Case]:
    rng = random.Random(45)
    bit_devs = ["X", "Y", "B", "M", "L", "F", "SM", "TS", "CC"]
    word_devs = ["D", "W", "R", "ZR", "SD", "TN"]
    hexdev = {"X", "Y", "B", "W", "ZR", "SB", "DX", "DY", "SW"}
    cases: list[Case] = []
    for n in range(30):
        tags = []
        for i in range(rng.randint(4, 30)):
            kind = rng.random()
            if kind < 0.3:
                dev = rng.choice(bit_devs)
                num = rng.randrange(0, 600)
                text = f"{num:X}" if dev in hexdev else str(num)
                tags.append(T(f"t{i}", f"{dev}{text}", "bool"))
            elif kind < 0.4:
                dev = rng.choice(["M", "X", "Y"])
                num = rng.randrange(0, 40) * 16
                text = f"{num:X}" if dev in hexdev else str(num)
                tags.append(T(f"t{i}", f"{dev}{text}", rng.choice(["uint16", "int16", "uint32"])))
            elif kind < 0.5:
                dev = rng.choice(word_devs[:4])
                num = rng.randrange(0, 300)
                text = f"{num:X}" if dev in hexdev else str(num)
                tags.append(T(f"t{i}", f"{dev}{text}.{rng.randrange(0, 32):02d}", "bool"))
            elif kind < 0.55:
                num = rng.randrange(0, 300)
                tags.append(T(f"t{i}", f"DS{rng.choice('HL')}{num}.{rng.choice([2, 6, 10, 20])}", "string"))
            elif kind < 0.62:
                tags.append(T(f"t{i}", f"D{rng.randrange(0, 300)}[{rng.randint(1, 8)}]", rng.choice(["int16", "float32"])))
            else:
                dev = rng.choice(word_devs)
                num = rng.randrange(0, 300)
                text = f"{num:X}" if dev in hexdev else str(num)
                dtype = rng.choice(["uint16", "int16", "int32", "uint32", "float32", "int64", "float64"])
                if dev in ("TN",):
                    dtype = "uint16"
                tags.append(T(f"t{i}", f"{dev}{text}", dtype))
            if rng.random() < 0.08:
                tags.append(T(f"dup{i}", tags[-1]["address"], tags[-1]["dtype"]))
        params: dict[str, Any] = {"max_gap": rng.choice([0, 4, 16, 100]), "max_words": rng.choice([8, 64, 960])}
        cases.append(plan_case(f"mix-{n:02d}", f"Seeded random mix {n}: {len(tags)} tags.", tags, **params))
    return cases


# --- melsec_mc/decode ---------------------------------------------------------------------------------

def mc_integers() -> list[Case]:
    cases: list[Case] = []
    for dtype, fmt, value in (
        ("uint16", "H", 65534), ("int16", "h", -2), ("uint16", "H", 0), ("int16", "h", -32768),
        ("int32", "i", -123456), ("uint32", "I", 0xDEADBEEF), ("int32", "i", 2147483647),
        ("int64", "q", -(2 ** 40) - 5), ("uint64", "Q", 2 ** 63 + 7), ("uint64", "Q", 2 ** 64 - 1),
        ("int64", "q", -(2 ** 63)),
    ):
        words = words_le(fmt, value)
        slug = f"{dtype}-{value}".replace("-", "m", 1) if value < 0 else f"{dtype}-{value}"
        cases.append(decode_case(f"low-first-{slug}", f"{dtype} {value}, the low word at the lower device number.",
                                 [T("a", "D0", dtype)], words))
        cases.append(decode_case(f"high-first-{slug}", f"{dtype} {value}, first_word_low off: the high word first.",
                                 [T("a", "D0", dtype)], list(reversed(words)), first_word_low=False))
    cases.append(decode_case("word-order-matters", "0x00010002 as uint32: the order of the words changes the value.",
                             [T("a", "D0", "uint32"), T("b", "D0", "int32")], [2, 1]))
    return cases


def mc_floats() -> list[Case]:
    cases: list[Case] = []
    for slug, dtype, fmt, value in (
        ("f32-12-5", "float32", "f", 12.5), ("f32-negative", "float32", "f", -0.15625), ("f32-small", "float32", "f", 1.5e-30),
        ("f64", "float64", "d", 1234.5678), ("f64-negative", "float64", "d", -1e300), ("f32-zero", "float32", "f", 0.0),
        ("f32-negative-zero", "float32", "f", -0.0),
    ):
        cases.append(decode_case(slug, f"{dtype} {value!r}.", [T("a", "D0", dtype)], words_le(fmt, value)))
    for slug, raw in (("nan", 0x7FC00000), ("inf", 0x7F800000), ("neg-inf", 0xFF800000), ("denormal", 0x00000001),
                      ("neg-denormal", 0x80000001)):
        words = [raw & 0xFFFF, raw >> 16]
        cases.append(decode_case(f"nonnormal-zero-{slug}", f"float32 {slug}: Kepware 'Replace with Zero'.",
                                 [T("a", "D0", "float32")], words))
        cases.append(decode_case(f"nonnormal-unmodified-{slug}", f"float32 {slug}, nonnormal_float unmodified.",
                                 [T("a", "D0", "float32")], words, nonnormal_float="unmodified"))
    for slug, raw in (("nan", 0x7FF8000000000000), ("inf", 0x7FF0000000000000), ("denormal", 1)):
        words = [(raw >> (16 * i)) & 0xFFFF for i in range(4)]
        cases.append(decode_case(f"nonnormal-zero-f64-{slug}", f"float64 {slug} replaced with zero.",
                                 [T("a", "D0", "float64")], words))
        cases.append(decode_case(f"nonnormal-unmodified-f64-{slug}", f"float64 {slug} unmodified.",
                                 [T("a", "D0", "float64")], words, nonnormal_float="unmodified"))
    return cases


def mc_bits_strings_arrays() -> list[Case]:
    return [
        decode_case("bit-set", "Bit 5 of a word set.", [T("a", "D100.05", "bool")], [1 << 5]),
        decode_case("bit-clear", "Bit 5 of a word clear.", [T("a", "D100.05", "bool")], [0xFFFF & ~(1 << 5)]),
        decode_case("bit-15-sign", "Bit 15 is the sign bit of the word.", [T("a", "D100.15", "bool")], [0x8000]),
        decode_case("bit-17-second-word", "Bit 17 is bit 1 of the second word.", [T("a", "D100.17", "bool")], [0, 2]),
        decode_case("bit-17-high-first", "Bit 17 with first_word_low off: the first word is the high one.",
                    [T("a", "D100.17", "bool")], [2, 0], first_word_low=False),
        decode_case("bit-31", "Bit 31 of a two-word value.", [T("a", "D100.31", "bool")], [0, 0x8000]),
        decode_case("bits-of-one-word", "Several bits and the word itself from one word.",
                    [T("a", "D5.0", "bool"), T("b", "D5.1", "bool"), T("c", "D5.15", "bool"), T("w", "D5", "uint16")],
                    [0x8001]),
        decode_case("bit-device-bits", "On a bit device the lowest number is bit 0 of the word.",
                    [T("m0", "M0", "bool"), T("m1", "M1", "bool"), T("m17", "M17", "bool"), T("m31", "M31", "bool")],
                    [0b01, 0x8002]),
        decode_case("bit-device-hex", "A hex bit device: X1F is bit 15 of word 1.",
                    [T("a", "X1F", "bool"), T("b", "X10", "bool"), T("c", "X0", "bool")], [1, 0x8000]),
        decode_case("bit-device-as-word", "16 points of M read as an unsigned word.", [T("a", "M16", "uint16")], [0xFFFF]),
        decode_case("bit-device-as-dword", "32 points of X read as an int32.", [T("a", "X0010", "int32")], [0xFFFF, 0xFFFF]),
        decode_case("string-high", "DSH: the first character in the high byte; ends at the first NUL.",
                    [T("a", "DSH60.010", "string")], [0x5055, 0x4D50, 0x2041, 0, 0]),
        decode_case("string-low", "DSL: the first character in the low byte.", [T("a", "DSL70:006", "string")],
                    [0x5550, 0x504D, 0x4120]),
        decode_case("string-full", "A string using every byte.", [T("a", "DSH0.004", "string")], [0x4142, 0x4344]),
        decode_case("string-nul-first", "A string that starts with NUL is empty.", [T("a", "DSH0.004", "string")], [0, 0x4142]),
        decode_case("string-latin1", "Bytes above 0x7F are latin-1 characters.", [T("a", "DSH0.002", "string")], [0xE9FC]),
        decode_case("array-1d", "A one dimensional array.", [T("a", "D40[4]", "int16")], [1, 0xFFFF, 3, 4]),
        decode_case("array-2d", "A two dimensional array, row by row.", [T("a", "D50[2][2]", "float32")],
                    words_le("f", 1.5) + words_le("f", -2.0) + words_le("f", 3.0) + words_le("f", 0.25)),
        decode_case("array-high-first", "An array of uint32 with first_word_low off.", [T("a", "D0[2]", "uint32")],
                    [1, 2, 3, 4], first_word_low=False),
    ]


def mc_block_items() -> list[Case]:
    return [
        decode_case("offsets", "Items at their offsets inside a merged block; the gap words are ignored.",
                    [T("a", "D10", "uint16"), T("b", "D12", "uint32"), T("c", "D20", "int16")],
                    [1, 99, 0x0003, 0x0004, 99, 99, 99, 99, 99, 0xFFFF]),
        decode_case("aliases", "Two names, one address: both get the value.",
                    [T("a", "D1", "uint16"), T("b", "D1", "uint16"), T("c", "D1.0", "bool")], [7, 5]),
        decode_case("same-words-two-dtypes", "One word as uint16, int16, and half of a uint32.",
                    [T("a", "D0", "uint16"), T("b", "D0", "int16"), T("c", "D0", "uint32")], [0xFFFF, 1]),
        decode_case("short-reply", "A reply shorter than the block fails only the tags that do not fit.",
                    [T("a", "D0", "uint16"), T("b", "D1", "uint32"), T("c", "D5", "uint16")], [1, 2]),
        decode_case("empty-reply", "No words at all: every tag fails.", [T("a", "D0", "uint16"), T("b", "D1", "uint16")], []),
        decode_case("long-reply", "Extra words are ignored.", [T("a", "D0", "uint16")], [1, 2, 3]),
    ]


def mc_decode_random() -> list[Case]:
    rng = random.Random(46)
    cases: list[Case] = []
    for n in range(20):
        tags = []
        base = rng.randrange(0, 50)
        offset = 0
        for i in range(rng.randint(2, 8)):
            dtype = rng.choice(["uint16", "int16", "int32", "uint32", "float32", "int64", "float64", "bool"])
            if dtype == "bool":
                tags.append(T(f"t{i}", f"D{base + offset}.{rng.randrange(0, 16):02d}", "bool"))
                offset += 1
            else:
                tags.append(T(f"t{i}", f"D{base + offset}", dtype))
                offset += {"uint16": 1, "int16": 1}.get(dtype, 4 if "64" in dtype else 2) + rng.randrange(0, 3)
        words = [rng.randrange(0, 0x10000) for _ in range(offset + 4)]
        params: dict[str, Any] = {"first_word_low": rng.random() < 0.5, "nonnormal_float": rng.choice(["zero", "unmodified"])}
        cases.append(decode_case(f"mix-{n:02d}", f"Seeded random mix {n}: {len(tags)} tags, random words.", tags, words, **params))
    return cases


# --- melsec_mc/frame and melsec_mc/reply -------------------------------------------------------------------

def frame_case(cid: str, desc: str, tags: list[dict[str, Any]], timeout_ms: int = 1000, **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags, "timeout_ms": timeout_ms}


def mc_requests() -> list[Case]:
    one = [T("a", "D100", "uint16")]
    return [
        frame_case("d100-q", "Batch read of D100, the manual's example shape: command 0401, subcommand 0000, 3-byte number, device code A8.", one),
        frame_case("d0-five-words", "Five words from D0.", [T("a", "D0[5]", "uint16")]),
        frame_case("iqr", "iQ-R: subcommand 0002, 4-byte device number, 2-byte device code.", one, plc_type="iQ-R"),
        frame_case("iql", "iQ-L uses the Q form of the frame.", one, plc_type="iQ-L"),
        frame_case("l-qna", "L and QnA use the Q form of the frame.", one, plc_type="L"),
        frame_case("every-device", "Every device code, one block each (bit devices read in words).",
                   [T(f"b{n}", f"{n}0", "bool") for n in ("X", "Y", "DX", "DY", "B", "SB", "M", "SM", "L", "F", "V", "TS", "TC", "SS", "SC", "CS", "CC")]
                   + [T(f"w{n}", f"{n}10", "uint16") for n in ("D", "SD", "W", "SW", "R", "ZR", "TN", "SN", "CN")]),
        frame_case("every-device-iqr", "The same blocks in the iQ-R form.",
                   [T(f"b{n}", f"{n}0", "bool") for n in ("X", "Y", "DX", "DY", "B", "SB", "M", "SM", "L", "F", "V", "TS", "TC", "SS", "SC", "CS", "CC")]
                   + [T(f"w{n}", f"{n}10", "uint16") for n in ("D", "SD", "W", "SW", "R", "ZR", "TN", "SN", "CN")], plc_type="iQ-R"),
        frame_case("hex-head", "A hex device head number: B001F is point 31, read from word 1 (point 16).", [T("a", "B001F", "bool")]),
        frame_case("big-number", "A large device number needs all three bytes.", [T("a", "D4184000", "uint16")]),
        frame_case("big-number-iqr", "A large device number in the 4-byte iQ-R form.", [T("a", "D4184000", "uint16")], plc_type="iQ-R"),
        frame_case("block-960", "960 words: the 3E limit.", [T("a", "D0[960]", "uint16")]),
        frame_case("route", "Network, PC number, module I/O and station go into the header.", one,
                   network=1, pc=2, dest_moduleio=0x3E1, dest_modulesta=3),
        frame_case("timer-300ms", "A 300 ms timeout is 2 units of 250 ms.", one, timeout_ms=300),
        frame_case("timer-1ms", "At least one unit.", one, timeout_ms=1),
        frame_case("timer-max", "At most 0xFFFF units.", one, timeout_ms=100_000_000),
        frame_case("timer-3000ms", "The default device timeout of 3 s.", one, timeout_ms=3000),
        frame_case("timer-250ms", "250 ms is exactly one unit.", one, timeout_ms=250),
        frame_case("timer-251ms", "251 ms rounds up to two units.", one, timeout_ms=251),
        frame_case("qna", "QnA uses the Q form of the frame.", one, plc_type="QnA"),
        frame_case("two-blocks", "Two blocks give two requests, in plan order.",
                   [T("a", "D0", "uint16"), T("b", "D500", "uint16"), T("c", "M32", "bool")]),
        frame_case("bit-device-words", "A bit device block: head point and word count.",
                   [T("a", "M0", "bool"), T("b", "M47", "bool")]),
        frame_case("no-tags", "No tag, no request; the CPU model request is still built.", []),
    ]


def reply_case(cid: str, desc: str, frame: bytes, words: int) -> Case:
    return cid, desc, {"frame": frame.hex(), "words": words}


def _reply(data: bytes, end: int = 0, route: bytes = b"\x00\xff\xff\x03\x00", subheader: bytes = b"\xd0\x00",
           length: int | None = None) -> bytes:
    body = end.to_bytes(2, "little") + data
    n = len(body) if length is None else length
    return subheader + route + n.to_bytes(2, "little") + body


def mc_replies() -> list[Case]:
    err_info = b"\x00\xff\xff\x03\x00" + (0x0401).to_bytes(2, "little") + (0).to_bytes(2, "little")
    cases = [
        reply_case("two-words", "A good reply with the two words asked for.", _reply(b"\x34\x12\x78\x56"), 2),
        reply_case("one-word", "One word.", _reply(b"\xff\xff"), 1),
        reply_case("wrong-count-short", "One word fewer than asked is an error, not a zero.", _reply(b"\x34\x12"), 2),
        reply_case("wrong-count-long", "One word more than asked is an error.", _reply(b"\x34\x12\x78\x56\x00\x00"), 2),
        reply_case("odd-bytes", "An odd number of data bytes.", _reply(b"\x34\x12\x78"), 2),
        reply_case("no-data", "End code 0 and no data.", _reply(b""), 1),
        reply_case("wrong-subheader", "A reply that does not start with d000.", _reply(b"\x34\x12", subheader=b"\xd1\x00"), 1),
        reply_case("request-echo", "A request frame (5000...) is not a reply.", b"\x50\x00" + _reply(b"\x00\x00")[2:], 1),
        reply_case("length-too-long", "The header announces more than the frame carries.", _reply(b"\x34\x12", length=9), 1),
        reply_case("length-too-short", "The header announces less than the frame carries.", _reply(b"\x34\x12", length=3), 1),
        reply_case("header-only", "A frame that ends inside the header.", b"\xd0\x00\x00\xff\xff\x03\x00\x02", 1),
        reply_case("empty-frame", "An empty frame.", b"", 1),
        reply_case("length-zero", "A header announcing no end code.", b"\xd0\x00\x00\xff\xff\x03\x00\x00\x00", 1),
    ]
    for code in (0xC050, 0xC051, 0xC052, 0xC053, 0xC054, 0xC055, 0xC056, 0xC058, 0xC059, 0xC05B, 0xC05C, 0xC05F,
                 0xC060, 0xC061, 0xC06F, 0x4030, 0x4031, 0x4010, 0xC0FF, 0x0001, 0xFFFF):
        cases.append(reply_case(f"end-code-{code:04x}", f"End code {code:#06x} with the error information block.",
                                _reply(err_info, end=code), 4))
    cases.append(reply_case("end-code-no-info", "An error end code with no error information block.", _reply(b"", end=0xC056), 4))
    cases.append(reply_case("end-code-wrong-count-ignored", "With an end code the data length is not checked.",
                            _reply(b"\x01", end=0xC059), 4))
    return cases


SPECS: dict[str, list[FileSpec]] = {
    "melsec_mc/plan": [
        ("addresses", "Valid Kepware Mitsubishi (Q Series) address forms (D-054).", mc_addresses()),
        ("rejected", "Rejected addresses go to invalid with a reason (D-054).", mc_rejected()),
        ("blocks", "Block merging by device, max_gap and max_words; bit devices in words (D-055).", mc_blocks()),
        ("params", "Driver params: refused values and accepted forms.", mc_params()),
        ("random_mix", "Seeded random mixes of devices, dtypes and limits.", mc_random()),
    ],
    "melsec_mc/decode": [
        ("integers", "Integers and word order (first_word_low) (D-054).", mc_integers()),
        ("floats", "Floats and Kepware's non-normal float handling (D-054).", mc_floats()),
        ("bits_strings_arrays", "Bits of words, bit devices, strings and arrays (D-054).", mc_bits_strings_arrays()),
        ("block_items", "Items at offsets inside a block; aliases; short, empty and long replies.", mc_block_items()),
        ("random_mix", "Seeded random words decoded as random tags.", mc_decode_random()),
    ],
    "melsec_mc/frame": [
        ("requests", "The batch-read request bytes pymcprotocol builds for each block, and its monitoring timer (D-053).",
         mc_requests()),
    ],
    "melsec_mc/reply": [
        ("replies", "Reply frame checks, end codes and their mapping to a config or a comm error (D-053, D-056).",
         mc_replies()),
    ],
}
