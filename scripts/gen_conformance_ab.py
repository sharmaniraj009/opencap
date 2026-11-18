"""Allen-Bradley conformance vector inputs (Logix and PCCC planning and decoding, ADR-0014).

Case data only: `scripts/gen_conformance.py` imports `SPECS` from here, computes every
`expected` with `tests/conformance/reference.py` and writes the files. Run that script, not
this one. References: docs/decisions.md D-008, D-024 to D-027.
"""

from __future__ import annotations

import random
from typing import Any

from tests.conformance.vectors import encode_value

Case = tuple[str, str, dict[str, Any]]          # (id, description, input)
FileSpec = tuple[str, str, list[Case]]          # (file stem, file description, cases)


def T(name: str, address: str, dtype: str = "float32", **extra: Any) -> dict[str, Any]:
    return {"name": name, "address": address, "dtype": dtype, **extra}


def plan_case(cid: str, desc: str, tags: list[dict[str, Any]], **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags}


# --- ab_logix/plan -------------------------------------------------------------------------------

def logix_addresses() -> list[Case]:
    return [
        plan_case("bare-and-member", "A bare name and a structure member are read by name.",
                  [T("A", "FT_01"), T("B", "INF_FLOW_TOTAL.TOTAL"), T("C", "PUMP_1.RUN_FB", "bool")]),
        plan_case("array-indexes", "Array elements with 1, 2 and 3 indexes.",
                  [T("A", "ARRAY_REAL[4]"), T("B", "ARR[1,2]"), T("C", "ARR[1,2,3]")]),
        plan_case("udt-array-member-chain", "A chain of array and member segments.",
                  [T("A", "UDT[2].Member[3].X", "int32")]),
        plan_case("program-scope", "Program-scoped tags on a Logix controller.",
                  [T("A", "Program:MainProgram.Tag"), T("B", "Program:Main.UDT.Member", "int16")]),
        plan_case("bits", "A trailing .n is a bit of an integer, read through the integer (up to .63).",
                  [T("A", "DINT_TAG.5", "bool"), T("B", "UDT.Word.31", "bool"), T("C", "Program:Main.Status.0", "bool"),
                   T("D", "LINT_TAG.63", "bool")]),
        plan_case("underscore-and-spaces", "A leading underscore is legal; surrounding blanks are ignored.",
                  [T("A", "_Hidden"), T("B", "  FT_02 ")]),
        plan_case("bit-of-array-element", "A bit of an array element.", [T("A", "ARR[7].3", "bool")]),
        plan_case("bit-dtype-int", "A bit tag may be typed int16 (0 or 1).", [T("A", "DINT_TAG.1", "int16")]),
        plan_case("numeric-member-is-a-bit", "A numeric last segment after a structure path is a bit, not a member.",
                  [T("A", "Timer1.ACC.0", "bool"), T("B", "Timer1.ACC", "int32")]),
    ]


def logix_rejected() -> list[Case]:
    bad = [
        ("empty", "", "Empty address."),
        ("blank", "   ", "Blank address."),
        ("pccc-float", "F28:0", "A PCCC data-table address on a symbolic device."),
        ("pccc-int", "N54:5", "A PCCC integer address on a symbolic device."),
        ("pccc-bit", "B3:0/14", "A PCCC bit address on a symbolic device."),
        ("digit-first", "1ABC", "A name cannot start with a digit."),
        ("double-dot", "A..B", "Empty segment."),
        ("trailing-dot", "A.", "Trailing dot."),
        ("leading-dot", ".A", "Leading dot."),
        ("space-inside", "A B", "Blank inside a name."),
        ("dash", "A-B", "Dash in a name."),
        ("colon", "A:B", "Colon outside Program:."),
        ("open-bracket", "A[", "Unclosed bracket."),
        ("empty-index", "A[]", "Empty index."),
        ("letter-index", "A[x]", "Non-numeric index."),
        ("two-brackets", "A[1][2]", "Indexes are written [i,j], not [i][j]."),
        ("four-dims", "A[1,2,3,4]", "More than 3 indexes."),
        ("slash", "A.B/3", "Slash bit form is PCCC, not Logix."),
        ("program-no-program-name", "Program:.X", "Program: without a program name."),
        ("program-no-tag", "Program:Main", "Program: without a tag."),
        ("bit-64", "A.64", "Bit 64 is out of range."),
        ("block-read", "A{10}", "Array block reads {n} are not supported."),
        ("block-read-index", "A[0]{10}", "Array block reads {n} are not supported after an index."),
    ]
    cases = [plan_case(f"reject-{cid}", f"{desc} The tag goes to invalid; the good tag still plans.",
                       [T("BAD", addr), T("GOOD", "FT_01")]) for cid, addr, desc in bad]
    cases += [
        plan_case("reject-program-on-micro800", "Program-scoped tags are not readable on Micro800 (globals only).",
                  [T("BAD", "Program:Main.X"), T("GOOD", "FT_01")], micro800=True),
        plan_case("reject-string-on-bit", "dtype string is not supported on a bit address.",
                  [T("BAD", "DINT_TAG.3", "string"), T("GOOD", "DINT_TAG.4", "bool")]),
        plan_case("reject-several-sorted", "Several bad tags: invalid is keyed and sorted by tag name.",
                  [T("Z", "N7:0"), T("A", ""), T("M", "OK_TAG"), T("B", "X.99")]),
        plan_case("reject-disabled-not-reported", "A disabled tag is neither planned nor reported.",
                  [T("OFF", "1BAD", enabled=False), T("ON", "FT_01")]),
    ]
    return cases


