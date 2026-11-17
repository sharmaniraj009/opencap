"""PCCC (SLC / MicroLogix / PLC-5 data-table) address parser.

PLAN Phase 3 acceptance: one unit test for every distinct address pattern found in
the real project (Allen-Bradley ControlLogix model 23 devices and the DF1 devices,
which use the same syntax). `PROJECT_PATTERNS` is that list; the real-project test
checks that it matches the project exactly.
"""

from __future__ import annotations

import re

import pytest

from opencap.drivers.ab_pccc import PcccAddress, parse_pccc_address


def address_pattern(address: str) -> str:
    """Digits -> '#'; symbolic names (no ':' and no '/') collapse to '<symbolic>'."""
    if not re.search(r"[:/]", address):
        return "<symbolic>"
    return re.sub(r"\d+", "#", address)


# (pattern, dtype) -> (example address from the project, expected parse or error substring)
PROJECT_PATTERNS: dict[tuple[str, str], tuple[str, PcccAddress | str]] = {
    ("F#:#", "float32"): ("F34:3", PcccAddress("F", 34, 3)),
    ("N#:#", "uint16"): ("N54:17", PcccAddress("N", 54, 17)),
    ("N#:#", "float32"): ("N66:10", PcccAddress("N", 66, 10)),
    ("B#:#/#", "bool"): ("B3:17/14", PcccAddress("B", 3, 17, bit=14)),
    ("B#/#", "bool"): ("B3/0", PcccAddress("B", 3, 0, bit=0)),
    ("RTC:#.HR", "uint16"): ("RTC:0.HR", "function file RTC"),
    ("RTC:#.MIN", "uint16"): ("RTC:0.MIN", "function file RTC"),
    ("<symbolic>", "float32"): ("GSM_ONFB", "symbolic address on a PCCC device"),
    # DF1 devices (Phase 7 reuses this parser)
    ("I:#/#", "bool"): ("I:0/0", PcccAddress("I", 1, 0, word=0, bit=0)),
    ("ST#:#", "string"): ("ST56:10", PcccAddress("ST", 56, 10)),
    ("T#:#.PRE", "uint16"): ("T47:0.PRE", PcccAddress("T", 47, 0, field="PRE")),
}


@pytest.mark.parametrize(
    ("address", "expected"),
    list(PROJECT_PATTERNS.values()),
    ids=[f"{p}|{t}" for p, t in PROJECT_PATTERNS],
)
def test_project_pattern(address: str, expected: PcccAddress | str) -> None:
    if isinstance(expected, str):
        with pytest.raises(ValueError, match=re.escape(expected)):
            parse_pccc_address(address)
    else:
        assert parse_pccc_address(address) == expected


def test_project_pattern_table_is_self_consistent() -> None:
    for (pattern, _dtype), (example, _expected) in PROJECT_PATTERNS.items():
        assert address_pattern(example) == pattern


# --- other symbolic names seen on PCCC devices -------------------------------

@pytest.mark.parametrize("address", ["BOD_1", "SEWAGE_CURRENT_DAY_1", "EU_MAX", "RAW_MIN", "MAINS_ON"])
def test_symbolic_names_are_rejected(address: str) -> None:
    with pytest.raises(ValueError, match="symbolic address on a PCCC device"):
        parse_pccc_address(address)


# --- bit-number form ----------------------------------------------------------

@pytest.mark.parametrize(
    ("address", "element", "bit"),
    [("B3/0", 0, 0), ("B3/14", 0, 14), ("B3/15", 0, 15), ("B3/16", 1, 0), ("B3/17", 1, 1), ("B3/4095", 255, 15)],
)
def test_b_bit_number_form(address: str, element: int, bit: int) -> None:
    assert parse_pccc_address(address) == PcccAddress("B", 3, element, bit=bit)


def test_b_bit_number_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of range"):
        parse_pccc_address("B3/4096")


# --- other supported file types -------------------------------------------------

