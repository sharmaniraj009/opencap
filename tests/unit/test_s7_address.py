"""S7 address parser (Kepware "Siemens TCP/IP Ethernet" syntax plus the Step 7 / Siemens forms).

One case per address form found in the project being migrated (`MD232`, `MW`, `MB`, `M10.3`,
`DB100,D04`, `DB100,D016`, `DBn,REALx`, `DBn,DBDx`, `DBn,Wx`, `DBn,Bx`, `DBn,Xx.y`, `C0`, `T0`),
the Siemens-native `DBn.DBDx` forms, the rest of Kepware's S7 data types, and the rejections.
"""

from __future__ import annotations

import pytest

from opencap.drivers.s7 import AREA_CODES, S7Params, parse_s7_address

# (address, dtype) -> (area, db, start, nbytes, kind, bit)
FORMS = [
    # --- forms in the project -----------------------------------------------------------
    ("MD232", "float32", ("M", 0, 232, 4, "dword", None)),
    ("MD232", "uint32", ("M", 0, 232, 4, "dword", None)),
    ("MD232", "int32", ("M", 0, 232, 4, "dword", None)),
    ("MW10", "uint16", ("M", 0, 10, 2, "word", None)),
    ("MW10", "int16", ("M", 0, 10, 2, "word", None)),
    ("MB7", "uint16", ("M", 0, 7, 1, "byte", None)),            # Kepware Byte, widened by the importer
    ("MB7", "int16", ("M", 0, 7, 1, "byte", None)),             # Kepware Char
    ("M10.3", "bool", ("M", 0, 10, 1, "bit", 3)),
    ("DB100,D04", "float32", ("DB", 100, 4, 4, "dword", None)),
    ("DB100,D016", "float32", ("DB", 100, 16, 4, "dword", None)),   # leading zero: byte 16
    ("DB100,D016", "uint32", ("DB", 100, 16, 4, "dword", None)),
    ("DB100,REAL8", "float32", ("DB", 100, 8, 4, "real", None)),
    ("DB100,DBD12", "float32", ("DB", 100, 12, 4, "dword", None)),
    ("DB100,DBW2", "int16", ("DB", 100, 2, 2, "word", None)),
    ("DB100,DBB3", "uint16", ("DB", 100, 3, 1, "byte", None)),
    ("DB100,DBX5.6", "bool", ("DB", 100, 5, 1, "bit", 6)),
    ("DB100,W2", "uint16", ("DB", 100, 2, 2, "word", None)),
    ("DB100,B3", "uint16", ("DB", 100, 3, 1, "byte", None)),
    ("DB100,X5.6", "bool", ("DB", 100, 5, 1, "bit", 6)),
    ("C0", "uint16", ("C", 0, 0, 2, "counter", None)),
    ("T0", "uint32", ("T", 0, 0, 2, "timer", None)),
    # --- Siemens-native and Step 7 forms ----------------------------------------------------
    ("DB100.DBD12", "float32", ("DB", 100, 12, 4, "dword", None)),
    ("DB100.DBW2", "uint16", ("DB", 100, 2, 2, "word", None)),
    ("DB100.DBB3", "uint16", ("DB", 100, 3, 1, "byte", None)),
    ("DB100.DBX5.6", "bool", ("DB", 100, 5, 1, "bit", 6)),
    ("DB100DBD12", "float32", ("DB", 100, 12, 4, "dword", None)),      # Kepware legacy alternate
    ("DB100DBX5.6", "bool", ("DB", 100, 5, 1, "bit", 6)),
    # --- other Kepware S7 data types -------------------------------------------------------------
    ("DB1,BYTE3", "uint16", ("DB", 1, 3, 1, "byte", None)),
    ("DB1,C3", "int16", ("DB", 1, 3, 1, "byte", None)),
    ("DB1,CHAR3", "int16", ("DB", 1, 3, 1, "byte", None)),
    ("DB1,WORD2", "uint16", ("DB", 1, 2, 2, "word", None)),
    ("DB1,I2", "int16", ("DB", 1, 2, 2, "word", None)),
    ("DB1,INT2", "int16", ("DB", 1, 2, 2, "word", None)),
    ("DB1,DWORD4", "uint32", ("DB", 1, 4, 4, "dword", None)),
    ("DB1,DI4", "int32", ("DB", 1, 4, 4, "dword", None)),
    ("DB1,DINT4", "int32", ("DB", 1, 4, 4, "dword", None)),
    ("DB1,LREAL8", "float64", ("DB", 1, 8, 8, "lreal", None)),
    ("DB1,LINT8", "int64", ("DB", 1, 8, 8, "lword", None)),
    ("DB1,LWORD8", "uint64", ("DB", 1, 8, 8, "lword", None)),
    ("DB1,B0.7", "bool", ("DB", 1, 0, 1, "byte", 7)),          # bit of a byte
    ("DB1,W0.15", "bool", ("DB", 1, 0, 2, "word", 15)),        # bit of a word value
    ("MD0.31", "bool", ("M", 0, 0, 4, "dword", 31)),
    ("DB1,LWORD0.63", "bool", ("DB", 1, 0, 8, "lword", 63)),
    ("DB1,STRING10.20", "string", ("DB", 1, 10, 22, "string", None)),   # max len + actual len + 20
    ("DB1,B0.10", "string", ("DB", 1, 0, 10, "rawstring", None)),       # raw, NUL-terminated
    ("MB4.6", "string", ("M", 0, 4, 6, "rawstring", None)),
    # --- areas ---------------------------------------------------------------------------------------
    ("IW0", "int16", ("I", 0, 0, 2, "word", None)),
    ("EW0", "int16", ("I", 0, 0, 2, "word", None)),            # SIMATIC (German) mnemonic
    ("I0.0", "bool", ("I", 0, 0, 1, "bit", 0)),
    ("IX1.7", "bool", ("I", 0, 1, 1, "bit", 7)),
    ("IB3", "uint16", ("I", 0, 3, 1, "byte", None)),
    ("ID4", "float32", ("I", 0, 4, 4, "dword", None)),
    ("IREAL8", "float32", ("I", 0, 8, 4, "real", None)),
    ("QB1", "uint16", ("Q", 0, 1, 1, "byte", None)),
    ("AB1", "uint16", ("Q", 0, 1, 1, "byte", None)),
    ("Q0.5", "bool", ("Q", 0, 0, 1, "bit", 5)),
    ("FD4", "float32", ("M", 0, 4, 4, "dword", None)),         # F = flag memory = M
    ("F20.3", "bool", ("M", 0, 20, 1, "bit", 3)),
    ("MI2", "int16", ("M", 0, 2, 2, "word", None)),
    ("MDI4", "int32", ("M", 0, 4, 4, "dword", None)),
    ("MREAL0", "float32", ("M", 0, 0, 4, "real", None)),
    ("Z3", "uint16", ("C", 0, 3, 2, "counter", None)),          # SIMATIC counter mnemonic
    ("C12", "int16", ("C", 0, 12, 2, "counter", None)),
    ("T7", "int32", ("T", 0, 7, 2, "timer", None)),
    # --- spelling ------------------------------------------------------------------------------------
    ("db100,real8", "float32", ("DB", 100, 8, 4, "real", None)),
    (" MD232 ", "float32", ("M", 0, 232, 4, "dword", None)),
]