def logix_batching() -> list[Case]:
    many = [T(f"T{i:02d}", f"TAG_{i:02d}") for i in range(45)]
    return [
        plan_case("max-batch-default-40", "Default max_batch 40: 45 distinct names make batches of 40 and 5.", many),
        plan_case("max-batch-10", "max_batch 10.", many[:25], max_batch=10),
        plan_case("max-batch-1", "max_batch 1: one name per read.", many[:3], max_batch=1),
        plan_case("max-batch-0-is-default", "max_batch 0 (or null) means the default 40.", many, max_batch=0),
        plan_case("max-batch-null-is-default", "max_batch null means the default 40.", many[:41], max_batch=None),
        plan_case("max-batch-negative", "A negative max_batch is refused when planning.", many[:2], max_batch=-1),
        plan_case("names-sorted", "Names are read in sorted (code point) order: upper case before lower case.",
                  [T("t1", "zeta"), T("t2", "Alpha"), T("t3", "beta"), T("t4", "_x"), T("t5", "Program:P.A")]),
        plan_case("dedup", "The same address under three names is read once and served to all three.",
                  [T("A", "FLOW"), T("B", "FLOW"), T("C", "FLOW"), T("D", "LEVEL")]),
        plan_case("bits-share-one-read", "Bits of one integer and the integer itself are one read; the read size "
                  "is the largest of a 4-byte bit host and the tag dtypes.",
                  [T("B0", "STATUS.0", "bool"), T("B5", "STATUS.5", "bool"), T("W", "STATUS", "int16")]),
        plan_case("bits-with-lint-host", "A bit next to an int64 tag of the same name: the read is 8 bytes.",
                  [T("B", "BIG.40", "bool"), T("Q", "BIG", "int64")]),
        plan_case("items-order", "Items are ordered by read, then by tag name.",
                  [T("z", "B_TAG"), T("a", "B_TAG"), T("m", "A_TAG")]),
        plan_case("input-order-irrelevant", "Tags in reverse order plan exactly the same.",
                  list(reversed([T(f"T{i:02d}", f"N_{i:02d}", "int32") for i in range(12)]))),
        plan_case("disabled-ignored", "Disabled tags are not read.",
                  [T("ON", "FT_01"), T("OFF", "FT_02", enabled=False)]),
        plan_case("dedup-across-batches", "De-duplication happens before batching: 3 names x 2 tags with "
                  "max_batch 2 make 2 reads.", [T(f"T{i}", f"N_{i // 2}") for i in range(6)], max_batch=2),
        plan_case("empty", "No tags: no blocks.", []),
    ]


def logix_packing() -> list[Case]:
    forty = [T(f"T{i:02d}", f"FLOW_TOTALIZER_{i:02d}") for i in range(40)]
    three = [T("A", "AA"), T("B", "AB"), T("C", "AC")]           # 16 bytes each in a packet
    return [
        plan_case("micro800-one-per-name", "Micro800: no Multiple Service Packet, one request per name (D-008).",
                  [T(f"T{i:02d}", f"TAG_{i:02d}") for i in range(45)], micro800=True),
        plan_case("logix-large-one-packet", "Logix with 4000 B: 40 names fit one Multiple Service Packet.", forty),
        plan_case("logix-500-several", "Logix with connection_size 500: the same 40 names need several packets.",
                  forty, connection_size=500),
        plan_case("single-name-one-request", "A single name is one request.", [T("A", "FT_01")]),
        plan_case("single-name-micro800", "A single name on Micro800 is one request.", [T("A", "FT_01")],
                  micro800=True),
        plan_case("connection-size-default-logix", "Logix defaults to 4000 B (Extended Forward Open).",
                  [T("A", "X")]),
        plan_case("connection-size-default-micro800", "Micro800 defaults to 500 B.", [T("A", "X")], micro800=True),
        plan_case("connection-size-null-micro800", "connection_size null keeps the Micro800 default.",
                  [T("A", "X")], micro800=True, connection_size=None),
        plan_case("connection-size-explicit-micro800", "An explicit connection_size wins on Micro800.",
                  [T("A", "X")], micro800=True, connection_size=4000),
        plan_case("packet-exactly-full", "Three 16-byte entries after the 10-byte header fill a 58-byte packet "
                  "exactly: one packet.", three, connection_size=58),
        plan_case("packet-one-byte-short", "With 57 bytes the third entry starts a second packet.",
                  three, connection_size=57),
        plan_case("fragmented-string", "A STRING (88 B) larger than the connection is read in fragments; the "
                  "other name still takes its own packet.",
                  [T("TXT", "LONG_STRING", "string"), T("A", "FT_01")], connection_size=60),
        plan_case("fragmented-string-micro800", "Micro800: an oversized single read is fragmented too.",
                  [T("TXT", "S", "string"), T("A", "FT_01")], micro800=True, connection_size=50),
        plan_case("single-read-fits", "A single read is not fragmented while data + request fit the connection.",
                  [T("TXT", "S", "string")], connection_size=98),
        plan_case("fragments-exact-2", "connection_size 52: 44 data bytes per fragment, 88 bytes in exactly "
                  "2 fragments.", [T("TXT", "S", "string")], connection_size=52),
        plan_case("fragments-one-short-3", "connection_size 51: 43 bytes per fragment, 3 fragments.",
                  [T("TXT", "S", "string")], connection_size=51),
        plan_case("fragmented-in-packet", "In a Multiple Service Packet an entry larger than the connection is "
                  "read in fragments while the others share packets.",
                  [T("A", "AA"), T("B", "AB"), T("S", "S", "string")], connection_size=52),
        plan_case("data-sizes", "Reply data size per dtype: bool 1, 16-bit 2, 32-bit 4, 64-bit 8, string 88.",
                  [T("B", "D_B", "bool"), T("I", "D_I", "int16"), T("U", "D_U", "uint16"), T("D", "D_D", "int32"),
                   T("UD", "D_UD", "uint32"), T("F", "D_F", "float32"), T("L", "D_L", "int64"),
                   T("UL", "D_UL", "uint64"), T("LF", "D_LF", "float64"), T("S", "D_S", "string")]),
        plan_case("path-sizes", "Request path size: odd names are padded, member and index segments add up.",
                  [T("A", "A"), T("B", "AB"), T("C", "ABC"), T("D", "AB.CD"), T("E", "Program:Main.X")]),
        plan_case("index-sizes", "Array index segments: 8-bit 2 B, 16-bit 4 B, 32-bit 6 B.",
                  [T("A", "ARR[3]"), T("B", "ARR[300]"), T("C", "ARR[70000]"), T("D", "ARR[255,256,65536]")]),
        plan_case("connection-size-1", "connection_size 1: every read is fragmented.",
                  [T("A", "X"), T("B", "Y", "int64")], connection_size=1),
    ]


