"""opencap.core.quality: the one quality mapping table (ADR-0006, #30).

The expected values here are written out by hand from ADR-0006 and the specifications, not
derived from the module, so a wrong edit to the table fails twice: here and in the vectors.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from opencap.config.models import Quality
from opencap.core import quality as q
from tests.conformance.vectors import load

ROOT = Path(__file__).resolve().parents[2]

# Rows of OPC 10000-6 Annex A as published in OPCFoundation/UA-Nodeset, Schema/StatusCode.csv
# (name, value, description), copied verbatim for the codes ADR-0006 uses.
SPEC_ROWS = [
    ("Good", 0x00000000, "The operation succeeded."),
    ("UncertainLastUsableValue", 0x40900000, "Whatever was updating this value has stopped doing so."),
    ("BadCommunicationError", 0x80050000, "A low level communication error occurred."),
    ("BadWaitingForInitialData", 0x80320000,
     "Waiting for the server to obtain values from the underlying data source."),
    ("BadDataEncodingInvalid", 0x80380000, "The data encoding is invalid."),
    ("BadConfigurationError", 0x80890000,
     "There is a problem with the configuration that affects the usefulness of the value."),
]

# ADR-0006, column by column: (quality, ever_good) -> (OPC UA name, OPC DA byte, Sparkplug is_null)
TABLE = {
    ("good", True): ("Good", 0xC0, False),
    ("good", False): ("Good", 0xC0, False),                        # ever_good is ignored for good
    ("stale", True): ("UncertainLastUsableValue", 0x44, False),
    ("stale", False): ("BadWaitingForInitialData", 0x00, True),
    ("bad_comm", True): ("BadCommunicationError", 0x18, False),     # the last value is kept
    ("bad_comm", False): ("BadCommunicationError", 0x18, True),     # ... if there is one
    ("bad_config", True): ("BadConfigurationError", 0x04, True),
    ("bad_config", False): ("BadConfigurationError", 0x04, True),
    ("bad_decode", True): ("BadDataEncodingInvalid", 0x00, True),
    ("bad_decode", False): ("BadDataEncodingInvalid", 0x00, True),
}


def test_status_codes_are_the_specifications():
    assert q.STATUS_CODES == {name: value for name, value, _ in SPEC_ROWS}


def test_status_codes_match_asyncua():
    """asyncua's StatusCodes are generated from the same NodeSet CSV: a second, independent copy."""
    ua = pytest.importorskip("asyncua").ua
    for name, value in q.STATUS_CODES.items():
        assert getattr(ua.StatusCodes, name) == value, name


@pytest.mark.parametrize(("quality", "ever_good"), list(TABLE))
def test_the_adr_table(quality: Quality, ever_good: bool):
    name, da, null = TABLE[quality, ever_good]
    assert q.opcua_name(quality, ever_good) == name
    assert q.to_opcua(quality, ever_good) == q.STATUS_CODES[name]
    assert q.to_opcda(quality, ever_good) == da
    assert q.to_sparkplug(quality, ever_good) == {"is_null": null, "Quality": da}
    assert q.to_subdevice(quality) == ("Data" if quality == "good" else "BadTags")


@pytest.mark.parametrize(("quality", "ever_good"), list(TABLE))
def test_opcua_severity_agrees_with_the_opcda_quality_bits(quality: Quality, ever_good: bool):
    """OPC UA severity (top 2 bits: 00 good, 01 uncertain, 10 bad) and the OPC DA quality field
    (QQ of QQSSSSLL: 11 good, 01 uncertain, 00 bad) say the same thing for every row."""
    ua_severity = q.to_opcua(quality, ever_good) >> 30
    da_quality = q.to_opcda(quality, ever_good) >> 6
    assert {0b00: 0b11, 0b01: 0b01, 0b10: 0b00}[ua_severity] == da_quality
    assert q.to_opcda(quality, ever_good) & 0b11 == 0            # no limit bits


def test_sparkplug_defaults_to_a_tag_with_a_last_value():
    assert q.to_sparkplug("stale") == {"is_null": False, "Quality": 0x44}
    assert q.to_sparkplug("bad_config") == {"is_null": True, "Quality": 0x04}


def test_every_quality_is_covered_and_ordered():
    assert set(q.QUALITIES) == set(get_args(Quality))
    assert sorted(q.QUALITY_ORDER) == sorted(q.QUALITIES)
    assert q.QUALITY_ORDER == ("good", "stale", "bad_comm", "bad_decode", "bad_config")


@pytest.mark.parametrize(("qualities", "worst"), [
    ([], "good"),
    (["good"], "good"),
    (["good", "stale"], "stale"),
    (["stale", "bad_comm"], "bad_comm"),
    (["bad_decode", "bad_comm"], "bad_decode"),
    (["bad_config", "bad_decode", "good"], "bad_config"),
    (iter(["good", "bad_comm", "good"]), "bad_comm"),               # any iterable
])
def test_worst(qualities, worst):
    assert q.worst(qualities) == worst


@pytest.mark.parametrize("fn", [
    lambda x: q.to_opcua(x, True), lambda x: q.to_opcda(x, True), lambda x: q.to_sparkplug(x),
    lambda x: q.opcua_name(x, False), q.to_subdevice, lambda x: q.worst(["good", x]),
])
def test_unknown_quality_is_refused(fn):
    with pytest.raises(ValueError, match="unknown quality 'bad'"):
        fn("bad")


def test_the_conformance_vectors_are_this_table():
    """tests/conformance/quality/ (also checked generically by test_conformance_vectors.py)."""
    vectors = load("quality")
    maps = [v for v in vectors if v.input["op"] == "map"]
    assert {(v.input["quality"], v.input["ever_good"]) for v in maps} == set(TABLE)
    for v in maps:
        name, da, null = TABLE[v.input["quality"], v.input["ever_good"]]
        assert v.expected["opcua_name"] == name and v.expected["opcua"] == q.STATUS_CODES[name], v.key
        assert v.expected["opcda"] == da and v.expected["sparkplug"] == {"is_null": null, "Quality": da}, v.key
    for v in vectors:
        if v.input["op"] == "worst":
            assert v.expected["worst"] == q.worst(v.input["qualities"]), v.key


def test_no_north_code_hard_codes_a_quality_code():
    """ADR-0006: only opencap.core.quality spells these StatusCodes."""
    codes = re.compile("|".join(f"0x{v:08x}" for v in q.STATUS_CODES.values() if v), re.IGNORECASE)
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in sorted((ROOT / "src" / "opencap").rglob("*.py"))
            if p.name != "quality.py" or p.parent.name != "core"
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if codes.search(line)]
    assert not hits, f"hard-coded quality StatusCodes outside opencap.core.quality: {hits}"
