"""Omron FINS conformance vector inputs: KEPServerEX address parsing (ADR-0014, #13, #166).

Case data only: `scripts/gen_conformance.py` imports `SPECS` from here, computes every `expected`
with `tests/conformance/reference.py` and writes `tests/conformance/omron_fins/addresses/`.

There is no Python FINS driver (Rust-first, ADR-0002), so the reference is the simulator's own
address parser (`tests/helpers/fins_sim.py: parse_address`). To keep the vectors independent of
that parser, every case here also carries the **hand-written** expectation from the sources below;
`tests/unit/test_fins_sim.py` checks that the generated files equal it, so a parser bug cannot
silently become a vector.

Sources: PTC "Omron FINS Ethernet Driver" manual v1.083, CS1 (p.57-61), CJ1 (p.62-66) and CJ2
(p.66-70) addressing tables, for syntax, ranges and data types; Omron W342 5-2-2 for the area codes.
docs/protocols/omron-fins.md section 5 summarises them.
"""

from __future__ import annotations

import re
from typing import Any

Case = tuple[str, str, dict[str, Any]]          # (id, description, input)
FileSpec = tuple[str, str, list[Case]]          # (file stem, file description, cases)


def V(address: str, area_code: str, word: int, kind: str, words: int, *, bit: int | None = None,
      dtype: str | None = None, model: str | None = None, dims: list[int] | None = None,
      str_len: int | None = None, str_order: str | None = None, bcd: bool = False,
      note: str = "") -> tuple[dict[str, Any], dict[str, Any], str]:
    """A valid address: (input, hand-written expected, note)."""
    inp = {"address": address, **({"dtype": dtype} if dtype else {}), **({"model": model} if model else {})}
    exp = {"area_code": area_code, "word": word, "bit": bit, "kind": kind, "dims": dims or [],
           "str_len": str_len, "str_order": str_order, "bcd": bcd, "words": words}
    return inp, exp, note