def logix_params() -> list[Case]:
    tags = [T("A", "FT_01")]
    return [
        plan_case("param-connection-size-0", "connection_size 0 is refused.", tags, connection_size=0),
        plan_case("param-connection-size-negative", "A negative connection_size is refused.", tags, connection_size=-5),
        plan_case("param-connection-size-string", "connection_size as a string is refused.", tags,
                  connection_size="500"),
        plan_case("param-connection-size-bool", "connection_size true is refused.", tags, connection_size=True),
        plan_case("param-batching-values", "batching accepts auto, on, off and booleans (no plan change).",
                  tags, batching="ON"),
        plan_case("param-batching-false", "batching false is accepted.", tags, batching=False),
        plan_case("param-batching-bogus", "batching must be auto, on or off.", tags, batching="maybe"),
        plan_case("param-slot-out-of-range", "slot 256 is refused.", tags, slot=256),
        plan_case("param-slot-not-int", "A non-numeric slot is refused.", tags, slot="x"),
        plan_case("param-path-odd-route", "A CIP route needs port/link pairs.", tags, path="1,0,2"),
        plan_case("param-path-backplane", "path 1,3 is a backplane slot: accepted.", tags, path="1,3"),
        plan_case("param-micro800-truthy", "micro800 is read as a truth value.", tags, micro800=1),
        plan_case("param-unknown-ignored", "Unknown params are ignored.", tags, colour="blue"),
    ]


def logix_random() -> list[Case]:
    """Seeded mixes like the pycomm3 calibration test (tests/unit/test_logix_plan.py)."""
    rng = random.Random(20261120)
    dtypes = ["float32", "uint16", "int16", "bool", "uint32", "int32", "int64", "float64", "string"]
    cases: list[Case] = []
    for k in range(12):
        params: dict[str, Any] = {"micro800": rng.random() < 0.4,
                                  "connection_size": rng.choice([None, 200, 500, 4000])}
        if rng.random() < 0.5:
            params["max_batch"] = rng.choice([5, 20, 40, 60])
        tags: list[dict[str, Any]] = []
        for i in range(rng.randint(10, 60)):
            name = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ_") for _ in range(rng.randint(3, 30))) + str(i)
            kind = rng.random()
            dtype = rng.choice(dtypes)
            if kind < 0.2:
                name += f"[{rng.choice([3, 300, 70000])}]"
            elif kind < 0.4:
                name += f".M{rng.randint(1, 9999)}"
            elif kind < 0.5:
                name += f".{rng.randint(0, 63)}"
                dtype = rng.choice(["bool", "int16"])
            elif kind < 0.55 and tags:
                name = rng.choice(tags)["address"]
            tags.append(T(f"T{i:02d}", name, dtype))
        cases.append(plan_case(f"mix-{k:02d}", f"Seeded random mix {k}: {len(tags)} tags, "
                               f"micro800={params['micro800']}, connection_size={params['connection_size']}.",
                               tags, **params))
    return cases


# --- ab_logix/decode -----------------------------------------------------------------------------

def lx_case(cid: str, desc: str, tags: list[dict[str, Any]], values: list[Any] | None, **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags, "values": encode_value(values)}


def lx_one(cid: str, desc: str, dtype: str, raw: Any) -> Case:
    return lx_case(cid, desc, [T("X", "TAG", dtype)], [raw])


def logix_coerce() -> list[Case]:
    return [
        lx_one("bool-from-0", "bool from integer 0.", "bool", 0),
        lx_one("bool-from-1", "bool from integer 1.", "bool", 1),
        lx_one("bool-from-2", "bool from a non-zero integer is true.", "bool", 2),
        lx_one("bool-from-neg", "bool from -1 is true.", "bool", -1),
        lx_one("bool-from-true", "bool from a BOOL value.", "bool", True),
        lx_one("bool-from-float-0", "bool from 0.0 is false.", "bool", 0.0),
        lx_one("bool-from-float-half", "bool from 0.5 is true.", "bool", 0.5),
        lx_one("bool-from-nan", "bool from NaN is true (NaN is not zero).", "bool", float("nan")),
        lx_one("int16-from-65535", "int16 wraps 65535 to -1.", "int16", 65535),
        lx_one("int16-from-32768", "int16 wraps 32768 to -32768.", "int16", 32768),
        lx_one("int16-from-neg", "int16 keeps -1.", "int16", -1),
        lx_one("int16-from-big", "int16 keeps only the low 16 bits of a DINT.", "int16", 0x12345678),
        lx_one("int16-from-true", "int16 from a BOOL is 1.", "int16", True),
        lx_one("uint16-from-neg", "uint16 masks -1 to 65535.", "uint16", -1),
        lx_one("int32-from-uint", "int32 wraps 4294967295 to -1.", "int32", 4294967295),
        lx_one("uint32-from-neg", "uint32 masks -2 to 4294967294.", "uint32", -2),
        lx_one("int64-from-u64-max", "int64 wraps 2^64-1 to -1.", "int64", (1 << 64) - 1),
        lx_one("uint64-from-neg", "uint64 masks -1 to 2^64-1.", "uint64", -1),
        lx_one("int64-min", "int64 keeps -2^63.", "int64", -(1 << 63)),
        lx_one("float32-from-int", "float32 from an integer is a float.", "float32", 3),
        lx_one("float32-not-narrowed", "float32 keeps the double it got (no float32 rounding).", "float32", 0.1),
        lx_one("float32-from-true", "float32 from a BOOL is 1.0.", "float32", True),
        lx_one("float32-nan", "NaN passes through float32.", "float32", float("nan")),
        lx_one("float64-inf", "Infinity passes through float64.", "float64", float("inf")),
        lx_one("float64-big-int", "A large integer converts to the nearest double.", "float64", (1 << 63) - 1),
        lx_one("string", "A STRING value.", "string", "PUMP 1 RUN"),
        lx_one("string-empty", "An empty STRING.", "string", ""),
        lx_one("string-from-int", "A string tag given an integer is a decode error.", "string", 5),
        lx_one("int-from-str", "An integer tag given a string is a decode error.", "int16", "5"),
        lx_one("int-from-list", "A number tag given a list (an array read) is a decode error.", "float32", [1.0, 2.0]),
        lx_one("int-from-float", "An integer tag given a float is a decode error.", "int16", 1.5),
        lx_one("int-from-whole-float", "Even a whole float is refused for an integer dtype.", "int32", 2.0),
        lx_one("bool-from-str", "bool from a string is a decode error.", "bool", "true"),
    ]