@pytest.mark.parametrize(("address", "dtype", "want"), FORMS, ids=[f"{a.strip()}@{d}" for a, d, _ in FORMS])
def test_address_forms(address: str, dtype: str, want: tuple) -> None:
    a = parse_s7_address(address, dtype)
    assert (a.area, a.db, a.start, a.nbytes, a.kind, a.bit) == want
    assert a.dtype == dtype and a.text == address.strip()
    assert a.area_code == AREA_CODES[a.area]


def test_spellings_of_the_same_data_have_the_same_key() -> None:
    keys = {parse_s7_address(a, "float32").key for a in ("DB100,D12", "DB100,D012", "DB100,DBD12", "DB100.DBD12",
                                                          "DB100DBD12", "db100,dword12")}
    assert len(keys) == 1
    assert parse_s7_address("DB100,REAL12", "float32").key != parse_s7_address("DB100,D12", "float32").key
    assert parse_s7_address("DB100,D12", "float32").key != parse_s7_address("DB100,D12", "uint32").key
    assert parse_s7_address("MW0", "int16").key != parse_s7_address("MW0", "uint16").key


def test_arrays() -> None:
    a = parse_s7_address("DB1,W0[4]", "uint16")
    assert (a.dims, a.count, a.size, a.nbytes) == ((4,), 4, 2, 8)
    b = parse_s7_address("MD0[2][3]", "float32")
    assert (b.dims, b.count, b.nbytes) == ((2, 3), 6, 24)
    c = parse_s7_address("DB1,B10[5]", "uint16")
    assert (c.start, c.nbytes) == (10, 5)
    d = parse_s7_address("DB2,REAL4[3]", "float32")
    assert (d.start, d.end, d.nbytes) == (4, 16, 12)


def test_units_for_timers_and_counters_are_elements() -> None:
    t = parse_s7_address("T5", "uint32")
    assert (t.start, t.units, t.end, t.nbytes) == (5, 1, 6, 2)
    c = parse_s7_address("C0", "uint16")
    assert (c.start, c.units, c.end) == (0, 1, 1)
    m = parse_s7_address("MD232", "float32")
    assert (m.units, m.end) == (4, 236)