def X(address: str, dtype: str | None, model: str | None, reason: str, why: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """An address KEPServerEX refuses: (input, hand-written expected {error: reason}, why)."""
    inp = {"address": address, **({"dtype": dtype} if dtype else {}), **({"model": model} if model else {})}
    return inp, {"error": reason}, why


VALID = [
    V('D100', '0x82', 100, 'word', 1, note='default type Word'),
    V('D0', '0x82', 0, 'word', 1),
    V('D32767', '0x82', 32767, 'word', 1, note='last DM word'),
    V('D00100', '0x82', 100, 'word', 1, note='leading zeros (the manual writes D00000-D32767)'),
    V('D100', '0x82', 100, 'word', 1, dtype='int16'),
    V('D100', '0x82', 100, 'word', 2, dtype='float32'),
    V('D100', '0x82', 100, 'word', 2, dtype='int32'),
    V('D32766', '0x82', 32766, 'word', 2, dtype='uint32', note='last DWord in DM'),
    V('D200', '0x82', 200, 'word', 1, dtype='bcd16', note='KEPServerEX BCD'),
    V('D200', '0x82', 200, 'word', 2, dtype='bcd32', note='KEPServerEX LBCD'),
    V('D0D', '0x82', 0, 'word', 1, bcd=True, note="'D' suffix: Word as unsigned BCD"),
    V('D0D', '0x82', 0, 'word', 1, dtype='int16', bcd=True, note="'D' suffix on a Short: signed BCD"),
    V('D10D', '0x82', 10, 'word', 2, dtype='uint32', bcd=True, note="'D' suffix on a DWord"),
    V('D100.0', '0x02', 100, 'bit', 1, bit=0, dtype='bool'),
    V('D100.15', '0x02', 100, 'bit', 1, bit=15, dtype='bool'),
    V('CIO0', '0xB0', 0, 'word', 1),
    V('CIO10', '0xB0', 10, 'word', 2, dtype='float32'),
    V('CIO6143', '0xB0', 6143, 'word', 1, note='last CIO word'),
    V('CIO0010.05', '0x30', 10, 'bit', 1, bit=5, dtype='bool', note='CIOxxxx.bb as the manual writes it'),
    V('CIO10.05', '0x30', 10, 'bit', 1, bit=5, dtype='bool'),
    V('CIO10.5', '0x30', 10, 'bit', 1, bit=5, dtype='bool'),
    V('W0', '0xB1', 0, 'word', 1),
    V('W511', '0xB1', 511, 'word', 1),
    V('W3.07', '0x31', 3, 'bit', 1, bit=7, dtype='bool'),
    V('H20', '0xB2', 20, 'word', 1),
    V('H1535', '0xB2', 1535, 'word', 1, note='H512-H1535: function block holding area'),
    V('H511.15', '0x32', 511, 'bit', 1, bit=15, dtype='bool'),
    V('A448', '0xB3', 448, 'word', 1),
    V('A959', '0xB3', 959, 'word', 1, note='last AR word on CS1/CJ1'),
    V('A100.03', '0x33', 100, 'bit', 1, bit=3, dtype='bool'),
    V('A1471', '0xB3', 1471, 'word', 1, model='CJ2'),
    V('A10000', '0xB3', 10000, 'word', 1, model='CJ2', note='CJ2 A10000-A11535'),
    V('E00:00100', '0xA0', 100, 'word', 1, note='EM bank 0 (decimal bank number)'),
    V('E01:100', '0xA1', 100, 'word', 1),
    V('E12:32767', '0xAC', 32767, 'word', 1, note='last bank on CS1/CJ1 (bank 12 = C hex)'),
    V('E12:32766', '0xAC', 32766, 'word', 2, dtype='float32'),
    V('E15:0', '0xAF', 0, 'word', 1, model='CJ2', note='bank 15 = F hex'),
    V('E16:0', '0x60', 0, 'word', 1, model='CJ2', note='bank 16 = 10 hex: word code 60'),
    V('E24:5', '0x68', 5, 'word', 1, model='CJ2', note='bank 24 = 18 hex, the last'),
    V('E00:00100.03', '0x20', 100, 'bit', 1, bit=3, dtype='bool'),
    V('E16:1.02', '0xE0', 1, 'bit', 1, bit=2, dtype='bool', model='CJ2', note='bank 10 hex bit code E0'),
    V('E00100', '0x98', 100, 'word', 1, note='current EM bank'),
    V('E100.04', '0x0A', 100, 'bit', 1, bit=4, dtype='bool', note='current EM bank, bit'),
    V('T0000', '0x89', 0, 'word', 1, note='timer present value; default type BCD'),
    V('T4095', '0x89', 0xfff, 'word', 1),
    V('T7', '0x89', 7, 'word', 1, dtype='uint16'),
    V('TS0007', '0x09', 7, 'flag', 1, dtype='bool', note='timer completion flag'),
    V('C3', '0x89', 0x8003, 'word', 1, note='counter n is FINS address 0x8000 + n'),
    V('C4095', '0x89', 0x8fff, 'word', 1),
    V('CS4', '0x09', 0x8004, 'flag', 1, dtype='bool', note='counter completion flag'),
    V('TK00', '0x06', 0, 'flag', 1, dtype='bool', note='task flag'),
    V('TK31', '0x06', 31, 'flag', 1, dtype='bool'),
    V('TK127', '0x06', 127, 'flag', 1, dtype='bool', model='CJ2'),
    V('IR00', '0xDC', 0x100, 'word', 1, note='index register: one 4-byte element, default DWord'),
    V('IR15', '0xDC', 0x10f, 'word', 1, dtype='float32'),
    V('DR00', '0xBC', 0x200, 'word', 1, note='data register'),
    V('DR14', '0xBC', 0x20e, 'word', 2, dtype='int32'),
    V('D01000.100H', '0x82', 1000, 'string', 50, dtype='string', str_len=100, str_order='H', note="the manual's example: 100 characters, high byte first"),
    V('D01100.078L', '0x82', 1100, 'string', 39, dtype='string', str_len=78, str_order='L', note="the manual's example: low byte first"),
    V('D02000.055D', '0x82', 2000, 'string', 55, dtype='string', str_len=55, str_order='D', note="the manual's example: one character per word, high byte"),
    V('D02200.037E', '0x82', 2200, 'string', 37, dtype='string', str_len=37, str_order='E', note="the manual's example: one character per word, low byte"),
    V('H0.2H', '0xB2', 0, 'string', 1, dtype='string', str_len=2, str_order='H'),
    V('D32767.002H', '0x82', 32767, 'string', 1, dtype='string', str_len=2, str_order='H'),
    V('D100[4]', '0x82', 100, 'word', 4, dtype='int16', dims=[4]),
    V('D100[4]', '0x82', 100, 'word', 8, dtype='float32', dims=[4]),
    V('D100[2][3]', '0x82', 100, 'word', 6, dtype='uint16', dims=[2, 3]),
    V('D100 [2] [3]', '0x82', 100, 'word', 6, dtype='uint16', dims=[2, 3], note='spaces as the manual prints Dxxxx [rows] [cols]'),
    V('W10[2][2]', '0xB1', 10, 'word', 8, dtype='uint32', dims=[2, 2]),
]

INVALID = [
    X('', None, None, 'syntax', 'empty'),
    X('D', None, None, 'syntax', 'no number'),
    X('D-1', None, None, 'syntax', 'negative'),
    X('D32768', None, None, 'range', 'past the end of DM'),
    X('D32767', 'float32', None, 'range', 'a 32-bit value needs D32767 and D32768'),
    X('CIO6144', None, None, 'range', 'past the end of CIO'),
    X('W512', None, None, 'range', 'past the end of WR'),
    X('H1536', None, None, 'range', 'past the end of HR'),
    X('A960', None, None, 'range', 'past the end of AR on CS1/CJ1'),
    X('A10000', None, 'CJ1', 'range', 'A10000-A11535 exist on CJ2 only'),
    X('A1472', None, 'CJ2', 'range', 'gap between A1471 and A10000'),
    X('T4096', None, None, 'range', 'past the last timer'),
    X('C4096', None, None, 'range', 'past the last counter'),
    X('TK32', None, 'CJ1', 'range', 'TK00-TK31 on CS1/CJ1'),
    X('IR16', None, None, 'range', 'IR00-IR15'),
    X('IR00', 'uint16', None, 'unsupported', 'index registers are 32-bit only'),
    X('DR16', None, None, 'range', 'DR00-DR15'),
    X('DR15', 'int32', None, 'range', 'a 32-bit value needs DR15 and DR16'),
    X('E00:32768', None, None, 'range', 'past the end of an EM bank'),
    X('E13:0', None, 'CJ1', 'range', 'EM banks 0-12 on CS1/CJ1'),
    X('E25:0', None, 'CJ2', 'range', 'EM banks 0-24 on CJ2'),
    X('E0_100', None, None, 'syntax', "Omron's W342 notation, not KEPServerEX syntax (E00:00100)"),
    X('EM100', None, None, 'syntax', 'not KEPServerEX syntax (the current bank is E00100)'),
    X('D100.16', None, None, 'range', 'bit above 15'),
    X('D100.', None, None, 'syntax', 'bit missing'),
    X('D1.2.3', None, None, 'syntax', 'two bit numbers'),
    X('X100', None, None, 'syntax', 'unknown area'),
    X('DM100', None, None, 'syntax', 'not KEPServerEX syntax for DM'),
    X('TS1.02', None, None, 'unsupported', 'a completion flag has no bit'),
    X('T1.02', None, None, 'unsupported', 'timer present values have no bits'),
    X('D100.000H', None, None, 'range', 'string of length 0'),
    X('D100.009L', None, None, 'unsupported', 'two-per-word strings have an even length'),
    X('D100.514H', None, None, 'range', 'two-per-word strings are at most 512 characters'),
    X('D100.257D', None, None, 'range', 'one-per-word strings are at most 256 characters'),
    X('D32767.004H', None, None, 'range', 'string runs past the end of DM'),
    X('D100[0]', None, None, 'unsupported', 'zero array dimension'),
    X('D100.05[2]', None, None, 'unsupported', 'Boolean arrays are not supported'),
    X('D100[1][2][3]', None, None, 'unsupported', 'three dimensions'),
    X('D32767[2]', None, None, 'range', 'array runs past the end of DM'),
    X('T0D', None, None, 'unsupported', "'D' suffix not allowed where the default type is BCD"),
    X('D0D', 'float32', None, 'unsupported', "'D' suffix not allowed on Float"),
    X('D0D', 'bcd16', None, 'unsupported', "'D' suffix not allowed on a BCD tag"),
    X('D100', 'bool', None, 'unsupported', 'Boolean needs a bit address'),
]


def _id(inp: dict[str, Any], seen: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", inp["address"].lower()).strip("-") or "empty"
    for extra in (inp.get("dtype"), inp.get("model")):
        if extra:
            base += "-" + extra.lower()
    cid, n = base, 2
    while cid in seen:
        cid, n = f"{base}-{n}", n + 1
    seen.add(cid)
    return cid


def _cases(rows: list[tuple[dict[str, Any], dict[str, Any], str]], default: str) -> list[Case]:
    seen: set[str] = set()
    out = []
    for inp, _, note in rows:
        what = f"{inp['address'] or '(empty)'}" + (f" as {inp['dtype']}" if "dtype" in inp else "") \
            + f" on {inp.get('model', 'CJ1')}"
        out.append((_id(inp, seen), f"{what}: {note}" if note else f"{what}: {default}", inp))
    return out


def hand_written() -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
    """file stem -> [(input, hand-written expected)], in file order (checked by test_fins_sim)."""
    return {"valid": [(i, e) for i, e, _ in VALID], "invalid": [(i, e) for i, e, _ in INVALID]}


SPECS: dict[str, list[FileSpec]] = {
    "omron_fins/addresses": [
        ("valid", "KEPServerEX Omron FINS addresses (CS1/CJ1/CJ2) -> FINS area code, word address, bit, "
                  "kind, dims, string length/order, BCD suffix and the words a read covers.",
         _cases(VALID, "accepted")),
        ("invalid", "Addresses KEPServerEX refuses, with the reason category (syntax, range, unsupported).",
         _cases(INVALID, "refused")),
    ],
}