def logix_bits() -> list[Case]:
    def bit(cid: str, desc: str, address: str, raw: Any, dtype: str = "bool") -> Case:
        return lx_case(cid, desc, [T("X", address, dtype)], [raw])
    return [
        bit("bit-0-set", "Bit 0 of 1.", "W.0", 1),
        bit("bit-5-clear", "Bit 5 of 1 is clear.", "W.5", 1),
        bit("bit-31-of-min-dint", "Bit 31 of -2^31 (a DINT's sign bit).", "W.31", -(1 << 31)),
        bit("bit-63-of-neg-one", "Bit 63 of -1: negative values are two's complement.", "W.63", -1),
        bit("bit-63-of-min-lint", "Bit 63 of -2^63.", "W.63", -(1 << 63)),
        bit("bit-40-of-positive", "A bit above 31 of a positive LINT.", "W.40", 1 << 40),
        bit("bit-of-bool", "A BOOL value is an integer: bit 0 of true is set.", "W.0", True),
        bit("bit-1-of-bool", "Bit 1 of true is clear.", "W.1", True),
        bit("bit-as-int16", "A bit typed int16 gives 0 or 1.", "W.2", 4, "int16"),
        bit("bit-as-float32", "A bit typed float32 gives 0.0 or 1.0.", "W.2", 4, "float32"),
        bit("bit-of-float", "A bit of a REAL is a decode error.", "W.0", 1.0),
        bit("bit-of-str", "A bit of a string is a decode error.", "W.0", "1"),
        lx_case("bits-and-word", "Bits and the integer itself from one value.",
                [T("B0", "W.0", "bool"), T("B1", "W.1", "bool"), T("V", "W", "int32")], [-2]),
    ]


def logix_reply_shape() -> list[Case]:
    three = [T("A", "A1"), T("B", "B1"), T("C", "C1")]
    return [
        lx_case("all-present", "One value per read, in read order.", three, [1.0, 2.0, 3.0]),
        lx_case("none-element", "pycomm3 gave no value for one name: only its tags fail.", three, [1.0, None, 3.0]),
        lx_case("short-reply", "A reply shorter than the reads fails the missing names.", three, [1.0]),
        lx_case("whole-reply-none", "No values at all: every tag fails.", three, None),
        lx_case("empty-reply", "An empty reply: every tag fails.", three, []),
        lx_case("long-reply", "Extra values are ignored.", three, [1.0, 2.0, 3.0, 4.0]),
        lx_case("aliases", "Aliases of one name get the same value; each fails on its own dtype.",
                [T("F", "X"), T("I", "X", "int16"), T("S", "X", "string")], [7]),
        lx_case("none-element-aliases", "A missing value fails every alias with the name in the reason.",
                [T("A", "X"), T("B", "X", "int32")], [None]),
        lx_case("sorted-names", "Values follow the sorted read order, not the tag order.",
                [T("Z", "zz", "int16"), T("A", "AA", "int16")], [1, 2]),
        lx_case("micro800-same-decode", "Micro800 decodes the same way.", three, [1, True, -1], micro800=True),
    ]


# --- ab_pccc/plan --------------------------------------------------------------------------------

def P(name: str, address: str, dtype: str = "int16", **extra: Any) -> dict[str, Any]:
    return {"name": name, "address": address, "dtype": dtype, **extra}


def pccc_addresses() -> list[Case]:
    return [
        plan_case("integer-file", "N file elements and an element bit.",
                  [P("A", "N7:0"), P("B", "N7:0/3", "bool"), P("C", "N7:255")]),
        plan_case("float-file", "F file elements.", [P("A", "F8:0", "float32"), P("B", "F8:1", "float64")]),
        plan_case("long-file", "L file elements and bit 31.", [P("A", "L9:4", "int32"), P("B", "L9:4/31", "bool")]),
        plan_case("binary-file", "B file words and element bits.",
                  [P("A", "B3:0", "uint16"), P("B", "B10:5/15", "bool")]),
        plan_case("binary-bit-number", "B3/17 (bit-number form) is element 1, bit 1, read as a whole word.",
                  [P("A", "B3/17", "bool"), P("B", "B3/0", "bool"), P("C", "B3/4095", "bool")]),
        plan_case("timer-subelements", "Timer sub-elements .ACC/.PRE/.EN/.TT/.DN and the /DN form.",
                  [P("ACC", "T4:0.ACC"), P("PRE", "T4:1.PRE"), P("EN", "T4:2.EN", "bool"), P("TT", "T4:2.TT", "bool"),
                   P("DN", "T4:2.DN", "bool"), P("DN2", "T4:3/DN", "bool")]),
        plan_case("counter-subelements", "Counter sub-elements .ACC/.PRE/.CU/.CD/.DN/.OV/.UN/.UA.",
                  [P("ACC", "C5:0.ACC"), P("PRE", "C5:0.PRE"), P("CU", "C5:0.CU", "bool"), P("CD", "C5:0.CD", "bool"),
                   P("DN", "C5:0.DN", "bool"), P("OV", "C5:0.OV", "bool"), P("UN", "C5:0.UN", "bool"),
                   P("UA", "C5:0.UA", "bool")]),
        plan_case("status-file", "S file: S:1, S:1/5 and S2:1/5 are the same file 2.",
                  [P("A", "S:1"), P("B", "S:1/5", "bool"), P("C", "S2:1/5", "bool"), P("D", "S:3", "uint16")]),
        plan_case("input-file", "I file: slot, word and bit forms; I:0/19 is word 1 bit 3 of slot 0.",
                  [P("A", "I:1"), P("B", "I:1.2"), P("C", "I:1.2/3", "bool"), P("D", "I1:1.3/4", "bool"),
                   P("E", "I:0/19", "bool")]),
        plan_case("output-file", "O file: O:0/2 and O0:2.1.", [P("A", "O:0/2", "bool"), P("B", "O0:2.1")]),
        plan_case("string-file", "ST file elements.", [P("A", "ST9:0", "string"), P("B", "ST9:5", "string")]),
        plan_case("case-and-blanks", "Addresses are case-insensitive; surrounding blanks are ignored.",
                  [P("A", "f8:1", "float32"), P("B", "  N7:1 "), P("C", "t4:0.acc"), P("D", "b3:2/1", "bool")]),
        plan_case("max-element-and-file", "Element 255 and file 255 are the largest accepted.",
                  [P("A", "N255:255"), P("B", "F1:0", "float32")]),
    ]


