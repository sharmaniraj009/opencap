"""Regenerate the shared conformance vectors from the Python reference (ADR-0014).

The inputs below (Allen-Bradley ones in `gen_conformance_ab.py`) are written by hand
(directed cases, one behaviour each) plus a few seeded random mixes; every `expected` is what
the current Python code makes of the input
(`tests/conformance/reference.py`). Files land in `tests/conformance/<area>/`, format in
`tests/conformance/README.md`.

    python scripts/gen_conformance.py            # rewrite the files
    python scripts/gen_conformance.py --check    # exit 1 if a file is stale, missing or extra

`tests/unit/test_conformance_vectors.py` fails when the files and the Python code disagree.
After a deliberate behaviour change: regenerate, review the diff vector by vector, and say in
the PR which engine issue ports the change.
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "scripts"):        # the repo (opencap, tests) and the AB case module beside us
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gen_conformance_ab  # noqa: E402
import gen_conformance_system_tags  # noqa: E402
import gen_conformance_fins  # noqa: E402

from opencap import FIRM_VERSION  # noqa: E402
from opencap.config.models import Device, PayloadCfg, PublishCfg, Scale, Tag  # noqa: E402
from tests.conformance import reference  # noqa: E402
from tests.conformance.vectors import AREAS, encode_value, render  # noqa: E402
from tests.conformance.vectors import ROOT as VECTORS_ROOT  # noqa: E402

Case = tuple[str, str, dict[str, Any]]          # (id, description, input)
FileSpec = tuple[str, str, list[Case]]          # (file stem, file description, cases)


# --- helpers -----------------------------------------------------------------------------------

def T(name: str, address: str, dtype: str = "uint16", **extra: Any) -> dict[str, Any]:
    return {"name": name, "address": address, "dtype": dtype, **extra}


def regs(bits: int, n: int, *, big: bool = True, fwl: bool = True, fdl: bool = True) -> list[int]:
    """Wire registers holding the `n`-register bit pattern `bits` under the given orders
    (byte_order_big, first_word_low, first_dword_low): the input side of a decode vector."""
    words = [(bits >> (16 * (n - 1 - i))) & 0xFFFF for i in range(n)]      # most significant first
    if n == 2 and fwl:
        words = words[::-1]
    elif n == 4:
        hi, lo = words[:2], words[2:]
        if fwl:
            hi, lo = hi[::-1], lo[::-1]
        words = lo + hi if fdl else hi + lo
    if not big:
        words = [((w & 0xFF) << 8) | (w >> 8) for w in words]
    return words


def f32_bits(x: float) -> int:
    return struct.unpack(">I", struct.pack(">f", x))[0]


def f64_bits(x: float) -> int:
    return struct.unpack(">Q", struct.pack(">d", x))[0]


def orders_id(big: bool, fwl: bool, fdl: bool | None = None) -> str:
    out = ("big" if big else "swap") + ("-fwl" if fwl else "-fwh")
    if fdl is not None:
        out += "-fdl" if fdl else "-fdh"
    return out


def order_params(big: bool, fwl: bool, fdl: bool | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"byte_order_big": big, "first_word_low": fwl}
    if fdl is not None:
        p["first_dword_low"] = fdl
    return p


# --- modbus/plan ---------------------------------------------------------------------------------

def plan_case(cid: str, desc: str, tags: list[dict[str, Any]], **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags}


def plan_merge() -> list[Case]:
    return [
        plan_case("empty", "No tags: no blocks, nothing invalid.", []),
        plan_case("single-float32", "One float32 at 400045 reads FC03 44..45.", [T("FLOW_1", "400045", "float32")]),
        plan_case("contiguous", "Adjacent float32, float32, uint16 merge into one 5-register block.",
                  [T("A", "400001", "float32"), T("B", "400003", "float32"), T("C", "400005")]),
        plan_case("gap-4-merges-gap-5-splits",
                  "Default max_gap 4: a 4-register hole merges, a 5-register hole starts a new block.",
                  [T("A", "400001", "float32"), T("B", "400007"), T("C", "400013")]),
        plan_case("max-gap-0", "max_gap 0: only touching tags merge; a 1-register hole splits.",
                  [T("A", "400001"), T("B", "400002"), T("C", "400004")], max_gap=0),
        plan_case("max-gap-1", "max_gap 1: a 1-register hole merges.",
                  [T("A", "400001"), T("B", "400003")], max_gap=1),
        plan_case("max-gap-10", "max_gap 10: a 10-register hole merges, 11 splits.",
                  [T("A", "400001"), T("B", "400012"), T("C", "400024")], max_gap=10),
        plan_case("fc-never-merge", "Coils, discrete inputs, input and holding registers never share a "
                  "block; blocks are sorted by function code then address.",
                  [T("H", "400001"), T("I", "300001"), T("D", "100001", "bool"), T("C", "000001", "bool"),
                   T("H2", "400002"), T("I2", "300002")]),
        plan_case("input-order-irrelevant", "Tags given in reverse address order plan exactly as sorted ones.",
                  [T(f"R{i}", f"4{i * 3 + 1:05d}") for i in reversed(range(12))]),
        plan_case("overlap-mixed-types", "A float32 and a uint16 overlapping its second register share one block.",
                  [T("F", "400010", "float32"), T("W", "400011"), T("X", "400013", "int32")]),
        plan_case("overlap-contained", "A uint16 inside a 4-register float64 does not extend the block.",
                  [T("D", "400100", "float64"), T("W", "400102")]),
        plan_case("block-tags-sorted", "A block's tag list is sorted by name, not by address.",
                  [T("Zeta", "400001"), T("alpha", "400002"), T("Mid", "400003"), T("BETA", "400004")]),
        plan_case("gap-measured-from-block-end",
                  "The gap is measured from the end of the open block (the longest tag), not the last tag.",
                  [T("L", "400001", "float64"), T("S", "400002"), T("N", "400009")]),
        plan_case("many-small-blocks", "Tags 6 registers apart never merge with the default gap.",
                  [T(f"T{i:02d}", f"4{i * 6 + 1:05d}") for i in range(10)]),
    ]


def plan_block_size() -> list[Case]:
    forty = [T(f"R{i:02d}", f"4{i + 1:05d}") for i in range(40)]
    seventy = [T(f"R{i:02d}", f"4{i + 1:05d}") for i in range(70)]
    return [
        plan_case("default-span-64", "Default max_holding 64: 70 contiguous registers split 64 + 6.", seventy),
        plan_case("kep-32", "D-005: max_holding 32 (the KEPServer device setting) splits 40 registers 32 + 8.",
                  forty, max_holding=32),
        plan_case("span-exact-limit", "A block may be exactly max_holding long (max_gap large enough to bridge).",
                  [T("A", "400001"), T("B", "400032")], max_holding=32, max_gap=40),
        plan_case("span-limit-plus-one", "A tag that would make the block one longer than max_holding starts a "
                  "new block even within max_gap.", [T("A", "400001"), T("B", "400033")], max_holding=32, max_gap=40),
        plan_case("float32-never-straddles", "A 32-bit tag that would cross the span limit moves whole to the "
                  "next block.", [T(f"R{i:02d}", f"4{i + 1:05d}") for i in range(31)] + [T("F", "400032", "float32")],
                  max_holding=32),
        plan_case("holding-capped-125", "max_holding 500 is capped at the protocol limit 125.",
                  [T(f"R{i:03d}", f"4{i + 1:05d}") for i in range(130)], max_holding=500),
        plan_case("input-uses-max-input", "Input registers use max_input, holding registers max_holding.",
                  [T(f"I{i:02d}", f"3{i + 1:05d}") for i in range(20)]
                  + [T(f"H{i:02d}", f"4{i + 1:05d}") for i in range(20)], max_input=8, max_holding=16),
        plan_case("coils-use-max-coils", "Coils use max_coils.",
                  [T(f"C{i:02d}", f"0{i + 1:05d}", "bool") for i in range(20)], max_coils=8),
        plan_case("discrete-use-max-discrete", "Discrete inputs use max_discrete, independent of max_coils.",
                  [T(f"D{i:02d}", f"1{i + 1:05d}", "bool") for i in range(20)], max_coils=4, max_discrete=16),
        plan_case("coils-capped-2000", "max_coils 5000 is capped at the protocol limit 2000 bits.",
                  [T("CA", "000001[1500]", "bool"), T("CB", "001501[600]", "bool")], max_coils=5000),
        plan_case("tag-larger-than-span-invalid", "D-006: an array longer than max_holding goes to invalid and "
                  "is never sent oversized.", [T("BIG", "400001[70]"), T("OK", "400100")]),
        plan_case("tag-larger-than-protocol-invalid", "A 126-register array is invalid even with max_holding 500.",
                  [T("BIG", "400001[126]"), T("OK", "400200")], max_holding=500),
        plan_case("coil-array-over-2000-invalid", "A 2001-bit coil array is invalid at the 2000-bit protocol cap.",
                  [T("BIG", "000001[2001]", "bool")], max_coils=2000),
        plan_case("string-250-needs-125", "A 250-byte string is 125 registers: valid at max_holding 125, "
                  "invalid at the default 64.",
                  [T("S", "400001.250H", "string")], max_holding=125),
        plan_case("string-250-default-invalid", "A 250-byte string does not fit the default max_holding 64.",
                  [T("S", "400001.250H", "string"), T("W", "400300")]),
        plan_case("float64-array-at-limit", "16 float64 (64 registers) fit the default span exactly.",
                  [T("A", "400001[16]", "float64")]),
        plan_case("max-holding-1", "max_holding 1: every register is its own block and a float32 is invalid.",
                  [T("A", "400001"), T("B", "400002"), T("F", "400010", "float32")], max_holding=1),
    ]


def plan_bit_gaps() -> list[Case]:
    return [
        plan_case("coil-gap-64-merges", "FC01 gap defaults to max_gap x 16 = 64 bits: a 64-bit hole merges.",
                  [T("C1", "000001", "bool"), T("C2", "000066", "bool")], max_coils=2000),
        plan_case("coil-gap-65-splits", "A 65-bit hole between coils splits.",
                  [T("C1", "000001", "bool"), T("C2", "000067", "bool")], max_coils=2000),
        plan_case("max-gap-scales-bit-gap", "max_gap 1 makes the coil gap 16 bits.",
                  [T("C1", "000001", "bool"), T("C2", "000018", "bool"), T("C3", "000036", "bool")],
                  max_gap=1, max_coils=2000),
        plan_case("max-gap-0-bits", "max_gap 0 makes the coil gap 0: only touching coils merge.",
                  [T("C1", "000001", "bool"), T("C2", "000002", "bool"), T("C3", "000004", "bool")], max_gap=0),
        plan_case("max-gap-bits-override", "max_gap_bits 3 overrides max_gap x 16 for coils and discrete inputs.",
                  [T("C1", "000001", "bool"), T("C2", "000005", "bool"), T("C3", "000010", "bool"),
                   T("D1", "100001", "bool"), T("D2", "100005", "bool")], max_gap_bits=3),
        plan_case("max-gap-bits-0", "max_gap_bits 0 with a large max_gap: coils still merge only when touching.",
                  [T("C1", "000001", "bool"), T("C2", "000003", "bool"), T("R1", "400001"), T("R2", "400009")],
                  max_gap_bits=0, max_gap=10),
        plan_case("max-gap-bits-null-default", "max_gap_bits null means the default (max_gap x 16).",
                  [T("C1", "000001", "bool"), T("C2", "000066", "bool")], max_gap_bits=None, max_coils=2000),
        plan_case("max-gap-bits-registers-unaffected", "max_gap_bits does not change the register gap.",
                  [T("R1", "400001"), T("R2", "400006"), T("R3", "400012")], max_gap_bits=100),
        plan_case("discrete-gap-default", "FC02 uses the same 64-bit default gap.",
                  [T("D1", "100001", "bool"), T("D2", "100066", "bool"), T("D3", "100200", "bool")],
                  max_discrete=2000),
        plan_case("coil-span-with-gaps", "Coil gaps count toward max_coils: 3 coils 30 apart split at max_coils 64.",
                  [T("C1", "000001", "bool"), T("C2", "000031", "bool"), T("C3", "000061", "bool"),
                   T("C4", "000091", "bool")]),
    ]


def plan_strings_bits() -> list[Case]:
    return [
        plan_case("string-even", "String .020H is 10 registers.", [T("S", "400001.020H", "string")]),
        plan_case("string-odd-rounds-up", "String .005L is 3 registers; the suffix is case-insensitive.",
                  [T("S", "300101.005l", "string")]),
        plan_case("string-1", "A 1-byte string still reads one register.", [T("S", "400010.001H", "string")]),
        plan_case("strings-merge", "Adjacent strings and a word merge into one block.",
                  [T("S1", "400001.004H", "string"), T("S2", "400003.003L", "string"), T("W", "400005")]),
        plan_case("string-and-word-same-start", "A string and a uint16 at the same start are separate items in "
                  "one block.", [T("S", "400001.006H", "string"), T("W", "400001")]),
        plan_case("bits-share-register", "Bits .0, .7 and .15 of one register and the register itself: one "
                  "1-register block.",
                  [T("B0", "400001.0", "bool"), T("B7", "400001.7", "bool"), T("B15", "400001.15", "bool"),
                   T("W", "400001")]),
        plan_case("bits-adjacent-registers", "Bits in neighbouring registers merge like words.",
                  [T("A", "400001.3", "bool"), T("B", "400002.3", "bool"), T("C", "400010.0", "bool")]),
        plan_case("bit-one-based", "zero_based_bit off: .1 and .16 are valid, same register.",
                  [T("B1", "400001.1", "bool"), T("B16", "400001.16", "bool")], zero_based_bit=False),
        plan_case("bit-modicon", "modicon_bit_order does not change the plan, only which bit is taken.",
                  [T("M0", "400001.0", "bool"), T("M15", "400001.15", "bool")], modicon_bit_order=True),
        plan_case("bit-on-input-register", "A .b bit on an input register reads FC04.",
                  [T("IB", "300005.2", "bool"), T("IW", "300006", "int16")]),
        plan_case("string-in-input-registers", "Strings work on input registers (FC04).",
                  [T("S", "300001.010H", "string")]),
    ]


def plan_addressing() -> list[Case]:
    return [
        plan_case("zero-based-default", "Default zero_based: reference 400001 is protocol address 0.",
                  [T("A", "400001"), T("B", "465536")]),
        plan_case("zero-based-off", "zero_based off: the reference is the protocol address; 400000 is valid.",
                  [T("Z", "400000"), T("A", "400001")], zero_based=False),
        plan_case("zero-based-off-coil-0", "zero_based off: coil 000000 is protocol address 0.",
                  [T("C0", "000000", "bool"), T("C1", "000001", "bool")], zero_based=False),
        plan_case("six-digit-max", "The 6-digit form reaches reference 65536 (protocol address 65535).",
                  [T("LAST", "465536"), T("LAST_C", "065536", "bool")]),
        plan_case("five-digit-forms", "5-digit forms: 40001 is 400001, 30001 is 300001, 00001 and 10001 bits.",
                  [T("H", "40001"), T("I", "30010"), T("C", "00005", "bool"), T("D", "19999", "bool")]),
        plan_case("whitespace-stripped", "Leading and trailing whitespace around the address is ignored.",
                  [T("A", "  400001 "), T("B", "\t400002")]),
        plan_case("array-spaces", "Spaces inside and around array brackets are allowed.",
                  [T("A", "400001 [ 2 ] [ 3 ]", "int16"), T("B", "400010[ 4 ]", "uint16")]),
        plan_case("arrays-2d-count", "A [2][3] float32 array is 12 registers.", [T("M", "400001[2][3]", "float32")]),
        plan_case("arrays-1-element", "A [1] array is a 1-element array (still one register).",
                  [T("A", "400001[1]")]),
        plan_case("int64-count", "64-bit types take 4 registers.",
                  [T("Q", "400001", "int64"), T("U", "400005", "uint64"), T("D", "400009", "float64")]),
        plan_case("last-register-float32", "A float32 at 465535 uses the last two registers: valid.",
                  [T("F", "465535", "float32")]),
        plan_case("bool-coil-array", "A bool array on coils is valid (one bit per element).",
                  [T("CA", "000001[16]", "bool"), T("CB", "000017[2][4]", "bool")]),
    ]


def plan_rejected() -> list[Case]:
    bad = [
        ("empty", "", "uint16", "Empty address."),
        ("blank", "   ", "uint16", "Address of spaces only."),
        ("letters", "abc", "uint16", "Not digits."),
        ("four-digits", "4001", "uint16", "Too few digits."),
        ("seven-digits", "4000011", "uint16", "Too many digits."),
        ("area-5", "500001", "uint16", "Unknown area digit 5."),
        ("area-2", "200001", "uint16", "Unknown area digit 2."),
        ("ref-0-zero-based", "400000", "uint16", "Reference 0 with zero_based on."),
        ("ref-65537", "465537", "uint16", "6-digit reference above 65536."),
        ("past-end-float32", "465536", "float32", "Second register past protocol address 65535."),
        ("past-end-array", "465530[10]", "uint16", "Array runs past the last address."),
        ("register-bool-no-bit", "400001", "bool", "A bool on a register needs .b."),
        ("coil-uint16", "000001", "uint16", "Coils are bool only."),
        ("discrete-float32", "100001", "float32", "Discrete inputs are bool only."),
        ("coil-bit-suffix", "000001.1", "bool", "No .b inside a coil."),
        ("bit-on-uint16", "400001.3", "uint16", "A .b address must be bool."),
        ("bit-16", "400001.16", "bool", "Bit .16 with zero_based_bit on."),
        ("bit-array", "400001.3[2]", "bool", "No arrays on .b addresses."),
        ("array-dim-0", "400001[0]", "uint16", "Array dimension 0."),
        ("array-3d", "400001[2][2][2]", "uint16", "More than 2 array dimensions."),
        ("register-bool-array", "400001[2]", "bool", "A bool array on registers is not allowed."),
        ("string-no-length", "400001", "string", "A string needs .lllH or .lllL."),
        ("string-no-order", "400001.20", "string", "A string needs the H or L suffix."),
        ("string-length-0", "400001.000H", "string", "String length 0."),
        ("string-length-251", "400001.251H", "string", "String length above 250."),
        ("string-order-on-uint16", "400001.020H", "uint16", "A .lllH address needs dtype string."),
        ("string-on-coil", "000001.020H", "string", "Strings only on registers."),
        ("string-array", "400001.020H[2]", "string", "No arrays of strings."),
        ("five-digit-ref-0", "40000", "uint16", "5-digit reference 0 with zero_based on."),
        ("trailing-junk", "400001x", "uint16", "Trailing characters."),
        ("negative", "-400001", "uint16", "A sign is not part of an address."),
    ]
    cases = [plan_case(f"reject-{cid}", f"{desc} The tag goes to invalid with a reason; the good tag still plans.",
                       [T("BAD", addr, dtype), T("GOOD", "400100")]) for cid, addr, dtype, desc in bad]
    cases += [
        plan_case("reject-bit-0-one-based", "zero_based_bit off: bit .0 is out of range 1..16.",
                  [T("BAD", "400001.0", "bool"), T("OK", "400001.16", "bool")], zero_based_bit=False),
        plan_case("reject-ref-65536-zero-based-off", "zero_based off: reference 65536 is past the last address.",
                  [T("BAD", "465536"), T("OK", "465535")], zero_based=False),
        plan_case("reject-several", "Several bad tags at once: invalid is keyed and sorted by tag name.",
                  [T("Z_BAD", "999999"), T("A_BAD", "400001", "bool"), T("M_OK", "400001"),
                   T("B_BAD", "400001.300H", "string")]),
        plan_case("reject-disabled-not-reported", "A disabled tag is neither planned nor reported, even with a "
                  "bad address.", [T("OFF_BAD", "nonsense", enabled=False), T("OFF_OK", "400002", enabled=False),
                                   T("ON", "400001")]),
    ]
    return cases


def plan_same_address() -> list[Case]:
    return [
        plan_case("aliases-one-item", "Two names at the same address and type are one wire item served to both.",
                  [T("FLOW", "400133", "float32"), T("FLOW_COPY", "400133", "float32"), T("W", "400136")]),
        plan_case("five-and-six-digit-alias", "400001 and 40001 are the same register: one block, both names.",
                  [T("SIX", "400001"), T("FIVE", "40001")]),
        plan_case("same-address-different-type", "Same address, different dtype: two items, one block.",
                  [T("U", "400001", "uint16"), T("I", "400001", "int16"), T("F", "400001", "float32")]),
        plan_case("three-aliases-spacing", "Spelling differences (spaces) do not create separate items.",
                  [T("A", "400010"), T("B", " 400010"), T("C", "400010 ")]),
        plan_case("same-bit-aliases", "The same .b bit under two names.",
                  [T("RUN", "400001.4", "bool"), T("RUN2", "400001.4", "bool"), T("STOP", "400001.5", "bool")]),
        plan_case("same-coil-aliases", "The same coil under two names, plus a neighbour.",
                  [T("C", "000010", "bool"), T("C_ALIAS", "00010", "bool"), T("N", "000011", "bool")]),
        plan_case("disabled-alias", "A disabled alias is left out of the block's tag list.",
                  [T("ON", "400001"), T("OFF", "400001", enabled=False)]),
        plan_case("all-disabled", "Only disabled tags: no blocks, nothing invalid.",
                  [T("A", "400001", enabled=False), T("B", "000001", "bool", enabled=False)]),
    ]


def plan_params() -> list[Case]:
    tags = [T("A", "400001"), T("B", "400003")]
    return [
        plan_case("param-max-holding-0", "max_holding 0 is refused when the driver is built.", tags, max_holding=0),
        plan_case("param-max-coils-negative", "max_coils -1 is refused.", tags, max_coils=-1),
        plan_case("param-max-gap-negative", "max_gap -1 is refused.", tags, max_gap=-1),
        plan_case("param-max-gap-bits-negative", "max_gap_bits -1 is refused.", tags, max_gap_bits=-1),
        plan_case("param-unit-id-256", "unit_id 256 is refused (0..255).", tags, unit_id=256),
        plan_case("param-unit-id-negative", "unit_id -1 is refused.", tags, unit_id=-1),
        plan_case("param-bool-string", "A boolean param given as a string is refused.", tags, zero_based="maybe"),
        plan_case("param-bool-2", "A boolean param given as 2 is refused.", tags, first_word_low=2),
        plan_case("param-int-as-string", "An integer param given as a string is refused.", tags, max_gap="4"),
        plan_case("param-int-as-bool", "An integer param given as true is refused.", tags, max_holding=True),
        plan_case("param-int-as-float", "An integer param given as 4.0 is refused.", tags, max_gap=4.0),
        plan_case("param-nonnormal-bogus", "nonnormal_float must be zero or unmodified.", tags,
                  nonnormal_float="nan"),
        plan_case("param-bool-as-0-1", "Boolean params may be written 0/1 (hand-written YAML).",
                  [T("Z", "400000"), T("A", "400001")], zero_based=0),
        plan_case("param-null-is-default", "A null param means its default.",
                  tags, max_gap=None, zero_based=None, max_holding=None),
        plan_case("param-unknown-ignored", "Unknown param keys are ignored.", tags, colour="blue", rack=3),
        plan_case("param-all-explicit", "Every param given explicitly with its default.", tags,
                  unit_id=0, zero_based=True, zero_based_bit=True, modicon_bit_order=False, byte_order_big=True,
                  first_word_low=True, first_dword_low=True, max_coils=64, max_discrete=64, max_input=64,
                  max_holding=64, max_gap=4, max_gap_bits=None, close_on_timeout=True, nonnormal_float="zero"),
    ]


def plan_random() -> list[Case]:
    """Seeded mixes of every address form against random limits: the interplay the directed
    cases do not reach."""
    rng = random.Random(20261118)
    dtypes = ["uint16", "int16", "int32", "uint32", "float32", "int64", "uint64", "float64"]
    cases: list[Case] = []
    for k in range(16):
        params: dict[str, Any] = {}
        if rng.random() < 0.6:
            params["max_holding"] = rng.choice([8, 16, 32, 64, 100, 125, 200])
        if rng.random() < 0.4:
            params["max_input"] = rng.choice([4, 16, 64, 125])
        if rng.random() < 0.4:
            params["max_coils"] = rng.choice([8, 64, 256, 2000, 4000])
        if rng.random() < 0.5:
            params["max_gap"] = rng.choice([0, 1, 2, 4, 8, 20])
        if rng.random() < 0.3:
            params["max_gap_bits"] = rng.choice([0, 7, 32, 200])
        if rng.random() < 0.3:
            params["zero_based"] = False
        if rng.random() < 0.3:
            params["zero_based_bit"] = False
        tags: list[dict[str, Any]] = []
        for i in range(rng.randint(8, 40)):
            name = f"T{i:02d}"
            kind = rng.random()
            area = rng.choice("4443")
            ref = rng.randint(1, 300)
            if kind < 0.45:
                dt = rng.choice(dtypes)
                dims = rng.choice(["", "", "", f"[{rng.randint(1, 6)}]", f"[{rng.randint(1, 3)}][{rng.randint(1, 3)}]"])
                tags.append(T(name, f"{area}{ref:05d}{dims}", dt))
            elif kind < 0.6:
                tags.append(T(name, f"{area}{ref:05d}.{rng.randint(0, 16)}", "bool"))
            elif kind < 0.7:
                tags.append(T(name, f"{area}{ref:05d}.{rng.randint(1, 60):03d}{rng.choice('HL')}", "string"))
            elif kind < 0.85:
                tags.append(T(name, f"{rng.choice('01')}{rng.randint(1, 3000):05d}", "bool"))
            elif kind < 0.92 and tags:
                tags.append(T(name, rng.choice(tags)["address"], rng.choice(tags)["dtype"]))   # aliases, maybe bad
            else:
                tags.append(T(name, f"{area}{rng.randint(0, 300):05d}[{rng.randint(20, 140)}]", rng.choice(dtypes)))
            if rng.random() < 0.05:
                tags[-1]["enabled"] = False
        rng.shuffle(tags)
        cases.append(plan_case(f"mix-{k:02d}", f"Seeded random mix {k}: {len(tags)} tags of every address form, "
                               f"params {sorted(params) or 'default'}.", tags, **params))
    return cases


# --- modbus/decode -------------------------------------------------------------------------------

def dec_case(cid: str, desc: str, tags: list[dict[str, Any]], *, registers: list[int] | None = None,
             bits: list[bool] | None = None, **params: Any) -> Case:
    reply = {"bits": bits} if bits is not None else {"registers": registers}
    return cid, desc, {"params": params, "tags": tags, "reply": reply}


def one(cid: str, desc: str, address: str, dtype: str, data: list[int], **params: Any) -> Case:
    return dec_case(cid, desc, [T("X", address, dtype)], registers=data, **params)


def decode_integers() -> list[Case]:
    cases: list[Case] = []
    for w in (0, 1, 0x7FFF, 0x8000, 0xFFFF, 0x1234):
        cases.append(dec_case(f"u16-i16-{w:04x}", f"Register 0x{w:04X} as uint16 and int16 (two's complement).",
                              [T("U", "400001", "uint16"), T("I", "400001", "int16")], registers=[w]))
    cases.append(dec_case("u16-i16-byte-swapped", "byte_order_big off swaps the bytes of each register.",
                          [T("U", "400001", "uint16"), T("I", "400001", "int16")], registers=[0x0180],
                          byte_order_big=False))
    for bits_, label in ((0x12345678, "12345678"), (0xFFFFFFFE, "fffffffe"), (0x80000000, "80000000")):
        for big in (True, False):
            for fwl in (True, False):
                cases.append(dec_case(
                    f"i32-u32-{label}-{orders_id(big, fwl)}",
                    f"0x{label.upper()} as int32 and uint32, byte_order_big={big}, first_word_low={fwl}.",
                    [T("I", "400001", "int32"), T("U", "400001", "uint32")],
                    registers=regs(bits_, 2, big=big, fwl=fwl), **order_params(big, fwl)))
    for bits_, label in ((0x0102030405060708, "0102030405060708"), (0xFFFFFFFFFFFFFFFF, "ffffffffffffffff"),
                         (0x8000000000000001, "8000000000000001")):
        for big in (True, False):
            for fwl in (True, False):
                for fdl in (True, False):
                    if label != "0102030405060708" and not big:
                        continue
                    cases.append(dec_case(
                        f"i64-u64-{label}-{orders_id(big, fwl, fdl)}",
                        f"0x{label.upper()} as int64 and uint64, byte_order_big={big}, first_word_low={fwl}, "
                        f"first_dword_low={fdl}.",
                        [T("I", "400001", "int64"), T("U", "400001", "uint64")],
                        registers=regs(bits_, 4, big=big, fwl=fwl, fdl=fdl), **order_params(big, fwl, fdl)))
    for big in (True, False):
        for fwl in (True, False):
            cases.append(dec_case(
                f"fixed-regs-32-{orders_id(big, fwl)}",
                f"The same registers [0x4093, 0x4A45] as int32, uint32 and float32 with byte_order_big={big}, "
                f"first_word_low={fwl}: each order gives a different value.",
                [T("I", "400001", "int32"), T("U", "400001", "uint32"), T("F", "400001", "float32")],
                registers=[0x4093, 0x4A45], **order_params(big, fwl)))
            for fdl in (True, False):
                cases.append(dec_case(
                    f"fixed-regs-64-{orders_id(big, fwl, fdl)}",
                    f"The same registers [0x0102, 0x0304, 0x0506, 0x0708] as int64, uint64 and float64 with "
                    f"byte_order_big={big}, first_word_low={fwl}, first_dword_low={fdl}.",
                    [T("I", "400001", "int64"), T("U", "400001", "uint64"), T("F", "400001", "float64")],
                    registers=[0x0102, 0x0304, 0x0506, 0x0708], **order_params(big, fwl, fdl)))
    cases.append(one("u32-raw-default-order", "Default first_word_low: [0x0001, 0x0002] is 0x00020001.",
                     "400001", "uint32", [0x0001, 0x0002]))
    cases.append(one("u64-raw-default-order",
                     "Default orders: registers [1, 2, 3, 4] as uint64 are 0x0004000300020001.",
                     "400001", "uint64", [1, 2, 3, 4]))
    cases.append(one("input-register-int16", "Input registers (FC04) decode like holding registers.",
                     "300001", "int16", [0xFF9C]))
    return cases


def decode_floats() -> list[Case]:
    cases: list[Case] = []
    for x, label in ((1.0, "1"), (-2.5, "neg2.5"), (123.456, "123.456"), (math.pi, "pi"),
                     (3.4028234663852886e38, "max"), (1.1754943508222875e-38, "min-normal")):
        for big in (True, False):
            for fwl in (True, False):
                cases.append(one(f"f32-{label}-{orders_id(big, fwl)}",
                                 f"float32 {x!r} (rounded to float32), byte_order_big={big}, first_word_low={fwl}.",
                                 "400001", "float32", regs(f32_bits(x), 2, big=big, fwl=fwl), **order_params(big, fwl)))
    for x, label in ((1.0, "1"), (-1234.5678, "neg1234.5678"), (math.e, "e"), (1e300, "1e300"),
                     (2.2250738585072014e-308, "min-normal")):
        for big in (True, False):
            for fwl in (True, False):
                for fdl in (True, False):
                    if label not in ("1", "neg1234.5678") and not (big and fwl):
                        continue
                    cases.append(one(f"f64-{label}-{orders_id(big, fwl, fdl)}",
                                     f"float64 {x!r}, byte_order_big={big}, first_word_low={fwl}, "
                                     f"first_dword_low={fdl}.",
                                     "400001", "float64", regs(f64_bits(x), 4, big=big, fwl=fwl, fdl=fdl),
                                     **order_params(big, fwl, fdl)))
    cases.append(one("f32-raw-default-order", "Default first_word_low: [0x0000, 0x3F80] is float32 1.0.",
                     "400001", "float32", [0x0000, 0x3F80]))
    cases.append(one("f32-pymodbus-cross-check", "Registers [0x4093, 0x4A45] word-high: the value pymodbus' "
                     "convert_from_registers gives (big word order).", "400001", "float32", [0x4093, 0x4A45],
                     first_word_low=False))
    return cases


def decode_nonnormal() -> list[Case]:
    f32 = [(0x7FC00000, "qnan"), (0x7FA00000, "snan"), (0xFFC00000, "neg-nan"), (0x7F800000, "inf"),
           (0xFF800000, "neg-inf"), (0x00000001, "denorm-min"), (0x007FFFFF, "denorm-max"),
           (0x80000001, "neg-denorm"), (0x80000000, "neg-zero"), (0x00000000, "zero")]
    f64 = [(0x7FF8000000000000, "qnan"), (0x7FF0000000000000, "inf"), (0xFFF0000000000000, "neg-inf"),
           (0x0000000000000001, "denorm-min"), (0x000FFFFFFFFFFFFF, "denorm-max"),
           (0x8000000000000000, "neg-zero")]
    cases: list[Case] = []
    for mode in ("zero", "unmodified"):
        for b, label in f32:
            cases.append(one(f"f32-{label}-{mode}", f"float32 bits 0x{b:08X} with nonnormal_float={mode} "
                             "(NaN, Inf and denormals are replaced with 0.0 in zero mode; -0.0 is normal).",
                             "400001", "float32", regs(b, 2), nonnormal_float=mode))
        for b, label in f64:
            cases.append(one(f"f64-{label}-{mode}", f"float64 bits 0x{b:016X} with nonnormal_float={mode}.",
                             "400001", "float64", regs(b, 4), nonnormal_float=mode))
    cases.append(one("f32-array-nan-element", "Zero mode replaces only the non-normal element of an array.",
                     "400001[3]", "float32", regs(f32_bits(1.5), 2) + regs(0x7FC00000, 2) + regs(f32_bits(-2.0), 2)))
    cases.append(one("default-mode-is-zero", "Without nonnormal_float, +Inf decodes as 0.0 (Kepware default).",
                     "400001", "float32", regs(0x7F800000, 2)))
    return cases


def decode_bits() -> list[Case]:
    b_tags = [T(f"B{b}", f"400001.{b}", "bool") for b in (0, 3, 4, 8, 15)]
    return [
        dec_case("reg-bits-lsb0", "Bit .0 is the LSB of the register.", b_tags, registers=[0b1000_0001_0000_1001]),
        dec_case("reg-bits-modicon", "modicon_bit_order: bit .0 is the MSB.", b_tags, registers=[0b1000_0001_0000_1001],
                 modicon_bit_order=True),
        dec_case("reg-bits-one-based", "zero_based_bit off: .1 is the LSB, .16 the MSB.",
                 [T("B1", "400001.1", "bool"), T("B16", "400001.16", "bool"), T("B9", "400001.9", "bool")],
                 registers=[0x0001], zero_based_bit=False),
        dec_case("reg-bits-one-based-modicon", "zero_based_bit off with modicon_bit_order: .1 is the MSB.",
                 [T("B1", "400001.1", "bool"), T("B16", "400001.16", "bool")], registers=[0x8000],
                 zero_based_bit=False, modicon_bit_order=True),
        dec_case("reg-bits-byte-swapped", "Bits are taken after byte_order_big: 0x0100 swapped has bit 0 set.",
                 [T("B0", "400001.0", "bool"), T("B8", "400001.8", "bool")], registers=[0x0100], byte_order_big=False),
        dec_case("reg-bits-all-ones", "0xFFFF: every bit true.", b_tags, registers=[0xFFFF]),
        dec_case("reg-bits-and-word", "Bits and the word of the same register.",
                 [T("B2", "400001.2", "bool"), T("W", "400001", "int16")], registers=[0xFFFC]),
        dec_case("input-reg-bit", "A bit in an input register (FC04).", [T("B", "300001.15", "bool")],
                 registers=[0x8000]),
        dec_case("coils-padded", "Coils: the reply is padded to a multiple of 8 bits (as pymodbus gives it).",
                 [T("C1", "000001", "bool"), T("C3", "000003", "bool")],
                 bits=[True, True, False, False, False, False, False, False]),
        dec_case("discrete-unpadded", "Discrete inputs with an exact-length reply.",
                 [T("D1", "100001", "bool"), T("D2", "100002", "bool")], bits=[False, True]),
        dec_case("coil-array", "A coil array decodes to a list.", [T("CA", "000001[4]", "bool")],
                 bits=[True, False, True, True, False, False, False, False]),
        dec_case("coil-array-2d", "A [2][3] coil array decodes row by row.", [T("CA", "000001[2][3]", "bool")],
                 bits=[True, False, False, False, True, True, False, False]),
        dec_case("coils-with-gap", "Coils with a gap: offsets are relative to the block start.",
                 [T("C10", "000010", "bool"), T("C20", "000020", "bool")],
                 bits=[True] + [False] * 9 + [True] + [False] * 5),
    ]


def decode_strings() -> list[Case]:
    abcd = [0x4142, 0x4344]
    return [
        one("str-h", ".004H: first character in the high byte.", "400001.004H", "string", abcd),
        one("str-l", ".004L: first character in the low byte.", "400001.004L", "string", abcd),
        one("str-nul-cut", "The value is cut at the first NUL.", "400001.004H", "string", [0x4142, 0x0043]),
        one("str-nul-first", "A NUL first character gives an empty string.", "400001.004H", "string", [0x0041, 0x4243]),
        one("str-odd-length", ".003H: the low byte of the last register is not part of the value.",
            "400001.003H", "string", abcd),
        one("str-odd-length-l", ".003L: the high byte of the last register is not part of the value.",
            "400001.003L", "string", abcd),
        one("str-1", "A 1-byte string.", "400001.001H", "string", [0x5A00]),
        one("str-latin1", "Bytes above 0x7F decode as latin-1 (0xE9 is e-acute, 0xFF y-diaeresis).",
            "400001.004H", "string", [0xE9FF, 0xA020]),
        one("str-control-bytes", "Control bytes other than NUL are kept.", "400001.004H", "string", [0x0109, 0x0A7F]),
        one("str-byte-order-ignored", "byte_order_big does not affect strings; only the H/L suffix does.",
            "400001.004H", "string", abcd, byte_order_big=False),
        one("str-20", "A 20-byte string padded with spaces.", "400001.020H", "string",
            [0x5055, 0x4D50, 0x2031, 0x2052, 0x554E] + [0x2020] * 5),
        dec_case("str-with-neighbours", "A string between two words in one block.",
                 [T("W1", "400001"), T("S", "400002.004L", "string"), T("W2", "400004", "int16")],
                 registers=[7, 0x4241, 0x4443, 0xFFFF]),
    ]


def decode_arrays() -> list[Case]:
    return [
        one("u16-array", "A uint16 array.", "400001[3]", "uint16", [1, 2, 3]),
        one("i16-array-2d", "A [2][2] int16 array, row by row.", "400001[2][2]", "int16", [1, 2, 3, 0xFFFF]),
        one("f32-array", "A float32 array, default word order.", "400001[2]", "float32", [0, 0x3F80, 0, 0x4000]),
        one("f32-array-2d", "A [2][3] float32 array.", "400001[2][3]", "float32",
            [w for x in (1.0, 2.0, 3.0, -1.0, 0.5, 100.0) for w in regs(f32_bits(x), 2)]),
        one("u32-array-fwh", "A uint32 array with first_word_low off.", "400001[2]", "uint32",
            [0x0001, 0x0000, 0xFFFF, 0xFFFF], first_word_low=False),
        one("i64-array", "An int64 array.", "400001[2]", "int64", regs(1, 4) + regs((1 << 64) - 2, 4)),
        one("f64-array-swapped", "A float64 array with every order flipped.", "400001[2]", "float64",
            regs(f64_bits(0.1), 4, big=False, fwl=False, fdl=False)
            + regs(f64_bits(-0.2), 4, big=False, fwl=False, fdl=False),
            byte_order_big=False, first_word_low=False, first_dword_low=False),
        one("array-1", "A [1] array is a one-element list, not a scalar.", "400001[1]", "int16", [0xFFFF]),
        one("i16-array-byte-swapped", "An int16 array with swapped bytes.", "400001[3]", "int16",
            [0x0100, 0xFFFF, 0x0080], byte_order_big=False),
    ]


def decode_block_items() -> list[Case]:
    return [
        dec_case("aliases-same-value", "Aliases get the same value; offsets are relative to the block start.",
                 [T("Y", "400133", "float32"), T("B", "400133", "float32"), T("W", "400136")],
                 registers=[0, 0x3F80, 0, 7]),
        dec_case("overlap-types", "Overlapping uint16, int32 and float32 at one start decode independently.",
                 [T("U", "400001", "uint16"), T("I", "400001", "int32"), T("F", "400001", "float32")],
                 registers=[0x0000, 0xBF80]),
        dec_case("gap-registers-ignored", "Registers in a gap are read but not decoded.",
                 [T("A", "400001"), T("B", "400005", "int16")], registers=[10, 0xDEAD, 0xBEEF, 0xCAFE, 0xFFFE]),
        dec_case("mixed-block", "A block mixing every register type.",
                 [T("I16", "400001", "int16"), T("U32", "400002", "uint32"), T("F32", "400004", "float32"),
                  T("F64", "400006", "float64"), T("S", "400010.004H", "string"), T("B", "400012.1", "bool")],
                 registers=[0xFFFF, 0x0002, 0x0001, *regs(f32_bits(-0.75), 2), *regs(f64_bits(6.25), 4),
                            0x4F4B, 0x2121, 0x0002]),
        dec_case("zero-based-off", "zero_based off shifts the block start but not the decoding.",
                 [T("A", "400001"), T("B", "400002", "int16")], registers=[5, 0x8000], zero_based=False),
        dec_case("disabled-not-decoded", "A disabled tag is not in the plan, so it gets no value.",
                 [T("A", "400001"), T("OFF", "400002", enabled=False)], registers=[42]),
        dec_case("scale-not-applied", "The decoder never scales (scaling is a separate step).",
                 [T("S", "400001", "uint16", scale={"raw_lo": 0, "raw_hi": 10, "eng_lo": 0, "eng_hi": 100})],
                 registers=[5]),
    ]


def decode_short() -> list[Case]:
    return [
        dec_case("short-fails-past-end", "A short reply fails only the tags past its end.",
                 [T("A", "400001"), T("F", "400002", "float32")], registers=[42, 0]),
        dec_case("short-empty", "An empty reply fails every tag.", [T("A", "400001"), T("B", "400002")],
                 registers=[]),
        dec_case("short-exact", "A reply of exactly the block length decodes everything.",
                 [T("A", "400001"), T("B", "400002")], registers=[1, 2]),
        dec_case("long-reply", "Extra registers after the block are ignored.", [T("A", "400001")],
                 registers=[1, 2, 3]),
        dec_case("short-string", "A string past the end of a short reply fails.",
                 [T("A", "400001"), T("S", "400002.006H", "string")], registers=[1, 0x4142]),
        dec_case("short-array", "An array that does not fit a short reply fails as a whole.",
                 [T("ARR", "400001[4]")], registers=[1, 2, 3]),
        dec_case("short-bits", "A short coil reply fails the coils past its end.",
                 [T("C1", "000001", "bool"), T("C9", "000009", "bool")], bits=[True]),
        dec_case("short-alias", "Aliases of a failed item all get the same decode error.",
                 [T("A", "400002"), T("A2", "400002"), T("Z", "400001")], registers=[9]),
    ]


def decode_random() -> list[Case]:
    """Seeded random registers under random orders: whatever bit patterns come up (NaNs,
    denormals, sign bits) must decode identically."""
    rng = random.Random(20261119)
    dtypes = ["uint16", "int16", "int32", "uint32", "float32", "int64", "uint64", "float64"]
    cases: list[Case] = []
    for k in range(16):
        params: dict[str, Any] = {"max_holding": 125, "max_gap": 125}
        for name in ("byte_order_big", "first_word_low", "first_dword_low", "modicon_bit_order", "zero_based_bit"):
            if rng.random() < 0.5:
                params[name] = rng.random() < 0.5
        if rng.random() < 0.4:
            params["nonnormal_float"] = "unmodified"
        tags: list[dict[str, Any]] = []
        for i in range(rng.randint(3, 12)):
            ref = rng.randint(1, 40)
            r = rng.random()
            if r < 0.6:
                dims = rng.choice(["", "", f"[{rng.randint(1, 3)}]"])
                tags.append(T(f"T{i:02d}", f"4{ref:05d}{dims}", rng.choice(dtypes)))
            elif r < 0.8:
                lo, hi = (0, 15) if params.get("zero_based_bit", True) else (1, 16)
                tags.append(T(f"T{i:02d}", f"4{ref:05d}.{rng.randint(lo, hi)}", "bool"))
            else:
                tags.append(T(f"T{i:02d}", f"4{ref:05d}.{rng.randint(1, 12):03d}{rng.choice('HL')}", "string"))
        span = max(int(t["address"][1:6]) + 12 for t in tags)
        data = [rng.choice([rng.randrange(0x10000), 0, 0xFFFF, 0x7F80, 0x8000, 0x0001]) for _ in range(span)]
        inp: dict[str, Any] = {"params": params, "tags": tags, "reply": {"registers": data}}
        driver_plan = reference.modbus_plan(inp)
        block = driver_plan["blocks"][0]
        inp["reply"]["registers"] = data[block["start"]:block["start"] + block["count"]]
        cases.append((f"mix-{k:02d}", f"Seeded random registers {k}: {len(tags)} tags, "
                      f"params {sorted(set(params) - {'max_holding', 'max_gap'}) or 'default orders'}.", inp))
    return cases


# --- scaling -------------------------------------------------------------------------------------

def plain_number(x: float) -> bool:
    """True if every JSON parser reads `x` back exactly: at most 15 significant digits and a
    decimal exponent within +-22 (Clinger's fast path, which needs no big-number arithmetic;
    serde_json without `float_roundtrip` is exact only there)."""
    sign, digits, exp = Decimal(repr(float(x))).normalize().as_tuple()
    return isinstance(exp, int) and len(digits) <= 15 and -22 <= exp <= 22


def scale(**kw: Any) -> dict[str, Any]:
    """A full Scale (every field explicit, no reliance on defaults) as plain JSON numbers."""
    out = Scale(**kw).model_dump(mode="json")
    bad = [k for k, v in out.items() if isinstance(v, float) and not plain_number(v)]
    assert not bad, f"scale fields {bad} are not plain JSON numbers; tag values carry exact floats instead"
    return out


def sc_case(cid: str, desc: str, sc: dict[str, Any], raw: Any) -> Case:
    return cid, desc, {"scale": sc, "raw": encode_value(raw)}


def scaling_linear() -> list[Case]:
    ma = scale(raw_lo=4, raw_hi=20, eng_lo=0, eng_hi=100, units="%")
    cnt = scale(raw_lo=0, raw_hi=32767, eng_lo=0, eng_hi=100, units="bar")
    return [
        sc_case("defaults-identity", "Kepware default scale (0..1000 -> 0..1000) returns the raw as a float.",
                scale(), 500),
        sc_case("ma-4", "4-20 mA to 0-100 %: 4 mA is 0.", ma, 4),
        sc_case("ma-12", "4-20 mA: 12 mA is 50.", ma, 12),
        sc_case("ma-20", "4-20 mA: 20 mA is 100.", ma, 20.0),
        sc_case("ma-below-range", "Below raw_lo without clamps extrapolates (negative).", ma, 2),
        sc_case("ma-above-range", "Above raw_hi without clamps extrapolates.", ma, 24.5),
        sc_case("counts-mid", "0..32767 counts to 0..100 bar: 16384.", cnt, 16384),
        sc_case("counts-int16-negative", "A negative int16 raw extrapolates below eng_lo.", cnt, -32768),
        sc_case("counts-float-raw", "A float raw value.", cnt, 1234.5),
        sc_case("inverted-eng", "eng_hi < eng_lo: the line slopes down.",
                scale(raw_lo=0, raw_hi=100, eng_lo=50, eng_hi=-50), 25),
        sc_case("inverted-raw", "raw_hi < raw_lo is allowed.",
                scale(raw_lo=100, raw_hi=0, eng_lo=0, eng_hi=10), 30),
        sc_case("negative-ranges", "Negative raw and engineering ranges.",
                scale(raw_lo=-10, raw_hi=-2, eng_lo=-40, eng_hi=-20), -6),
        sc_case("fraction-rounding", "A result that is not exact in binary (0.1 steps).",
                scale(raw_lo=0, raw_hi=3, eng_lo=0, eng_hi=0.3), 1),
        sc_case("uint64-max-raw", "uint64 max converts to the nearest double before scaling.",
                scale(raw_lo=0, raw_hi=1000, eng_lo=0, eng_hi=1), (1 << 64) - 1),
        sc_case("int64-min-raw", "int64 min converts exactly (a power of two).",
                scale(raw_lo=0, raw_hi=1, eng_lo=0, eng_hi=1), -(1 << 63)),
        sc_case("raw-zero-int", "Raw 0 (int) gives a float result.", scale(raw_lo=0, raw_hi=10, eng_lo=0, eng_hi=1), 0),
        sc_case("eng-span-zero", "eng_hi == eng_lo is allowed: every raw maps to eng_lo.",
                scale(raw_lo=0, raw_hi=10, eng_lo=7, eng_hi=7), 3),
        sc_case("half-scale-exact", "Exactly half scale.", scale(raw_lo=0, raw_hi=2, eng_lo=0, eng_hi=1), 1),
        sc_case("large-span", "Large engineering span.", scale(raw_lo=0, raw_hi=65535, eng_lo=-1e6, eng_hi=1e6), 40000),
    ]


def scaling_sqrt() -> list[Case]:
    sq = scale(type="sqrt", raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=50)
    return [
        sc_case("sqrt-quarter", "Square root: a quarter of the raw span is half the engineering span.", sq, 25),
        sc_case("sqrt-full", "Square root at raw_hi.", sq, 100),
        sc_case("sqrt-zero", "Square root at raw_lo.", sq, 0),
        sc_case("sqrt-below", "Square root below raw_lo: the fraction is taken as 0 (eng_lo), never NaN.", sq, -10),
        sc_case("sqrt-above", "Square root above raw_hi extrapolates (sqrt of a fraction > 1).", sq, 400),
        sc_case("sqrt-irrational", "Square root of 0.5.", sq, 50),
        sc_case("sqrt-offset", "Square root with offset ranges.",
                scale(type="sqrt", raw_lo=4, raw_hi=20, eng_lo=10, eng_hi=110), 8),
        sc_case("sqrt-inverted-eng", "Square root with eng_hi < eng_lo.",
                scale(type="sqrt", raw_lo=0, raw_hi=1, eng_lo=100, eng_hi=0), 0.36),
        sc_case("sqrt-tiny", "Square root of a tiny positive fraction.", sq, 1e-10),
    ]


def scaling_clamp_negate() -> list[Case]:
    cl = scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, clamp_lo=True, clamp_hi=True)
    inv = scale(raw_lo=0, raw_hi=100, eng_lo=10, eng_hi=0, clamp_lo=True, clamp_hi=True)
    return [
        sc_case("clamp-lo", "clamp_lo holds a value below the range at the low end.", cl, -50),
        sc_case("clamp-hi", "clamp_hi holds a value above the range at the high end.", cl, 150),
        sc_case("clamp-inside", "Clamps do not change a value inside the range.", cl, 42),
        sc_case("clamp-lo-only", "Only clamp_lo: above the range still extrapolates.",
                scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, clamp_lo=True), 150),
        sc_case("clamp-hi-only", "Only clamp_hi: below the range still extrapolates.",
                scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, clamp_hi=True), -50),
        sc_case("clamp-inverted-lo", "Inverted engineering range: clamps use the sorted limits (low side).", inv, 150),
        sc_case("clamp-inverted-hi", "Inverted engineering range: clamps use the sorted limits (high side).", inv, -50),
        sc_case("negate", "negate flips the sign of the scaled value.",
                scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, negate=True), 30),
        sc_case("negate-after-clamp", "negate applies after the clamp.",
                scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, clamp_hi=True, negate=True), 500),
        sc_case("negate-zero", "negate of 0.0 is -0.0 (the payload later writes it as 0.0).",
                scale(raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, negate=True), 0),
        sc_case("negate-sqrt", "negate with square root.",
                scale(type="sqrt", raw_lo=0, raw_hi=100, eng_lo=0, eng_hi=10, negate=True), 64),
        sc_case("clamp-inf", "+Inf raw is clamped to eng_hi.", cl, math.inf),
        sc_case("clamp-neg-inf", "-Inf raw is clamped to eng_lo.", cl, -math.inf),
    ]


def scaling_special() -> list[Case]:
    zero_span = scale(raw_lo=5, raw_hi=5, eng_lo=0, eng_hi=100)
    lin = scale(raw_lo=0, raw_hi=10, eng_lo=0, eng_hi=100)
    return [
        sc_case("zero-span-error", "raw_hi == raw_lo is an error (the tag gets bad_decode).", zero_span, 5),
        sc_case("zero-span-list-error", "raw_hi == raw_lo fails an array as a whole.", zero_span, [1, 2]),
        sc_case("zero-span-bool-passes", "A bool is never scaled, so even a broken scale passes it through.",
                zero_span, True),
        sc_case("zero-span-empty-list", "An empty array has nothing to scale: no error.", zero_span, []),
        sc_case("bool-passes", "bool values pass through unscaled.", lin, False),
        sc_case("str-passes", "Strings pass through unscaled.", lin, "RUN"),
        sc_case("none-passes", "No value passes through.", lin, None),
        sc_case("list", "Arrays scale element by element.", lin, [0, 1, 5, 10]),
        sc_case("list-2d", "2-D arrays scale element by element.", lin, [[0, 1], [2.5, -1]]),
        sc_case("list-mixed", "In an array only numbers are scaled; bools and strings pass through.",
                lin, [1, True, "x", 2.0]),
        sc_case("nan-linear", "NaN raw stays NaN on a linear scale.", lin, math.nan),
        sc_case("nan-clamped", "NaN is not clamped (comparisons with NaN are false).",
                scale(raw_lo=0, raw_hi=10, eng_lo=0, eng_hi=100, clamp_lo=True, clamp_hi=True), math.nan),
        sc_case("nan-sqrt", "NaN raw on a square-root scale gives eng_lo (the fraction is not > 0).",
                scale(type="sqrt", raw_lo=0, raw_hi=10, eng_lo=3, eng_hi=100), math.nan),
        sc_case("inf-linear", "+Inf raw gives +Inf without clamps.", lin, math.inf),
        sc_case("inf-sqrt", "+Inf raw on a square-root scale gives +Inf.",
                scale(type="sqrt", raw_lo=0, raw_hi=10, eng_lo=0, eng_hi=100), math.inf),
        sc_case("neg-zero-raw", "-0.0 raw scales like 0.", lin, -0.0),
        sc_case("huge-overflow", "A huge result overflows to +Inf without an error.",
                scale(raw_lo=0, raw_hi=0.5, eng_lo=0, eng_hi=1000), 1e308),
    ]


# --- payload -------------------------------------------------------------------------------------

T0 = datetime(2026, 11, 16, 10, 15, 1, 123456, tzinfo=UTC)


def pl_device(tags: list[tuple[str, str] | tuple[str, str, bool]], *, sub: str | None = "SUB_001",
              env: str = "prod", channel: str = "Plant A", name: str = "PLC1") -> Device:
    tag_objs = [Tag(name=t[0], address="400001", dtype=t[1], enabled=t[2] if len(t) > 2 else True)  # type: ignore[misc]
                for t in tags]
    return Device(id=f"{channel}/{name}", channel=channel, name=name, driver="modbus_tcp", host="192.0.2.10",
                  port=502, tags=tag_objs, publish=PublishCfg(sub_device_id=sub, env=env))


def tv(value: Any, quality: str = "good", ts: datetime | None | str = "T0") -> dict[str, Any]:
    when = T0 if ts == "T0" else ts
    return {"value": encode_value(value), "quality": quality,
            "source_ts": when.isoformat() if isinstance(when, datetime) else None}


def pl_case(cid: str, desc: str, device: Device, values: dict[str, dict[str, Any]], *,
            cfg: PayloadCfg | None = None, is_secure: bool = False, now: datetime = T0) -> Case:
    cfg = cfg or PayloadCfg()
    return cid, desc, {
        "firm_version": FIRM_VERSION,
        "device": device.model_dump(mode="json", exclude_defaults=True),
        "values": values,
        "cfg": cfg.model_dump(mode="json"),
        "is_secure": is_secure,
        "now": now.isoformat(),
    }


def payload_keys() -> list[Case]:
    three = pl_device([("FLOW_1", "float32"), ("RUN", "bool"), ("COUNT", "uint32")])
    good3 = {"FLOW_1": tv(12.5), "RUN": tv(True), "COUNT": tv(7)}
    return [
        pl_case("all-good", "Key order SubDeviceId, FirmVersion, Env, isSecure, DataValid, Data, DateTime; "
                "no BadTags when every tag is good.", three, good3),
        pl_case("is-secure", "isSecure follows the link's TLS setting.", three, good3, is_secure=True),
        pl_case("sub-device-null", "A device without a SubDeviceId sends JSON null.",
                pl_device([("A", "int16")], sub=None), {"A": tv(1)}),
        pl_case("env-test", "Env comes from device.publish.env.", pl_device([("A", "int16")], env="test"),
                {"A": tv(-1)}),
        pl_case("data-in-device-order", "Data keeps the device's tag order, not alphabetical order.",
                pl_device([("Z", "int16"), ("A", "int16"), ("M", "int16")]),
                {"A": tv(1), "M": tv(2), "Z": tv(3)}),
        pl_case("no-tags", "A device with no tags: empty Data, DataValid true, DateTime is now.",
                pl_device([]), {}),
        pl_case("non-ascii", "Non-ASCII names, values and ids are written as raw UTF-8, not \\u escapes.",
                pl_device([("Temp °C", "float32"), ("ünïcødé", "string"), ("😀_EMOJI", "string")],
                          sub="ünï \"sub\"", env="stäging"),
                {"Temp °C": tv(21.5), "ünïcødé": tv("ÿ latin-1 \xa0"), "😀_EMOJI": tv("😀")}),
        pl_case("escapes", "Quotes, backslashes and control characters are JSON-escaped; DEL and U+2028 are not.",
                pl_device([('quo"te', "string"), ("back\\slash", "string"), ("tab\tname", "string"),
                           ("ctl\x01", "string")]),
                {'quo"te': tv('say "hi"'), "back\\slash": tv("a\\b"), "tab\tname": tv("line\nbreak\r\t\b\f"),
                 "ctl\x01": tv("\x00\x1f\x7f ")}),
        pl_case("strings", "String values, including empty.", pl_device([("S", "string"), ("E", "string")]),
                {"S": tv("RUN"), "E": tv("")}),
        pl_case("bool-not-int", "bool stays a JSON bool, int stays an int.",
                pl_device([("B", "bool"), ("I", "int16"), ("F", "float32")]),
                {"B": tv(False), "I": tv(0), "F": tv(0.0)}),
        pl_case("big-ints", "64-bit integers are written exactly, never as floats.",
                pl_device([("U", "uint64"), ("I", "int64")]), {"U": tv((1 << 64) - 1), "I": tv(-(1 << 63))}),
        pl_case("arrays", "Array tags are JSON arrays, cleaned element by element.",
                pl_device([("A", "float32"), ("M", "int16"), ("C", "bool")]),
                {"A": tv([1.23456, -0.0, 2.0]), "M": tv([[1, 2], [3, 4]]), "C": tv([True, False])}),
        pl_case("empty-array", "An empty array is published as [].", pl_device([("A", "int16")]), {"A": tv([])}),
    ]


def payload_rounding() -> list[Case]:
    def r(cid: str, desc: str, x: float, decimals: int) -> Case:
        return pl_case(cid, desc, pl_device([("X", "float64")]), {"X": tv(x)}, cfg=PayloadCfg(float_decimals=decimals))
    return [
        r("tie-0.125-2", "round(0.125, 2) is 0.12 (half to even on the exact binary value).", 0.125, 2),
        r("tie-0.375-2", "round(0.375, 2) is 0.38.", 0.375, 2),
        r("tie-2.675-2", "2.675 is just below the tie in binary: 2.67.", 2.675, 2),
        r("tie-1.005-2", "1.005 is just below the tie in binary: 1.0.", 1.005, 2),
        r("tie-0.5-0", "round(0.5, 0) is 0.0.", 0.5, 0),
        r("tie-1.5-0", "round(1.5, 0) is 2.0.", 1.5, 0),
        r("tie-2.5-0", "round(2.5, 0) is 2.0.", 2.5, 0),
        r("neg-tie", "round(-0.125, 2) is -0.12.", -0.125, 2),
        r("decimals-4-default", "Default float_decimals 4.", 123456.789012, 4),
        r("neg-rounds-to-zero", "-0.00004 rounds to -0.0, which is written 0.0.", -0.00004, 4),
        r("neg-zero", "-0.0 is written 0.0.", -0.0, 4),
        r("whole-float", "A whole float keeps its .0.", 12.0, 4),
        r("exp-large", "1e16 is written in Python's repr form 1e+16.", 1e16, 4),
        r("exp-1.5e17", "1.5e17 is written 1.5e+17.", 1.5e17, 2),
        r("exp-small", "1e-5 at 6 decimals is written 1e-05.", 1e-5, 6),
        r("denorm-to-zero", "The smallest denormal rounds to 0.0.", 5e-324, 4),
        r("max-double", "The largest double survives rounding.", 1.7976931348623157e308, 4),
        r("below-tie-no-carry", "99.99995 is just below the tie in binary: 99.9999, no carry.", 99.99995, 4),
        r("carry", "9.99996 at 4 decimals carries into the integer part: 10.0.", 9.99996, 4),
        r("point-three", "0.1 + 0.2 at 4 decimals is 0.3.", 0.1 + 0.2, 4),
        r("float32-widened", "A float32 widened to double (2.56f) at 4 decimals.", 2.5600000381469727, 4),
        r("decimals-0", "float_decimals 0 keeps a float (4.0, not 4).", 3.7, 0),
        r("decimals-10", "float_decimals 10.", 1.23456789e-7, 10),
        r("decimals-negative", "Negative float_decimals rounds to tens: round(15.0, -1) is 20.0.", 15.0, -1),
        r("decimals-negative-2", "round(1234.5, -2) is 1200.0.", 1234.5, -2),
        r("17-digits", "A double with 17 significant digits is unchanged by 4-decimal rounding and written in "
          "shortest round-trip form.", 1234567890123456.7, 4),
        pl_case("ints-not-rounded", "Integers are never rounded, whatever float_decimals is.",
                pl_device([("I", "int32")]), {"I": tv(123456789)}, cfg=PayloadCfg(float_decimals=-2)),
        pl_case("array-rounding", "Each array element is rounded.", pl_device([("A", "float32")]),
                {"A": tv([0.125, 2.675, -0.00001])}, cfg=PayloadCfg(float_decimals=2)),
    ]


def payload_quality() -> list[Case]:
    dev = pl_device([("A", "int16"), ("B", "int16"), ("C", "int16"), ("D", "int16"), ("E", "int16")])
    return [
        pl_case("bad-tags-sorted", "Bad tags are left out of Data and listed sorted in BadTags; DataValid false.",
                pl_device([("Z", "int16"), ("A", "int16"), ("M", "int16")]),
                {"Z": tv(None, "bad_comm", None), "A": tv(5, "stale"), "M": tv(1)}),
        pl_case("every-bad-quality", "bad_comm, bad_config, bad_decode and stale all count as bad.", dev,
                {"A": tv(1), "B": tv(2, "bad_comm"), "C": tv(None, "bad_config", None), "D": tv(4, "bad_decode"),
                 "E": tv(5, "stale")}),
        pl_case("missing-is-bad", "A tag missing from the snapshot is bad.", dev,
                {"A": tv(1), "B": tv(2), "C": tv(3)}),
        pl_case("good-none-is-bad", "A good tag with no value is bad.", dev,
                {"A": tv(1), "B": tv(None), "C": tv(3), "D": tv(4), "E": tv(5)}),
        pl_case("nan-is-bad", "NaN and infinite floats make the tag bad.",
                pl_device([("N", "float32"), ("P", "float32"), ("M", "float32"), ("OK", "float32")]),
                {"N": tv(math.nan), "P": tv(math.inf), "M": tv(-math.inf), "OK": tv(1.0)}),
        pl_case("nan-in-array-is-bad", "A NaN element makes the whole array tag bad.",
                pl_device([("A", "float32"), ("B", "float32")]), {"A": tv([1.0, math.nan]), "B": tv([[1.0], [2.0]])}),
        pl_case("bad-tags-field-off", "bad_tags_field off: no BadTags key, but DataValid is still false.", dev,
                {"A": tv(1), "B": tv(2, "bad_comm")}, cfg=PayloadCfg(bad_tags_field=False)),
        pl_case("disabled-ignored", "Disabled tags are in neither Data nor BadTags and do not affect DataValid.",
                pl_device([("ON", "int16"), ("OFF", "int16", False), ("OFF2", "int16", False)]),
                {"ON": tv(1), "OFF": tv(2)}),
        pl_case("all-bad", "No good tag: empty Data, DataValid false, DateTime is now.",
                pl_device([("A", "int16"), ("B", "int16")]),
                {"A": tv(None, "stale", None), "B": tv(None, "bad_comm", None)},
                now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        pl_case("unknown-values-ignored", "Snapshot entries for names that are not tags of the device are ignored.",
                pl_device([("A", "int16")]), {"A": tv(1), "GHOST": tv(2)}),
    ]


def payload_datetime() -> list[Case]:
    two = pl_device([("A", "int16"), ("B", "int16")])

    def z(cid: str, desc: str, when: datetime, tz: str) -> Case:
        return pl_case(cid, desc, pl_device([("A", "int16")]), {"A": tv(1, ts=when)}, cfg=PayloadCfg(timezone=tz))

    return [
        pl_case("newest-good-ts", "DateTime is the newest source_ts among the tags in Data.", two,
                {"A": tv(1, ts=T0), "B": tv(2, ts=T0 + timedelta(seconds=90))}),
        pl_case("bad-ts-ignored", "A bad tag's newer timestamp is ignored.", two,
                {"A": tv(1, ts=T0), "B": tv(2, "bad_comm", T0 + timedelta(hours=1))}),
        pl_case("good-without-ts", "A good tag without source_ts does not count; the other tag's time is used.", two,
                {"A": tv(1, ts=None), "B": tv(2, ts=T0 - timedelta(days=1))}),
        pl_case("good-all-without-ts", "Good tags but none with source_ts: DateTime is now.", two,
                {"A": tv(1, ts=None), "B": tv(2, ts=None)}, now=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)),
        pl_case("micro-truncated", "Microseconds are truncated, not rounded.", pl_device([("A", "int16")]),
                {"A": tv(1, ts=datetime(2026, 11, 16, 10, 15, 1, 999999, tzinfo=UTC))}),
        z("kolkata", "Default zone Asia/Kolkata (+05:30).", T0, "Asia/Kolkata"),
        z("kolkata-date-rollover", "Asia/Kolkata rolls the date past midnight.",
          datetime(2026, 12, 31, 20, 0, 0, tzinfo=UTC), "Asia/Kolkata"),
        z("utc", "UTC.", T0, "UTC"),
        z("new-york-standard", "America/New_York in winter (-05:00).", datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
          "America/New_York"),
        z("new-york-dst", "America/New_York in summer (-04:00).", datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC),
          "America/New_York"),
        z("new-york-fold-first", "America/New_York: the first 01:30 of the DST end (EDT).",
          datetime(2026, 11, 1, 5, 30, 0, tzinfo=UTC), "America/New_York"),
        z("new-york-fold-second", "America/New_York: the second 01:30 of the DST end (EST).",
          datetime(2026, 11, 1, 6, 30, 0, tzinfo=UTC), "America/New_York"),
        z("new-york-spring-gap", "America/New_York: the instant after the spring-forward gap.",
          datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC), "America/New_York"),
        z("london-bst", "Europe/London in summer (BST, +01:00).", datetime(2026, 8, 1, 23, 30, 0, tzinfo=UTC),
          "Europe/London"),
        z("kathmandu", "Asia/Kathmandu (+05:45).", T0, "Asia/Kathmandu"),
        z("lord-howe-summer", "Australia/Lord_Howe in its summer (+11:00).", datetime(2026, 1, 10, 0, 0, 0, tzinfo=UTC),
          "Australia/Lord_Howe"),
        z("lord-howe-winter", "Australia/Lord_Howe in its winter (+10:30).", datetime(2026, 7, 10, 0, 0, 0, tzinfo=UTC),
          "Australia/Lord_Howe"),
        pl_case("offset-input", "A source_ts given with a +05:30 offset is the same instant as UTC 10:15:01.",
                pl_device([("A", "int16")]),
                {"A": {**tv(1), "source_ts": "2026-11-16T15:45:01.500000+05:30"}}, cfg=PayloadCfg(timezone="UTC")),
        pl_case("now-in-zone", "The now fallback is written in the configured zone too.",
                pl_device([("A", "int16")]), {"A": tv(None, "stale", None)},
                cfg=PayloadCfg(timezone="Asia/Kathmandu"), now=datetime(2026, 2, 28, 18, 14, 59, tzinfo=UTC)),
    ]


# --- files ---------------------------------------------------------------------------------------

# --- payload/iotgw -------------------------------------------------------------------------------

def iot_case(cid: str, desc: str, device: Device, values: dict[str, dict[str, Any]], *,
             cfg: PayloadCfg | None = None, now: datetime = T0) -> Case:
    _, _, inp = pl_case(cid, desc, device, values, cfg=cfg, now=now)
    del inp["firm_version"], inp["is_secure"]
    return cid, desc, inp


def payload_iotgw_shape() -> list[Case]:
    three = pl_device([("FLOW_1", "float32"), ("RUN", "bool"), ("COUNT", "uint32")])
    good3 = {"FLOW_1": tv(12.5), "RUN": tv(True), "COUNT": tv(7)}
    t1 = datetime(2026, 11, 16, 10, 15, 0, 987654, tzinfo=UTC)
    return [
        iot_case("all-good", "Standard template: timestamp, then values of id, v, q, t (ms since the epoch).",
                 three, good3),
        iot_case("id-is-channel-device-tag", "id is Channel.Device.Tag with the names as configured.",
                 pl_device([("A", "int16")], channel="Plant B", name="Line 2 PLC"), {"A": tv(1)}),
        iot_case("group-path-in-tag-name", "A tag name that already holds a group path is appended as is.",
                 pl_device([("LTI002.Pv", "float32")]), {"LTI002.Pv": tv(1.5)}),
        iot_case("values-in-device-order", "values keep the device's tag order.",
                 pl_device([("Z", "int16"), ("A", "int16"), ("M", "int16")]),
                 {"A": tv(1), "M": tv(2), "Z": tv(3)}),
        iot_case("disabled-not-listed", "Disabled tags are not in values.",
                 pl_device([("ON", "int16"), ("OFF", "int16", False)]), {"ON": tv(1), "OFF": tv(2)}),
        iot_case("no-tags", "A device with no tags: empty values, timestamp is now.", pl_device([]), {}),
        iot_case("t-is-source-ts", "t is each tag's own source timestamp, timestamp is the publish time.",
                 pl_device([("A", "int16"), ("B", "int16")]),
                 {"A": tv(1, ts=t1), "B": tv(2)}, now=T0 + timedelta(seconds=2)),
        iot_case("t-floors-to-ms", "Sub-millisecond parts are cut, never rounded up.",
                 pl_device([("A", "int16")]),
                 {"A": tv(1, ts=datetime(2026, 11, 16, 10, 15, 0, 999999, tzinfo=UTC))}),
        iot_case("t-non-utc-offset", "A source_ts with a UTC offset is converted to epoch time.",
                 pl_device([("A", "int16")]), {"A": {**tv(1), "source_ts": "2026-11-16T15:45:01.123456+05:30"}}),
        iot_case("timezone-setting-ignored", "PayloadCfg.timezone does not move epoch times.",
                 pl_device([("A", "int16")]), {"A": tv(1)}, cfg=PayloadCfg(timezone="UTC")),
        iot_case("types", "bool stays a JSON bool, int an int, float a float, string a string.",
                 pl_device([("B", "bool"), ("I", "int16"), ("F", "float32"), ("S", "string")]),
                 {"B": tv(False), "I": tv(0), "F": tv(0.0), "S": tv("RUN")}),
        iot_case("big-ints", "64-bit integers are written exactly.",
                 pl_device([("U", "uint64"), ("I", "int64")]), {"U": tv((1 << 64) - 1), "I": tv(-(1 << 63))}),
        iot_case("arrays", "Array tags are JSON arrays, cleaned element by element.",
                 pl_device([("A", "float32"), ("M", "int16")]),
                 {"A": tv([1.23456, -0.0, 2.0]), "M": tv([[1, 2], [3, 4]])}),
        iot_case("rounding", "Floats are rounded to float_decimals places, -0.0 becomes 0.0.",
                 pl_device([("A", "float32"), ("B", "float32"), ("C", "float64")]),
                 {"A": tv(123.40000152587891), "B": tv(-0.00001), "C": tv(2.675)}, cfg=PayloadCfg(float_decimals=2)),
        iot_case("non-ascii-and-escapes", "Non-ASCII is raw UTF-8; quotes and control characters are escaped.",
                 pl_device([("Temp °C", "float32"), ('quo"te', "string")], channel="Ünï", name='PLC "1"'),
                 {"Temp °C": tv(21.5), 'quo"te': tv("a\nb\\")}),
    ]


def payload_iotgw_quality() -> list[Case]:
    dev = pl_device([("A", "int16"), ("B", "int16"), ("C", "int16"), ("D", "int16"), ("E", "int16")])
    return [
        iot_case("every-bad-quality",
                 "bad_comm, bad_config, bad_decode and stale give q false; the last value is kept.",
                 dev, {"A": tv(1), "B": tv(2, "bad_comm"), "C": tv(None, "bad_config", None),
                       "D": tv(4, "bad_decode"), "E": tv(5, "stale")}),
        iot_case("missing-tag", "A tag missing from the snapshot is listed with v null, q false, t = timestamp.",
                 dev, {"A": tv(1)}),
        iot_case("never-read", "A tag never read (no value, no source_ts) is v null, q false, t = timestamp.",
                 pl_device([("A", "int16")]), {"A": tv(None, "stale", None)},
                 now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        iot_case("good-none", "A good tag with no value is q false.", pl_device([("A", "int16")]), {"A": tv(None)}),
        iot_case("nan-and-inf", "NaN and infinite floats are v null, q false.",
                 pl_device([("N", "float32"), ("P", "float32"), ("OK", "float32")]),
                 {"N": tv(math.nan), "P": tv(math.inf), "OK": tv(1.0)}),
        iot_case("nan-in-array", "A NaN element makes the whole array v null, q false.",
                 pl_device([("A", "float32")]), {"A": tv([1.0, math.nan])}),
        iot_case("bad-keeps-source-ts", "A bad tag keeps the source timestamp of its last good read.",
                 pl_device([("A", "int16")]),
                 {"A": tv(3, "bad_comm", ts=datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC))}),
        iot_case("bad-tags-field-ignored", "bad_tags_field has no meaning here: bad tags are always listed.",
                 pl_device([("A", "int16"), ("B", "int16")]), {"A": tv(1), "B": tv(2, "bad_comm")},
                 cfg=PayloadCfg(bad_tags_field=False)),
        iot_case("unknown-values-ignored", "Snapshot entries for names that are not tags of the device are ignored.",
                 pl_device([("A", "int16")]), {"A": tv(1), "GHOST": tv(2)}),
    ]


SPECS: dict[str, list[FileSpec]] = {
    "modbus/plan": [
        ("merge_and_gaps", "Block merging by function code, max_gap and overlaps (D-006).", plan_merge()),
        ("block_size", "Block-size caps: max_holding/input/coils/discrete and protocol limits (D-005, D-006).",
         plan_block_size()),
        ("bit_gaps", "Coil and discrete-input gaps: max_gap x 16 and max_gap_bits (D-006).", plan_bit_gaps()),
        ("strings_and_bits", "Strings .lllH/.lllL and .b bits in registers (D-006).", plan_strings_bits()),
        ("addressing", "Address forms: zero-based on/off, 5/6 digits, arrays, the last address.", plan_addressing()),
        ("rejected", "Rejected addresses go to invalid with a reason (D-006).", plan_rejected()),
        ("same_address", "Tags at the same address are one wire item (D-006).", plan_same_address()),
        ("params", "Driver params: refused values and lenient forms.", plan_params()),
        ("random_mix", "Seeded random mixes of every address form and limit.", plan_random()),
    ],
    "modbus/decode": [
        ("integers", "16/32/64-bit integers under every byte, word and dword order.", decode_integers()),
        ("floats", "float32/float64 under every byte, word and dword order.", decode_floats()),
        ("nonnormal", "NaN, infinity and denormal floats: nonnormal_float zero/unmodified (D-006).",
         decode_nonnormal()),
        ("bits", "Bits in registers (zero_based_bit, modicon_bit_order, byte order), coils and discrete inputs.",
         decode_bits()),
        ("strings", "Strings: H/L byte order, NUL cut, odd lengths, latin-1 (D-006).", decode_strings()),
        ("arrays", "1-D and 2-D arrays.", decode_arrays()),
        ("block_items", "Several items in one block: offsets, aliases, overlaps.", decode_block_items()),
        ("short_reply", "Short and long replies: per-tag decode errors.", decode_short()),
        ("random_mix", "Seeded random registers under random orders.", decode_random()),
    ],
    "scaling": [
        ("linear", "Linear scaling (Kepware semantics).", scaling_linear()),
        ("sqrt", "Square-root scaling.", scaling_sqrt()),
        ("clamp_negate", "Clamps and negate.", scaling_clamp_negate()),
        ("special", "Errors, pass-through types, arrays, NaN and infinity.", scaling_special()),
    ],
    "payload": [
        ("keys_and_types", "SubDevice JSON key order, value types and escaping (D-017).", payload_keys()),
        ("rounding", "Float rounding to float_decimals places, -0.0, repr forms (D-017).", payload_rounding()),
        ("quality", "Good and bad tags: Data, BadTags, DataValid (D-017).", payload_quality()),
        ("datetime", "DateTime: newest good source_ts, time zones, now fallback (D-017).", payload_datetime()),
    ],
    "payload/iotgw": [
        ("shape", "IoT Gateway standard template: ids, ms timestamps, types, rounding, order.",
         payload_iotgw_shape()),
        ("quality", "Bad, stale, missing and non-finite tags: v, q and t.", payload_iotgw_quality()),
    ],
}


SPECS.update(gen_conformance_ab.SPECS)      # Allen-Bradley Logix and PCCC (#5)
SPECS.update(gen_conformance_system_tags.SPECS)   # system and diagnostic tags (#120)
SPECS.update(gen_conformance_fins.SPECS)    # Omron FINS address parsing (#13, #166)


def build() -> dict[Path, str]:
    """{path: file text} for every vector file, expected values computed by the Python reference."""
    assert set(SPECS) == set(AREAS), "every area needs a spec"
    out: dict[Path, str] = {}
    for area, specs in SPECS.items():
        for stem, description, cases in specs:
            vectors = []
            ids: set[str] = set()
            for cid, desc, inp in cases:
                assert cid not in ids, f"{area}/{stem}: duplicate id {cid}"
                ids.add(cid)
                vectors.append({"id": cid, "description": desc, "input": inp,
                                "expected": reference.run(area, inp)})
            out[VECTORS_ROOT / area / f"{stem}.json"] = render(area, description, vectors)
    return out


def existing() -> set[Path]:
    return {p for area in AREAS for p in (VECTORS_ROOT / area).glob("*.json")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="only report stale, missing or extra files")
    args = ap.parse_args(argv)
    files = build()
    stale = sorted(p for p, text in files.items() if not p.exists() or p.read_text(encoding="utf-8") != text)
    extra = sorted(existing() - files.keys())
    rel: Callable[[Path], str] = lambda p: p.relative_to(ROOT).as_posix()  # noqa: E731
    if args.check:
        for p in stale:
            print(f"stale or missing: {rel(p)}")
        for p in extra:
            print(f"not generated (delete it or add it to the generator): {rel(p)}")
        return 1 if stale or extra else 0
    for p in stale:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(files[p], encoding="utf-8")
        print(f"wrote {rel(p)}")
    for p in extra:
        p.unlink()
        print(f"deleted {rel(p)}")
    n = sum(len(cases) for specs in SPECS.values() for _, _, cases in specs)
    print(f"{len(files)} files, {n} vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
