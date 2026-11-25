"""KEPServerEX-compatible tag CSV import and export (#10)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencap.__main__ import app
from opencap.config.kep_csv import (
    COLUMNS,
    DTYPE_TO_KEP,
    KepCsvError,
    parse_kep_csv,
    read_kep_csv,
    render_kep_csv,
    write_kep_csv,
)
from opencap.config.kep_import import KEP_DTYPES, KEP_TYPE_NAMES
from opencap.config.models import Device, Scale, Tag
from tests.helpers.configdir import write_config

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kep_tags_sample.csv"
HEADER = ",".join(COLUMNS)
EXPECTED_HEADER = (
    "Tag Name,Address,Data Type,Respect Data Type,Client Access,Scan Rate,Scaling,Raw Low,Raw High,"
    "Scaled Low,Scaled High,Scaled Data Type,Clamp Low,Clamp High,Eng Units,Description,Negate Value"
)


def parse(*rows: str, driver: str = "modbus_tcp", header: str = HEADER, **kw):
    return parse_kep_csv("\r\n".join([header, *rows]) + "\r\n", driver, **kw)


def one(row_fields: str, **kw) -> Tag:
    result = parse(row_fields, **kw)
    assert not result.errors, result.errors
    return result.tags[0]


def test_header_is_kepserverex_spelling():
    assert ",".join(COLUMNS) == EXPECTED_HEADER
    assert render_kep_csv([]).split("\r\n")[0] == EXPECTED_HEADER


# --- the sample fixture ---------------------------------------------------------------------

def test_fixture_reads_fully():
    tags, warnings = read_kep_csv(FIXTURE, "modbus_tcp")
    by = {t.name: t for t in tags}
    assert [t.name for t in tags] == ["FLOW_1", "PUMP_1_RUN", "TOTAL", "LEVEL_1", "LEVEL_2", "Tank1.Temp",
                                      "Tank1.Mode", "Meter.Raw", "Label"]
    assert (by["FLOW_1"].dtype, by["FLOW_1"].scan_ms, by["FLOW_1"].description) == ("float32", None, "Inlet flow")
    assert by["PUMP_1_RUN"].dtype == "bool" and by["PUMP_1_RUN"].scan_ms == 500
    assert by["TOTAL"].description == "Totalizer, resettable" and by["TOTAL"].dtype == "uint32"
    assert by["LEVEL_1"].scale == Scale(type="linear", raw_lo=0, raw_hi=32767, eng_lo=0, eng_hi=10,
                                        clamp_lo=True, clamp_hi=True, negate=False, units="m")
    assert by["LEVEL_2"].scale == Scale(type="sqrt", raw_lo=0, raw_hi=4095, eng_lo=0, eng_hi=100,
                                        clamp_hi=True, negate=True, units="%")
    assert by["Tank1.Temp"].dtype == "int16" and by["Tank1.Mode"].dtype == "uint16"
    assert by["Meter.Raw"].enabled is False and all(t.enabled for t in tags if t.name != "Meter.Raw")
    assert len(warnings) == 4
    assert any("line 6" in w and "Read/Write" in w for w in warnings)
    assert any("line 7" in w and "Char" in w and "int16" in w for w in warnings)
    assert any("line 8" in w and "Byte" in w for w in warnings)
    assert any("line 9" in w and "BCD" in w and "disabled" in w for w in warnings)


def test_fixture_has_no_private_data():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "192." not in text and "10." not in text.replace("Raw High", "")


# --- type mapping -----------------------------------------------------------------------------

def test_every_dtype_has_a_kep_name_that_reads_back():
    for dtype, kep_name in DTYPE_TO_KEP.items():
        assert one(f"T,400001,{kep_name}").dtype == dtype
    assert set(DTYPE_TO_KEP) == set(KEP_DTYPES.values()) - set()


@pytest.mark.parametrize("code,name", sorted(KEP_TYPE_NAMES.items()))
def test_data_type_names_follow_kep_import(code, name):
    if code == -1:
        return
    result = parse(f"T,400001,{name}")
    assert not result.errors
    tag = result.tags[0]
    assert tag.dtype == KEP_DTYPES[code]
    assert tag.enabled is (code not in (10, 11, 12))
    mapping_warnings = [w for w in result.warnings if "not valid for driver" not in w]
    assert bool(mapping_warnings) is (code in (2, 3, 10, 11, 12))


def test_data_type_is_case_insensitive_and_trimmed():
    assert one("T,400001, dword ").dtype == "uint32"
    assert one("T,400001,SHORT").dtype == "int16"


def test_default_data_type_needs_a_device_default():
    result = parse("T,400001,Default")
    assert [str(e) for e in result.errors] == ["line 2: tag 'T': Data Type 'Default' and no device default type to use"]
    assert one("T,400001,Default", default_dtype="float32").dtype == "float32"
    assert one("T,400001,", default_dtype="int16").dtype == "int16"          # empty = Default


def test_unknown_default_type_and_driver():
    assert "unknown default type" in str(parse("T,400001,Word", default_dtype="int7").errors[0])
    bad = parse("T,400001,Word", driver="profibus")
    assert bad.errors[0].line == 0 and "unknown driver" in bad.errors[0].message


@pytest.mark.parametrize("data_type", ["Word Array", "Boolean Array", "Word[4]", "Quad", "Float64"])
def test_unsupported_types_fail_the_row(data_type):
    result = parse(f"T,400001,{data_type}")
    assert result.tags == [] and len(result.errors) == 1 and result.errors[0].line == 2


# --- other columns ---------------------------------------------------------------------------

def test_client_access():
    assert parse("T,400001,Word,1,Read Only").warnings == []
    rw = parse("T,400001,Word,1,Read/Write")
    assert len(rw.warnings) == 1 and "read-only" in rw.warnings[0] and "ADR-0008" in rw.warnings[0]
    assert len(parse("T,400001,Word,1,Sometimes").warnings) == 1
    assert parse("T,400001,Word").warnings == []


def test_scan_rate():
    assert one("T,400001,Word,1,Read Only,250").scan_ms == 250
    assert one("T,400001,Word,1,Read Only,0").scan_ms is None
    assert one("T,400001,Word,1,Read Only,").scan_ms is None
    assert one("T,400001,Word,1,Read Only,250.0").scan_ms == 250
    for bad in ("fast", "-5"):
        assert parse(f"T,400001,Word,1,Read Only,{bad}").errors


def test_scaling_columns():
    s = one("T,400001,Word,1,Read Only,0,Linear,0,100,-10,10,Double,1,0,degC,,1").scale
    assert s == Scale(type="linear", raw_lo=0, raw_hi=100, eng_lo=-10, eng_hi=10, clamp_lo=True, negate=True,
                      units="degC")
    assert one("T,400001,Word,1,Read Only,0,Square Root,0,10,0,1").scale.type == "sqrt"
    assert one("T,400001,Word,1,Read Only,0,None,0,100,0,1").scale is None          # columns ignored
    assert one("T,400001,Word,1,Read Only,0,Linear").scale == Scale()                  # KEPServerEX defaults
    assert one("T,400001,Word,1,Read Only,0,linear,,,,,,yes,TRUE,,,0").scale.clamp_lo is True
    for bad in ("Cubic", "Logarithmic"):
        assert "unknown Scaling" in str(parse(f"T,400001,Word,1,Read Only,0,{bad}").errors[0])
    assert parse("T,400001,Word,1,Read Only,0,Linear,abc,1,0,1").errors
    assert parse("T,400001,Word,1,Read Only,0,Linear,0,nan,0,1").errors
    assert parse("T,400001,Word,1,Read Only,0,Linear,0,1,0,1,Double,maybe").errors


def test_equal_raw_limits_warn():
    result = parse("T,400001,Word,1,Read Only,0,Linear,5,5,0,1")
    assert result.tags and "bad_decode" in result.warnings[0]


def test_group_path_names_are_dot_joined():
    assert one("Group.Sub.Tag,400001,Word").name == "Group.Sub.Tag"
    for bad in (".Tag", "Group.", "A..B", "_System.Tag", "_system.x"):
        assert parse(f"{bad},400001,Word").errors, bad


def test_required_fields_and_duplicates():
    assert "empty Tag Name" in str(parse(",400001,Word").errors[0])
    assert "no Address" in str(parse("T,,Word").errors[0])
    dup = parse("A,400001,Word", "A,400002,Word")
    assert [e.line for e in dup.errors] == [3] and "line 2" in dup.errors[0].message and len(dup.tags) == 1


def test_address_checked_against_the_driver():
    ok = parse("T,400001,Word")
    assert ok.warnings == []
    bad = parse("T,not-an-address,Word")
    assert bad.tags and len(bad.warnings) == 1 and "not valid for driver modbus_tcp" in bad.warnings[0]


# --- file format -----------------------------------------------------------------------------

def test_bom_column_order_case_and_extra_columns():
    text = "﻿description , ADDRESS,tag name,Data Type,Extra\r\nHello,400001,T,Word,zzz\r\n\r\n"
    result = parse_kep_csv(text, "modbus_tcp")
    assert not result.errors
    assert result.tags == [Tag(name="T", address="400001", dtype="uint16", description="Hello")]


def test_minimal_required_columns_only():
    result = parse_kep_csv("Tag Name,Address,Data Type\nA,400001,Word\nB,400002,Float\n", "modbus_tcp")
    assert [t.name for t in result.tags] == ["A", "B"] and not result.errors


def test_header_problems():
    assert "lacks required column(s): Address" in str(parse_kep_csv("Tag Name,Data Type\nA,Word\n", "modbus_tcp").errors[0])
    assert "repeats" in str(parse_kep_csv("Tag Name,Address,Data Type,Address\n", "modbus_tcp").errors[0])
    assert parse_kep_csv("", "modbus_tcp").errors[0].message.startswith("empty file")
    assert parse_kep_csv("\r\n\r\n", "modbus_tcp").errors[0].line == 1


def test_error_lines_count_physical_lines_including_quoted_newlines():
    text = HEADER + '\r\nA,400001,Word,1,Read Only,0,None,,,,,,,,,"two\r\nlines",\r\nB,400002,Quad\r\n'
    result = parse_kep_csv(text, "modbus_tcp")
    assert [t.name for t in result.tags] == ["A"] and result.tags[0].description == "two\r\nlines"
    assert [e.line for e in result.errors] == [4]


def test_too_many_fields_is_an_error():
    result = parse("A,400001,Word,1,Read Only,0,None,,,,,,,,,,,extra,more")
    assert result.errors and "fields" in result.errors[0].message


def test_read_raises_with_every_failing_row_and_keeps_warnings(tmp_path: Path):
    p = tmp_path / "t.csv"
    p.write_text("\r\n".join([HEADER, "A,400001,Char", "B,400002,Quad", "C,,Word", "D,400004,Word"]), encoding="utf-8")
    with pytest.raises(KepCsvError) as exc:
        read_kep_csv(p, "modbus_tcp")
    assert [e.line for e in exc.value.errors] == [3, 4]
    assert len(exc.value.warnings) == 1 and "line 2" in exc.value.warnings[0]


# --- write and round trip -------------------------------------------------------------------

def test_write_exact_text():
    tags = [
        Tag(name="FLOW", address="400001", dtype="float32", description='say "hi", ok'),
        Tag(name="G.LVL", address="400003", dtype="uint16", scan_ms=500,
            scale=Scale(raw_hi=32767, eng_hi=2.5, clamp_hi=True, units="m")),
    ]
    assert render_kep_csv(tags) == (
        EXPECTED_HEADER + "\r\n"
        'FLOW,400001,Float,1,Read Only,,None,,,,,,,,,"say ""hi"", ok",\r\n'
        "G.LVL,400003,Word,1,Read Only,500,Linear,0,32767,0,2.5,Double,0,1,m,,0\r\n"
    )


def test_disabled_tags_add_an_enabled_column():
    tags = [Tag(name="A", address="400001", dtype="uint16"), Tag(name="B", address="400002", dtype="uint16", enabled=False)]
    text = render_kep_csv(tags)
    assert text.split("\r\n")[0].endswith(",Negate Value,Enabled")
    assert parse_kep_csv(text, "modbus_tcp").tags == tags
    assert "Enabled" not in render_kep_csv(tags[:1])


def _random_tag(rng: random.Random, i: int) -> Tag:
    words = ["Pump", "Tank", "°C", "ü", "a,b", 'q"t', "x y", "A.B", "M3/h", "line\nbreak", ""]
    text = lambda: " ".join(rng.choice(words) for _ in range(rng.randint(0, 3))).strip() or None  # noqa: E731
    scale = None
    if rng.random() < 0.6:
        scale = Scale(
            type=rng.choice(["linear", "sqrt"]),
            raw_lo=rng.choice([0, -1, 0.1, 1e-7, 4095, 1e20]), raw_hi=rng.choice([100, 32767, 0.3, 1e300, 65535.5]),
            eng_lo=rng.choice([0, -273.15, 1 / 3]), eng_hi=rng.choice([10, 1e6, 99.99, 2**53 + 0.0]),
            clamp_lo=rng.random() < 0.5, clamp_hi=rng.random() < 0.5, negate=rng.random() < 0.5,
            units=rng.choice([None, "m", "%", "°C"]),
        )
    group = rng.choice(["", "G1.", "G1.Sub."])
    return Tag(name=f"{group}T{i}", address=str(400001 + i), dtype=rng.choice(sorted(DTYPE_TO_KEP)),
               scan_ms=rng.choice([None, 100, 5000]), scale=scale, enabled=rng.random() < 0.8, description=text())


@pytest.mark.parametrize("seed", range(40))
def test_round_trip_write_then_read(seed: int, tmp_path: Path):
    rng = random.Random(seed)
    tags = [_random_tag(rng, i) for i in range(rng.randint(1, 25))]
    p = tmp_path / "t.csv"
    write_kep_csv(tags, p)
    back, _ = read_kep_csv(p, "modbus_tcp")
    assert back == tags
    assert render_kep_csv(back) == p.read_bytes().decode("utf-8")                  # and write is stable


def test_read_then_write_is_semantically_identical(tmp_path: Path):
    tags, _ = read_kep_csv(FIXTURE, "modbus_tcp")
    p = tmp_path / "out.csv"
    write_kep_csv(tags, p)
    again, warnings = read_kep_csv(p, "modbus_tcp")
    assert again == tags
    assert warnings == [], warnings        # Char/Byte/Read-Write were already normalised; BCD stays disabled quietly
    third = tmp_path / "out2.csv"
    write_kep_csv(again, third)
    assert third.read_bytes() == p.read_bytes()


def test_write_is_utf8_crlf_without_bom(tmp_path: Path):
    p = tmp_path / "t.csv"
    write_kep_csv([Tag(name="Temp", address="400001", dtype="int16", description="°C ü")], p)
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and "°C ü".encode() in raw


# --- CLI ---------------------------------------------------------------------------------------

runner = CliRunner()


def test_cli_import_writes_json_tag_list(tmp_path: Path):
    out = tmp_path / "tags.json"
    res = runner.invoke(app, ["tags", "import", str(FIXTURE), "--driver", "modbus_tcp", "--out", str(out)])
    assert res.exit_code == 0, res.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [t["name"] for t in data][:2] == ["FLOW_1", "PUMP_1_RUN"] and len(data) == 9
    assert data[3]["scale"]["units"] == "m" and data[7]["enabled"] is False
    assert [Tag.model_validate(t) for t in data] == read_kep_csv(FIXTURE, "modbus_tcp")[0]
    assert "warning: line 7" in res.output


def test_cli_import_reports_failing_rows_and_exits_nonzero(tmp_path: Path):
    bad = tmp_path / "bad.csv"
    bad.write_text("\r\n".join([HEADER, "A,400001,Word", "B,400002,Quad", "C,,Word"]), encoding="utf-8")
    out = tmp_path / "tags.json"
    res = runner.invoke(app, ["tags", "import", str(bad), "--driver", "modbus_tcp", "--out", str(out)])
    assert res.exit_code == 1
    assert "error: line 3:" in res.output and "error: line 4:" in res.output and "2 row(s) failed" in res.output
    assert not out.exists()


def test_cli_import_default_type(tmp_path: Path):
    src = tmp_path / "d.csv"
    src.write_text(HEADER + "\r\nA,400001,Default\r\n", encoding="utf-8")
    out = tmp_path / "t.json"
    args = ["tags", "import", str(src), "--driver", "modbus_tcp", "--out", str(out)]
    assert runner.invoke(app, args).exit_code == 1
    assert runner.invoke(app, [*args, "--default-type", "float32"]).exit_code == 0
    assert json.loads(out.read_text(encoding="utf-8"))[0]["dtype"] == "float32"


def test_cli_export_from_config(tmp_path: Path):
    device = Device(id="Plant A/PLC1", channel="Plant A", name="PLC1", driver="modbus_tcp", host="192.0.2.10",
                    port=502, tags=[Tag(name="FLOW_1", address="400001", dtype="float32", description="Flow"),
                                    Tag(name="G.LVL", address="400003", dtype="uint16",
                                        scale=Scale(raw_hi=100, eng_hi=10, units="m"))])
    cfg = write_config(tmp_path, [device])
    out = tmp_path / "out.csv"
    res = runner.invoke(app, ["tags", "export", "Plant A/PLC1", str(out), "--config", str(cfg)])
    assert res.exit_code == 0, res.output
    tags, _ = read_kep_csv(out, "modbus_tcp")
    assert tags == device.tags and "wrote 2 tags" in res.output
    assert runner.invoke(app, ["tags", "export", "No/Such", str(out), "--config", str(cfg)]).exit_code == 1