def pccc_rejected() -> list[Case]:
    bad = [
        ("empty", "", "int16", "Empty address."),
        ("blank", "   ", "int16", "Blank address."),
        ("element-256", "N7:256", "int16", "Element numbers are one byte (0-255)."),
        ("file-256", "N256:0", "int16", "File numbers are one byte (1-255)."),
        ("file-0", "N0:0", "int16", "File 0 is not a data file."),
        ("n-bit-16", "N7:0/16", "bool", "Bits of a 16-bit word are 0-15."),
        ("l-bit-32", "L9:0/32", "bool", "Bits of an L element are 0-31."),
        ("f-bit", "F8:0/1", "bool", "No bit access on a float file."),
        ("st-bit", "ST9:0/1", "bool", "No bit access on a string file."),
        ("timer-no-sub", "T4:0", "int16", "A timer needs a sub-element."),
        ("timer-cu", "T4:0.CU", "bool", ".CU is a counter sub-element."),
        ("counter-tt", "C5:0.TT", "bool", ".TT is a timer sub-element."),
        ("timer-len", "T4:0.LEN", "int16", ".LEN is not a timer sub-element."),
        ("input-file-2", "I2:0/1", "bool", "The input file is file 1."),
        ("output-file-1", "O1:0/1", "bool", "The output file is file 0."),
        ("status-file-5", "S5:1", "int16", "The status file is file 2."),
        ("control-file", "R6:0.LEN", "int16", "Control files are not supported by SLCDriver."),
        ("ascii-file", "A9:0", "int16", "ASCII files are not supported."),
        ("message-file", "MG10:0", "int16", "Message files are not supported."),
        ("hsc", "HSC:0.ACC", "int32", "MicroLogix function file HSC."),
        ("rtc-hour", "RTC:0.HR", "int16", "MicroLogix function file RTC (D-008)."),
        ("rtc-minute", "RTC:0.MIN", "int16", "MicroLogix function file RTC."),
        ("element-count", "N7:0{10}", "int16", "Element counts {n} are the planner's job."),
        ("no-element", "N7:", "int16", "Missing element."),
        ("trailing-slash", "N7:0/", "bool", "Missing bit."),
        ("double-colon", "N7::0", "int16", "Double colon."),
        ("dot-on-n", "N7:0.1", "int16", "A dot sub-element on an integer file."),
        ("symbolic", "GSM_ONFB", "float32", "A symbolic name on a PCCC device."),
        ("program-scope", "Program:Main.X", "int16", "A Logix program tag on a PCCC device."),
        ("bit-number-too-big", "B3/4096", "bool", "Bit numbers stop at 4095 (element 255, bit 15)."),
        ("io-slot-256", "I:256", "int16", "I/O slots are 0-255."),
        ("io-word-256", "I:0.256", "int16", "I/O words are 0-255."),
        ("io-bit-number-past-word-255", "I:0.255/16", "bool", "A bit number that carries past word 255."),
        ("st-element-256", "ST9:256", "string", "ST element 256."),
        ("dtype-string-on-n", "N7:0", "string", "dtype string on a 16-bit word."),
        ("dtype-int32-on-n", "N7:0", "int32", "int32 on a 16-bit word (no guessing at word pairs)."),
        ("dtype-uint32-on-n", "N7:0", "uint32", "uint32 on a 16-bit word."),
        ("dtype-int64-on-b", "B3:0", "int64", "int64 on a B word."),
        ("dtype-int16-on-f", "F8:0", "int16", "Integer dtypes on a float file."),
        ("dtype-bool-on-f", "F8:0", "bool", "bool on a float file."),
        ("dtype-int16-on-l", "L9:0", "int16", "int16 on a 32-bit L element."),
        ("dtype-float32-on-st", "ST9:0", "float32", "float32 on a string file."),
        ("dtype-string-on-bit", "B3:0/1", "string", "string on a bit."),
        ("dtype-string-on-timer-bit", "T4:0.DN", "string", "string on a timer bit."),
        ("dtype-int32-on-timer-acc", "T4:0.ACC", "int32", "int32 on a timer .ACC word."),
    ]
    cases = [plan_case(f"reject-{cid}", f"{desc} The tag goes to invalid; the good tag still plans.",
                       [P("BAD", addr, dtype), P("GOOD", "N7:10")]) for cid, addr, dtype, desc in bad]
    cases += [
        plan_case("reject-several-sorted", "Several bad tags: invalid is keyed and sorted by tag name.",
                  [P("Z", "RTC:0.HR"), P("A", ""), P("M", "N7:0"), P("B", "F8:0", "int16")]),
        plan_case("reject-disabled-not-reported", "A disabled tag is neither planned nor reported.",
                  [P("OFF", "RTC:0.HR", enabled=False), P("ON", "N7:0")]),
    ]
    return cases