@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("N7:0", PcccAddress("N", 7, 0)),
        ("N7:0/3", PcccAddress("N", 7, 0, bit=3)),
        ("N7:255", PcccAddress("N", 7, 255)),
        ("F8:0", PcccAddress("F", 8, 0)),
        ("L9:4", PcccAddress("L", 9, 4)),
        ("L9:4/31", PcccAddress("L", 9, 4, bit=31)),
        ("B3:0", PcccAddress("B", 3, 0)),
        ("B10:5/15", PcccAddress("B", 10, 5, bit=15)),
        ("T4:0.ACC", PcccAddress("T", 4, 0, field="ACC")),
        ("T4:1.PRE", PcccAddress("T", 4, 1, field="PRE")),
        ("T4:2.EN", PcccAddress("T", 4, 2, field="EN")),
        ("T4:2.TT", PcccAddress("T", 4, 2, field="TT")),
        ("T4:2.DN", PcccAddress("T", 4, 2, field="DN")),
        ("T4:2/DN", PcccAddress("T", 4, 2, field="DN")),
        ("C5:0.ACC", PcccAddress("C", 5, 0, field="ACC")),
        ("C5:0.PRE", PcccAddress("C", 5, 0, field="PRE")),
        ("C5:0.CU", PcccAddress("C", 5, 0, field="CU")),
        ("C5:0.CD", PcccAddress("C", 5, 0, field="CD")),
        ("C5:0.DN", PcccAddress("C", 5, 0, field="DN")),
        ("C5:0.OV", PcccAddress("C", 5, 0, field="OV")),
        ("C5:0.UN", PcccAddress("C", 5, 0, field="UN")),
        ("S:1", PcccAddress("S", 2, 1)),
        ("S:1/5", PcccAddress("S", 2, 1, bit=5)),
        ("S2:1/5", PcccAddress("S", 2, 1, bit=5)),
        ("I:1", PcccAddress("I", 1, 1)),
        ("I:1.2", PcccAddress("I", 1, 1, word=2)),
        ("I:1.2/3", PcccAddress("I", 1, 1, word=2, bit=3)),
        ("I1:1.2/3", PcccAddress("I", 1, 1, word=2, bit=3)),
        ("I:0/19", PcccAddress("I", 1, 0, word=1, bit=3)),       # bit number spans words
        ("O:0/2", PcccAddress("O", 0, 0, bit=2)),
        ("O0:2.1", PcccAddress("O", 0, 2, word=1)),
        ("ST9:0", PcccAddress("ST", 9, 0)),
        ("f8:1", PcccAddress("F", 8, 1)),                         # case-insensitive
        ("  N7:1 ", PcccAddress("N", 7, 1)),                      # surrounding blanks ignored
        ("t4:0.acc", PcccAddress("T", 4, 0, field="ACC")),
    ],
)
def test_supported_forms(address: str, expected: PcccAddress) -> None:
    assert parse_pccc_address(address) == expected


# --- rejected forms ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("", "empty address"),
        ("   ", "empty address"),
        ("N7:256", "out of range"),          # pycomm3 encodes the element as one byte
        ("N256:0", "out of range"),
        ("N0:0", "out of range"),
        ("N7:0/16", "out of range"),
        ("L9:0/32", "out of range"),
        ("F8:0/1", "bit access"),
        ("ST9:0/1", "bit access"),
        ("T4:0", "needs a sub-element"),
        ("T4:0.CU", "not valid for a T"),
        ("C5:0.TT", "not valid for a C"),
        ("T4:0.LEN", "not valid for a T"),
        ("I2:0/1", "file number"),
        ("O1:0/1", "file number"),
        ("R6:0.LEN", "file type R is not supported"),
        ("A9:0", "file type A is not supported"),
        ("MG10:0", "file type MG is not supported"),
        ("HSC:0.ACC", "function file HSC"),
        ("N7:0{10}", "not supported"),       # element counts are the planner's job
        ("N7:", "cannot parse"),
        ("N7:0/", "cannot parse"),
        ("N7::0", "cannot parse"),
        ("N7:0.1", "cannot parse"),
    ],
)
def test_rejected_forms(address: str, reason: str) -> None:
    with pytest.raises(ValueError, match=re.escape(reason)):
        parse_pccc_address(address)


def test_canonical_form_round_trips() -> None:
    for text in ["N7:0", "N7:0/3", "B3:1/1", "F8:2", "L9:4/31", "T4:0.ACC", "C5:1.DN",
                 "S:1/5", "I:1.2/3", "O:0.0/2", "ST9:0"]:
        addr = parse_pccc_address(text)
        assert parse_pccc_address(addr.canonical()) == addr
    assert parse_pccc_address("B3/17").canonical() == "B3:1/1"
    assert parse_pccc_address("I:0/19").canonical() == "I:0.1/3"


def test_element_sizes_match_pccc() -> None:
    sizes = {t: parse_pccc_address(a).elem_size for t, a in
             [("N", "N7:0"), ("B", "B3:0"), ("F", "F8:0"), ("L", "L9:0"), ("T", "T4:0.ACC"),
              ("C", "C5:0.ACC"), ("S", "S:0"), ("I", "I:0"), ("O", "O:0"), ("ST", "ST9:0")]}
    assert sizes == {"N": 2, "B": 2, "F": 4, "L": 4, "T": 6, "C": 6, "S": 2, "I": 2, "O": 2, "ST": 84}


def test_element_sizes_agree_with_pycomm3() -> None:
    pccc = pytest.importorskip("pycomm3.cip.pccc")
    for text in ["N7:0", "B3:0", "F8:0", "L9:0", "T4:0.ACC", "C5:0.ACC", "S:0", "I:0", "O:0", "ST9:0"]:
        addr = parse_pccc_address(text)
        assert addr.elem_size == pccc.PCCC_DATA_SIZE[addr.file_type], text
