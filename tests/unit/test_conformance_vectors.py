"""The shared conformance vectors (tests/conformance/, ADR-0014) are what the Python reference
does now, and they are sharp enough to catch a broken branch.

- Every vector's `expected` equals what the Python reference makes of its `input`.
- The files are exactly what `scripts/gen_conformance.py` writes (nothing stale, missing or extra).
- Mutants of the reference (a flipped word order, a wrong bit gap, ...) fail vectors.

After a deliberate behaviour change: `python scripts/gen_conformance.py`, review the diff.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import math
import struct
from collections.abc import Callable
from pathlib import Path

import pytest

from opencap.config.models import Scale
from opencap.drivers import modbus_tcp
from opencap.north import payload as payload_mod
from tests.conformance import reference
from tests.conformance.vectors import (
    AREAS,
    FORMAT,
    DecodeErrorValue,
    Vector,
    decode_value,
    encode_value,
    load,
    load_all,
    load_file,
    render,
    same,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS = load_all()


def _generator():
    spec = importlib.util.spec_from_file_location("gen_conformance", ROOT / "scripts" / "gen_conformance.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mismatch(v: Vector) -> str | None:
    """None if the Python reference reproduces `v`, else a readable report."""
    try:
        actual = reference.run(v.area, v.input)
    except Exception as exc:   # a vector the reference cannot even run is a failure, not a crash
        actual = {"exception": f"{type(exc).__name__}: {exc}"}
    if same(v.expected, actual):
        return None
    one = lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True)  # noqa: E731
    return f"{v.key}: {v.description}\n  expected {one(v.expected)}\n  actual   {one(actual)}"


# --- the files ---------------------------------------------------------------------------------

def test_enough_vectors_in_every_area():
    assert len(VECTORS) >= 150
    for area in AREAS:
        assert len(load(area)) >= 20, area
    assert len({v.key for v in VECTORS}) == len(VECTORS)
    assert all(v.description.strip() for v in VECTORS)


@pytest.mark.parametrize("vector", VECTORS, ids=[v.key for v in VECTORS])
def test_python_reference_matches_vector(vector: Vector):
    problem = _mismatch(vector)
    assert problem is None, problem


def test_files_are_what_the_generator_writes():
    gen = _generator()
    files = gen.build()
    stale = [p.relative_to(ROOT).as_posix() for p, text in files.items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    extra = [p.relative_to(ROOT).as_posix() for p in gen.existing() - files.keys()]
    assert not stale and not extra, (
        f"stale or missing: {stale}; not generated: {extra}. Run `python scripts/gen_conformance.py` "
        "and review the diff")


def test_generator_check_mode_passes(capsys):
    assert _generator().main(["--check"]) == 0, capsys.readouterr().out


# --- sharpness: mutants of the reference must fail vectors --------------------------------------

def _swap_word_order(first: int, second: int, first_word_low: bool) -> int:
    return (first << 16) | second if first_word_low else (second << 16) | first


def _float_keeping_denormals(u: int, width: int, p: modbus_tcp.ModbusParams) -> float:
    exp, top, fmt = ((u >> 23) & 0xFF, 0xFF, ">f") if width == 32 else ((u >> 52) & 0x7FF, 0x7FF, ">d")
    if exp == top and p.nonnormal_float == "zero":       # NaN/Inf replaced, denormals wrongly kept
        return 0.0
    return struct.unpack(fmt, u.to_bytes(width // 8, "big"))[0]


def _mutants() -> dict[str, tuple[str, Callable[[pytest.MonkeyPatch], None]]]:
    """name -> (area whose vectors must catch it, how to break the reference)."""
    from_mapping = modbus_tcp.ModbusParams.from_mapping
    clean_value = payload_mod.clean_value
    apply = Scale.apply
    return {
        "first_word_low inverted": (
            "modbus/decode", lambda mp: mp.setattr(modbus_tcp, "_dword", _swap_word_order)),
        "byte_order_big ignored": (
            "modbus/decode", lambda mp: mp.setattr(modbus_tcp.ModbusParams, "from_mapping", classmethod(
                lambda cls, params: dataclasses.replace(from_mapping(params), byte_order_big=True)))),
        "denormals kept in zero mode": (
            "modbus/decode", lambda mp: mp.setattr(modbus_tcp, "_float", _float_keeping_denormals)),
        "bit gap = max_gap": (
            "modbus/plan", lambda mp: mp.setattr(
                modbus_tcp.ModbusParams, "max_gap_for", lambda self, fc: self.max_gap)),
        "no protocol cap": (
            "modbus/plan", lambda mp: mp.setattr(
                modbus_tcp.ModbusParams, "max_span", lambda self, fc: getattr(self, modbus_tcp._FC_LIMIT_PARAM[fc]))),
        "clamps ignored": (
            "scaling", lambda mp: mp.setattr(
                Scale, "apply", lambda self, raw: apply(self.model_copy(update={"clamp_lo": False, "clamp_hi": False}),
                                                        raw))),
        "-0.0 kept in the payload": (
            "payload", lambda mp: mp.setattr(
                payload_mod, "clean_value",
                lambda v, d: round(v, d) if isinstance(v, float) and math.isfinite(v) else clean_value(v, d))),
    }


@pytest.mark.parametrize("name", list(_mutants()))
def test_a_mutant_of_the_reference_fails_vectors(name: str, monkeypatch: pytest.MonkeyPatch):
    area, patch = _mutants()[name]
    patch(monkeypatch)
    failing = [v.key for v in load(area) if _mismatch(v) is not None]
    assert failing, f"mutant {name!r} passes every {area} vector: the vectors are too weak"


# --- the loader and the value codec -------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None, True, False, 0, -1, (1 << 64) - 1, -(1 << 63), 0.0, -0.0, 1.5, 1e16, 5e-324, math.inf, -math.inf,
    "", "ünï\x00", [], [1, [2.5, None], "x"],
])
def test_value_codec_round_trips(value):
    enc = encode_value(value)
    assert json.loads(json.dumps(enc)) == enc
    back = decode_value(enc)
    assert type(back) is type(value)
    assert repr(back) == repr(value)


def test_value_codec_keeps_types_apart():
    assert encode_value(1) != encode_value(1.0) != encode_value(True)
    assert encode_value(0.0) != encode_value(-0.0)
    assert encode_value(math.nan) == {"t": "float", "v": "nan"}
    assert math.isnan(decode_value({"t": "float", "v": "nan"}))
    err = encode_value(modbus_tcp.DecodeError("short reply"))
    assert err == {"t": "decode_error", "v": "short reply"}
    assert decode_value(err) == DecodeErrorValue("short reply")
    with pytest.raises(TypeError):
        encode_value(object())
    with pytest.raises(ValueError):
        decode_value({"t": "complex", "v": "1j"})


def test_same_is_strict_about_types_and_lenient_about_nan():
    assert same({"a": [1, "x"]}, {"a": [1, "x"]})
    assert not same({"a": 1}, {"a": 1.0})
    assert not same({"a": True}, {"a": 1})
    assert not same([1], [1, 2])
    assert not same({"a": 1}, {"b": 1})
    assert same([math.nan], [math.nan])


def _write(tmp_path: Path, area: str, vectors: list[dict], fmt: int = FORMAT) -> Path:
    path = tmp_path / area / "x.json"
    path.parent.mkdir(parents=True)
    text = render(area, "test file", vectors).replace(f'"format": {FORMAT}', f'"format": {fmt}')
    path.write_text(text, encoding="utf-8")
    return path


def test_loader_reads_a_rendered_file(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.conformance.vectors.ROOT", tmp_path)
    path = _write(tmp_path, "scaling", [{"id": "a", "description": "d", "input": {}, "expected": {"value": 1}}])
    [v] = load_file(path)
    assert (v.area, v.file, v.id, v.key, v.expected) == ("scaling", "scaling/x.json", "a", "scaling/x/a", {"value": 1})


@pytest.mark.parametrize("vectors, fmt, match", [
    ([{"id": "a", "description": "d", "input": {}, "expected": {}}], 2, "format"),
    ([{"id": "a", "description": "d", "input": {}, "expected": {}}] * 2, FORMAT, "duplicate"),
    ([{"id": "Bad Id", "description": "d", "input": {}, "expected": {}}], FORMAT, "bad"),
    ([{"id": "a", "description": " ", "input": {}, "expected": {}}], FORMAT, "description"),
])
def test_loader_rejects_malformed_files(tmp_path, monkeypatch, vectors, fmt, match):
    monkeypatch.setattr("tests.conformance.vectors.ROOT", tmp_path)
    with pytest.raises(ValueError, match=match):
        load_file(_write(tmp_path, "scaling", vectors, fmt))


def test_loader_rejects_a_file_in_the_wrong_area(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.conformance.vectors.ROOT", tmp_path)
    path = _write(tmp_path, "payload", [{"id": "a", "description": "d", "input": {}, "expected": {}}])
    path.write_text(path.read_text(encoding="utf-8").replace('"area": "payload"', '"area": "scaling"'),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="area"):
        load_file(path)
    with pytest.raises(ValueError, match="unknown conformance area"):
        load("nope")
