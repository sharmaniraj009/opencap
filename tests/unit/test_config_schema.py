"""Config JSON Schema: drift test and conformance fixtures (ADR-0003)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from opencap.config import schema as cfg_schema  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schema"
FIXTURES = ROOT / "tests" / "conformance" / "config"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(doc: str):
    schema = _load(SCHEMA_DIR / cfg_schema.DOCUMENTS[doc][0])
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


@pytest.mark.parametrize("filename", sorted(cfg_schema.render_all()))
def test_committed_schema_matches_models(filename: str) -> None:
    committed = SCHEMA_DIR / filename
    assert committed.exists(), f"{committed} is missing: {cfg_schema.REGEN_HINT}"
    expected = cfg_schema.render_all()[filename]
    assert committed.read_text(encoding="utf-8") == expected, (
        f"schema/{filename} differs from the Pydantic models: {cfg_schema.REGEN_HINT}"
    )


@pytest.mark.parametrize("doc", sorted(cfg_schema.DOCUMENTS))
def test_schema_metadata(doc: str) -> None:
    schema = _load(SCHEMA_DIR / cfg_schema.DOCUMENTS[doc][0])
    assert schema["$schema"] == cfg_schema.SCHEMA_DIALECT
    assert schema["$id"].endswith(cfg_schema.DOCUMENTS[doc][0])
    assert schema["version"] == cfg_schema.SCHEMA_VERSION
    jsonschema.Draft202012Validator.check_schema(schema)


def test_render_is_deterministic() -> None:
    assert cfg_schema.render_all() == cfg_schema.render_all()


def test_cli_writes_all_files(tmp_path: Path) -> None:
    assert cfg_schema.main(["--out", str(tmp_path)]) == 0
    assert {p.name for p in tmp_path.iterdir()} == set(cfg_schema.render_all())


def test_fixture_counts() -> None:
    assert len(VALID) >= 5 and len(INVALID) >= 5


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture(path: Path) -> None:
    fx = _load(path)
    _, model = cfg_schema.DOCUMENTS[fx["document"]]
    model.model_validate(fx["data"])
    errors = list(_validator(fx["document"]).iter_errors(fx["data"]))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture(path: Path) -> None:
    fx = _load(path)
    assert fx["expected_error"]
    _, model = cfg_schema.DOCUMENTS[fx["document"]]
    with pytest.raises(ValueError) as exc:
        model.model_validate(fx["data"])
    assert fx["expected_error"] in str(exc.value)
    schema_errors = list(_validator(fx["document"]).iter_errors(fx["data"]))
    if fx["semantic"]:
        # a rule JSON Schema cannot express: the schema accepts it, Pydantic rejects it
        assert not schema_errors, "fixture marked semantic but the schema rejects it"
    else:
        assert schema_errors, "the generated schema accepted a structurally invalid document"