def test_string_default_length_follows_the_pdu() -> None:
    assert parse_s7_address("DB1,STRING0", "string").str_len == 254
    small = S7Params.from_mapping({"max_pdu": 240})
    assert parse_s7_address("DB1,STRING0", "string", small).str_len == 210


REJECTED = [
    ("MD232", "bool", "needs a bit number"),
    ("M10", "bool", "needs a bit number"),
    ("MW10", "float32", "16-bit"),
    ("MB0", "float32", "8-bit"),
    ("DB1,REAL0", "uint32", "REAL holds a float"),
    ("DB1,LREAL0", "float32", "LREAL holds a 64-bit float"),
    ("DB1,D0", "float64", "32-bit"),
    ("DB0,W0", "uint16", "DB number 0 out of range 1..65535"),
    ("DB65536,W0", "uint16", "DB number 65536 out of range"),
    ("DB1,X0.8", "bool", "bit 8 out of range 0..7"),
    ("M10.8", "bool", "bit 8 out of range 0..7"),
    ("DB1,W0.16", "bool", "bit 16 out of range 0..15"),
    ("MD0.32", "bool", "bit 32 out of range 0..31"),
    ("DB1,X0", "bool", "needs a bit number"),
    ("DB1,W65535", "uint16", "past the end"),
    ("MD65533", "float32", "past the end"),
    ("DB1,W0[32769]", "uint16", "past the end"),
    ("PIW0", "int16", "peripheral"),
    ("PQW0", "int16", "peripheral"),
    ("PEW0", "int16", "peripheral"),
    ("DB1,DT0", "string", "not supported"),
    ("DB1,DATE0", "string", "not supported"),
    ("DB1,TIME0", "string", "not supported"),
    ("DB1,T0", "string", "not supported"),
    ("DB1,TOD0", "string", "not supported"),
    ("DB1,KH30", "uint16", "unknown S7 data type 'KH'"),
    ("DB1,X0[4]", "bool", "arrays of bools"),
    ("DB1,STRING0.10[2]", "string", "arrays of strings"),
    ("DB1,W0[2][3][4]", "uint16", "at most 2 array dimensions"),
    ("DB1,W0[0]", "uint16", "array dimension must be >= 1"),
    ("DB1,STRING0.255", "string", "string length 255 out of range 1..254"),
    ("DB1,STRING0.0", "string", "string length 0 out of range"),
    ("DB1,B0.933", "string", "string length 933 out of range 1..932"),
    ("DB1,W0", "string", "string"),
    ("MB0", "string", "needs a length"),
    ("T0", "float32", "timer"),
    ("T0", "uint16", "timer"),
    ("C0", "float32", "counter"),
    ("T0[2]", "uint32", "arrays"),
    ("C0.1", "bool", "counter"),
    ("DB1.D0", "float32", "Siemens form"),
    ("DB1.W0", "uint16", "Siemens form"),
    ("DB1", "uint16", "not an S7 address"),
    ("XYZ", "uint16", "not an S7 address"),
    ("", "uint16", "empty address"),
    ("MW", "uint16", "not an S7 address"),
    ("MD232", "nonsense", "unknown dtype"),
]


@pytest.mark.parametrize(("address", "dtype", "reason"), REJECTED, ids=[f"{a or 'empty'}@{d}" for a, d, _ in REJECTED])
def test_rejected(address: str, dtype: str, reason: str) -> None:
    with pytest.raises(ValueError, match="s7 address") as info:
        parse_s7_address(address, dtype)
    assert reason in str(info.value)


def test_params_validation() -> None:
    p = S7Params.from_mapping({"rack": 0, "slot": 2, "max_pdu": 960, "link_type": 3, "byte_order_big": 0,
                               "unknown_key": "ignored"})
    assert (p.rack, p.slot, p.max_pdu, p.byte_order_big, p.connection_type) == (0, 2, 960, False, 3)
    assert S7Params.from_mapping(None).connection_type == 3          # Kepware default "PC"
    assert S7Params.from_mapping({"link_type": None}).connection_type == 3
    for value, ctype in ((1, 1), (2, 2), (3, 3), ("PG", 1), ("op", 2), ("PC", 3)):
        assert S7Params.from_mapping({"link_type": value}).connection_type == ctype
    for bad in ({"rack": 8}, {"slot": 32}, {"max_pdu": 200}, {"max_pdu": 1920}, {"link_type": 4},
                {"link_type": "XX"}, {"byte_order_big": "yes"}, {"max_gap": -1}, {"nonnormal_float": "nan"}):
        with pytest.raises(ValueError, match="s7 param"):
            S7Params.from_mapping(bad)