def pccc_blocks() -> list[Case]:
    return [
        plan_case("contiguous", "Contiguous elements merge into one {n} read.",
                  [P("A", "N7:0"), P("B", "N7:1"), P("C", "N7:2")]),
        plan_case("single-no-suffix", "A single element is read without {n}.", [P("A", "N7:5")]),
        plan_case("gap-4-merges", "Default max_gap 4: a 4-element hole merges.", [P("A", "N7:0"), P("B", "N7:5")]),
        plan_case("gap-5-splits", "A 5-element hole splits.", [P("A", "N7:0"), P("B", "N7:6")]),
        plan_case("max-gap-0", "max_gap 0: only touching elements merge.",
                  [P("A", "N7:0"), P("B", "N7:1"), P("C", "N7:3")], max_gap=0),
        plan_case("max-gap-10", "max_gap 10.", [P("A", "N7:0"), P("B", "N7:11"), P("C", "N7:23")], max_gap=10),
        plan_case("max-elements-word", "max_elements caps word files: 120 N elements split 100 + 20.",
                  [P(f"N{i:03d}", f"N7:{i}") for i in range(120)]),
        plan_case("max-elements-param", "max_elements 10.", [P(f"N{i:02d}", f"N7:{i}") for i in range(25)],
                  max_elements=10),
        plan_case("max-elements-1", "max_elements 1: one element per read.",
                  [P("A", "N7:0"), P("B", "N7:1")], max_elements=1),
        plan_case("request-size-float", "Default request_size 232 B: 58 floats per read.",
                  [P(f"F{i:02d}", f"F8:{i}", "float32") for i in range(70)]),
        plan_case("request-size-param", "request_size 100 B: 25 floats, 50 words per read.",
                  [P(f"F{i:02d}", f"F8:{i}", "float32") for i in range(30)]
                  + [P(f"N{i:02d}", f"N7:{i}") for i in range(60)], request_size=100),
        plan_case("request-size-clamped-255", "request_size 1000 is clamped to 255 B (one-byte PCCC size field): "
                  "127 words, 63 floats.",
                  [P(f"N{i:03d}", f"N7:{i}") for i in range(130)]
                  + [P(f"F{i:02d}", f"F8:{i}", "float32") for i in range(70)], request_size=1000, max_elements=500),
        plan_case("long-file-size", "L elements are 4 bytes: 58 per default read.",
                  [P(f"L{i:02d}", f"L9:{i}", "int32") for i in range(60)]),
        plan_case("gap-counts-toward-cap", "Holes count toward the element cap.",
                  [P("A", "N7:0"), P("B", "N7:4"), P("C", "N7:8"), P("D", "N7:12")], max_elements=10),
        plan_case("files-never-merge", "Different files (N7, N9, B3) never share a read; blocks sorted by file.",
                  [P("A", "N9:0"), P("B", "N7:0"), P("C", "B3:0", "uint16"), P("D", "N7:1")]),
        plan_case("io-merge-within-slot", "I/O words merge only within one slot.",
                  [P("A", "I:1.0"), P("B", "I:1.1"), P("C", "I:2.0"), P("D", "O:0.0"), P("E", "O:0.3")]),
        plan_case("status-words", "Status file words merge like data words.",
                  [P("A", "S:1"), P("B", "S:2"), P("C", "S:1/4", "bool")]),
        plan_case("timer-separate-reads", "Each T/C sub-element is its own one-element read.",
                  [P("A", "T4:0.ACC"), P("B", "T4:1.ACC"), P("C", "T4:0.PRE"), P("D", "T4:0.DN", "bool")]),
        plan_case("timer-same-sub-aliases", "The same timer sub-element under two names is one read.",
                  [P("A", "T4:0.ACC"), P("B", "t4:0.acc")]),
        plan_case("string-max-2", "ST reads hold at most 2 elements.",
                  [P("A", "ST9:0", "string"), P("B", "ST9:1", "string"), P("C", "ST9:2", "string")]),
        plan_case("string-max-2-at-255", "With request_size 255 three 84-byte ST elements would fit, but pycomm3 "
                  "accepts only {1} or {2}: still 2 per read.",
                  [P(f"S{i}", f"ST9:{i}", "string") for i in range(4)], request_size=255),
        plan_case("same-address-twice", "Two names on one address: one element, two items.",
                  [P("A", "N7:0"), P("B", "N7:0", "uint16")]),
        plan_case("bits-of-one-word", "Bits of one word and the word itself: one element; items ordered by "
                  "bit (word first), then name.",
                  [P("W", "B3:0", "uint16"), P("B2", "B3:0/2", "bool"), P("B1", "B3:0/1", "bool"),
                   P("B5", "B3/5", "bool")]),
        plan_case("bit-number-spans-elements", "B3/0 .. B3/47 span elements 0..2.",
                  [P("A", "B3/0", "bool"), P("B", "B3/31", "bool"), P("C", "B3/47", "bool")]),
        plan_case("io-bit-number-spans-words", "I:0/0 and I:0/35 span words 0..2 of slot 0.",
                  [P("A", "I:0/0", "bool"), P("B", "I:0/35", "bool")]),
        plan_case("disabled-ignored", "Disabled tags are not read.",
                  [P("ON", "N7:0"), P("OFF", "N7:1", enabled=False)]),
        plan_case("input-order-irrelevant", "Tags in reverse order plan the same.",
                  list(reversed([P(f"T{i:02d}", f"N7:{i * 3}") for i in range(10)]))),
        plan_case("empty", "No tags: no blocks.", []),
    ]


def pccc_params() -> list[Case]:
    tags = [P("A", "N7:0"), P("B", "N7:1")]
    return [
        plan_case("param-max-elements-0", "max_elements 0 is refused.", tags, max_elements=0),
        plan_case("param-max-gap-negative", "max_gap -1 is refused.", tags, max_gap=-1),
        plan_case("param-request-size-0", "request_size 0 is refused.", tags, request_size=0),
        plan_case("param-request-size-1", "request_size 1 B: smaller than one word, so one element per read.",
                  tags, request_size=1),
        plan_case("param-file-check-bogus", "file_check must be true or false.", tags, file_check="maybe"),
        plan_case("param-file-check-off", "file_check off is accepted (no plan change).", tags, file_check="off"),
        plan_case("param-path-odd-route", "A CIP route needs port/link pairs.", tags, path="1,0,2"),
        plan_case("param-path-route", "path 1,0 is accepted.", tags, path="1,0"),
        plan_case("param-null-is-default", "null params mean their defaults.", tags,
                  max_elements=None, max_gap=None, request_size=None),
        plan_case("param-unknown-ignored", "Unknown params are ignored.", tags, colour="blue"),
    ]


def pccc_random() -> list[Case]:
    rng = random.Random(20261121)
    cases: list[Case] = []
    for k in range(12):
        params: dict[str, Any] = {}
        if rng.random() < 0.5:
            params["max_gap"] = rng.choice([0, 1, 4, 10])
        if rng.random() < 0.5:
            params["max_elements"] = rng.choice([5, 20, 100, 300])
        if rng.random() < 0.5:
            params["request_size"] = rng.choice([20, 100, 232, 255, 600])
        tags: list[dict[str, Any]] = []
        for i in range(rng.randint(10, 60)):
            r = rng.random()
            fn = rng.choice([7, 9, 10])
            el = rng.randint(0, 80)
            if r < 0.3:
                tags.append(P(f"T{i:02d}", f"N{fn}:{el}", rng.choice(["int16", "uint16", "float32", "bool"])))
            elif r < 0.45:
                tags.append(P(f"T{i:02d}", f"N{fn}:{el}/{rng.randint(0, 15)}", "bool"))
            elif r < 0.55:
                tags.append(P(f"T{i:02d}", f"F{fn + 1}:{el}", "float32"))
            elif r < 0.65:
                tags.append(P(f"T{i:02d}", f"B3/{rng.randint(0, 800)}", "bool"))
            elif r < 0.72:
                tags.append(P(f"T{i:02d}", f"L{fn}:{el}", rng.choice(["int32", "uint32"])))
            elif r < 0.8:
                tags.append(P(f"T{i:02d}", f"T4:{rng.randint(0, 5)}.{rng.choice(['ACC', 'PRE', 'DN'])}",
                              "int16"))
            elif r < 0.86:
                tags.append(P(f"T{i:02d}", f"I:{rng.randint(0, 3)}.{rng.randint(0, 5)}"))
            elif r < 0.9:
                tags.append(P(f"T{i:02d}", f"ST9:{rng.randint(0, 5)}", "string"))
            elif r < 0.95 and tags:
                tags.append(P(f"T{i:02d}", rng.choice(tags)["address"], rng.choice(["int16", "bool"])))
            else:
                tags.append(P(f"T{i:02d}", rng.choice(["RTC:0.HR", "N7:300", "X_TAG", "F8:0/1"]), "int16"))
        rng.shuffle(tags)
        cases.append(plan_case(f"mix-{k:02d}", f"Seeded random mix {k}: {len(tags)} tags, "
                               f"params {sorted(params) or 'default'}.", tags, **params))
    return cases


# --- ab_pccc/decode ------------------------------------------------------------------------------

def pc_case(cid: str, desc: str, tags: list[dict[str, Any]], value: Any, **params: Any) -> Case:
    return cid, desc, {"params": params, "tags": tags, "value": encode_value(value)}


def pccc_words() -> list[Case]:
    return [
        pc_case("n-int16", "N words come back signed: int16 keeps -1.", [P("X", "N7:0")], -1),
        pc_case("n-uint16-masked", "uint16 on an N word masks -1 to 65535 (D-008).", [P("X", "N7:0", "uint16")], -1),
        pc_case("n-uint16-min", "uint16 masks -32768 to 32768.", [P("X", "N7:0", "uint16")], -32768),
        pc_case("n-float32", "float32 on an N word converts the integer (N66:10 case, D-008).",
                [P("X", "N66:10", "float32")], -123),
        pc_case("n-float64", "float64 on an N word.", [P("X", "N7:0", "float64")], 32767),
        pc_case("n-bool-nonzero", "bool on a word is true for any non-zero value.", [P("X", "N7:0", "bool")], -4),
        pc_case("n-bool-zero", "bool on a zero word is false.", [P("X", "N7:0", "bool")], 0),
        pc_case("n-array", "A {n} read returns a list; items take their offset.",
                [P("A", "N7:0"), P("B", "N7:2", "uint16"), P("C", "N7:3")], [10, 999, -1, -32768]),
        pc_case("n-bits-of-word", "Bits are decoded from the whole word: bit 15 of -32768 is set.",
                [P("B0", "N7:0/0", "bool"), P("B15", "N7:0/15", "bool"), P("W", "N7:0")], -32768),
        pc_case("b-bit-number", "Bit-number form across a 2-element read: B3/1 is element 0 bit 1, B3/17 element "
                "1 bit 1, B3/31 element 1 bit 15 (of a negative word).",
                [P("A", "B3/1", "bool"), P("B", "B3/16", "bool"), P("C", "B3/17", "bool"), P("D", "B3/31", "bool")],
                [2, -32766]),
        pc_case("b-word-uint16", "A B word typed uint16.", [P("X", "B3:0", "uint16")], -21846),
        pc_case("l-int32", "L elements are 32-bit: int32 keeps -1.", [P("X", "L9:0", "int32")], -1),
        pc_case("l-uint32", "uint32 on an L element masks -1 to 4294967295.", [P("X", "L9:0", "uint32")], -1),
        pc_case("l-int64", "int64 on an L element keeps the sign.", [P("X", "L9:0", "int64")], -2),
        pc_case("l-uint64", "uint64 on an L element masks to 64 bits.", [P("X", "L9:0", "uint64")], -2),
        pc_case("l-bit-31", "Bit 31 of a negative L element.", [P("X", "L9:0/31", "bool")], -(1 << 31)),
        pc_case("l-float32", "float32 on an L element.", [P("X", "L9:0", "float32")], 16777217),
        pc_case("s-word", "A status word with a bit.", [P("W", "S:1", "uint16"), P("B", "S:1/4", "bool")], 16),
        pc_case("io-words", "Input words of one slot, with a bit-number form spanning words.",
                [P("A", "I:1.0", "uint16"), P("B", "I:1/17", "bool")], [0xFFFF, 2]),
        pc_case("bool-word-value", "A bool value where a word is expected counts as an integer.",
                [P("X", "N7:0")], True),
        pc_case("bit-as-int16", "A bit typed int16 gives 0 or 1.", [P("X", "N7:0/2", "int16")], 4),
    ]


def pccc_float_string_tc() -> list[Case]:
    return [
        pc_case("f-float32", "F file float.", [P("X", "F8:0", "float32")], 1.5),
        pc_case("f-float64", "F file typed float64.", [P("X", "F8:0", "float64")], -0.1),
        pc_case("f-nan", "A NaN float passes through.", [P("X", "F8:0", "float32")], float("nan")),
        pc_case("f-array", "F {n} read.", [P("A", "F8:0", "float32"), P("B", "F8:1", "float32")], [0.25, -2.0]),
        pc_case("f-int-value", "An integer value on an F file converts to float.", [P("X", "F8:0", "float32")], 3),
        pc_case("st-string", "ST string.", [P("X", "ST9:0", "string")], "PUMP 1 RUN"),
        pc_case("st-pair", "ST {2} read.", [P("A", "ST9:0", "string"), P("B", "ST9:1", "string")], ["A", ""]),
        pc_case("t-acc", "Timer .ACC word.", [P("X", "T4:0.ACC")], 1234),
        pc_case("t-acc-uint16", "Timer .ACC typed uint16 masks a negative value.", [P("X", "T4:0.ACC", "uint16")], -1),
        pc_case("t-dn-bool", "Timer .DN: pycomm3 already extracted the bit (true).", [P("X", "T4:0.DN", "bool")], True),
        pc_case("t-dn-int", "Timer .DN given as integer 1.", [P("X", "T4:0.DN", "bool")], 1),
        pc_case("t-dn-zero", "Timer .DN false.", [P("X", "T4:0.DN", "bool")], 0),
        pc_case("t-dn-int16", "Timer .DN typed int16.", [P("X", "T4:0.DN", "int16")], True),
        pc_case("t-dn-nonzero-int", "A non-zero integer for a T/C bit is true (not bit 0 of it).",
                [P("X", "T4:0.DN", "bool")], 2),
        pc_case("c-acc-negative", "Counter .ACC stays signed in int16.", [P("X", "C5:0.ACC")], -5),
        pc_case("c-ov", "Counter .OV bit.", [P("X", "C5:0.OV", "bool")], False),
    ]


def pccc_errors() -> list[Case]:
    three = [P("A", "N7:0"), P("B", "N7:1"), P("C", "N7:2")]
    return [
        pc_case("none-reply", "No value in the reply: every tag fails.", three, None),
        pc_case("short-list", "A list shorter than the read fails the missing elements.", three, [1, 2]),
        pc_case("scalar-for-many", "A scalar for a {n} read covers only element 0.", three, 7),
        pc_case("none-element", "A none element fails only its tags.", three, [1, None, 3]),
        pc_case("str-in-word", "A string where a word is expected is a decode error.", [P("X", "N7:0")], "5"),
        pc_case("float-in-word", "A float where a word is expected is a decode error.", [P("X", "N7:0")], 1.0),
        pc_case("float-in-bit", "A float where a word with bits is expected.", [P("X", "N7:0/1", "bool")], 2.0),
        pc_case("str-in-float-file", "A string in a float file is a decode error.", [P("X", "F8:0", "float32")], "x"),
        pc_case("int-in-string-file", "An integer in a string file is a decode error.", [P("X", "ST9:0", "string")], 5),
        pc_case("list-element-in-word", "A nested list where a word is expected is a decode error.",
                [P("A", "N7:0"), P("B", "N7:1")], [[1, 2], 3]),
        pc_case("str-in-timer-bit", "A string for a timer bit is a decode error.", [P("X", "T4:0.DN", "bool")], "on"),
        pc_case("empty-list", "An empty list: every tag fails.", three, []),
        pc_case("long-list", "Extra elements are ignored.", three, [1, 2, 3, 4]),
    ]


SPECS: dict[str, list[FileSpec]] = {
    "ab_logix/plan": [
        ("addresses", "Valid symbolic address forms: names, members, indexes, program scope, .n bits.",
         logix_addresses()),
        ("rejected", "Rejected symbolic addresses go to invalid with a reason.", logix_rejected()),
        ("batching", "Read groups: max_batch, sorting, de-duplication, bits read through their integer (D-008).",
         logix_batching()),
        ("packing", "Request-size packing: greedy by response size against the connection size (D-008, D-025).",
         logix_packing()),
        ("params", "Driver params: refused values and accepted forms.", logix_params()),
        ("random_mix", "Seeded random mixes of address forms, dtypes and connection sizes.", logix_random()),
    ],
    "ab_logix/decode": [
        ("coerce", "pycomm3 values coerced to the configured dtype, or a decode error.", logix_coerce()),
        ("bits", ".n bits decoded from the returned integer.", logix_bits()),
        ("reply_shape", "Missing, short, long and none replies; aliases.", logix_reply_shape()),
    ],
    "ab_pccc/plan": [
        ("addresses", "Every supported PCCC address pattern (D-008).", pccc_addresses()),
        ("rejected", "Rejected PCCC addresses and dtypes go to invalid with a reason (D-008).", pccc_rejected()),
        ("blocks", "File blocks: gaps, element and byte caps (255 B), slots, T/C and ST reads (D-008).",
         pccc_blocks()),
        ("params", "Driver params: refused values and accepted forms.", pccc_params()),
        ("random_mix", "Seeded random mixes of file types, bits and limits.", pccc_random()),
    ],
    "ab_pccc/decode": [
        ("words", "Whole words to values and bits; N/B masking, N typed float32, signed L (D-008).", pccc_words()),
        ("float_string_tc", "Float and string files; timer and counter sub-elements.", pccc_float_string_tc()),
        ("errors", "Missing, short and wrongly typed replies.", pccc_errors()),
    ],
}
