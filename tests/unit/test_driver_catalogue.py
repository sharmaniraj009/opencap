"""The driver catalogue (API-16, #129): every registered driver declares its params schema, the
schemas are valid JSON Schema, the small validator the API uses agrees with a full one, every
shared config fixture still passes, and every address example is one the driver accepts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest

from opencap.config.models import Device, Tag
from opencap.drivers import DRIVERS, catalogue, get_driver_class
from opencap.drivers._schema import validate_params
from opencap.drivers.base import ALL_DTYPES

FIXTURES = Path(__file__).parents[1] / "conformance" / "config" / "valid"
NAMES = sorted(DRIVERS)


def test_every_registered_driver_is_listed():
    assert [d["name"] for d in catalogue.listing()["drivers"]] == list(DRIVERS)


@pytest.mark.parametrize("name", NAMES)
def test_entry_is_complete_and_the_schema_is_valid_json_schema(name):
    entry = catalogue.entry(name)
    assert entry["transport"] and entry["address_examples"] and set(entry["dtypes"]) <= set(ALL_DTYPES)
    schema = entry["params_schema"]
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object" and schema["properties"]
    for key, prop in schema["properties"].items():                      # every key is described
        assert prop.get("description"), f"{name}.{key} has no description"


@pytest.mark.parametrize("name", NAMES)
def test_documented_defaults_validate_and_match_the_driver(name):
    """Defaults in the schema are valid values, and an empty params object is valid."""
    schema = get_driver_class(name).params_schema
    defaults = {k: p["default"] for k, p in schema["properties"].items() if "default" in p}
    assert validate_params(schema, {}) == [] or schema.get("required")
    assert validate_params(schema, defaults) == []
    jsonschema.Draft202012Validator(schema).validate(defaults)


@pytest.mark.parametrize("name", NAMES)
def test_validator_agrees_with_a_full_json_schema_validator(name):
    schema = get_driver_class(name).params_schema
    full = jsonschema.Draft202012Validator(schema)
    samples: list[dict] = []
    for key, prop in schema["properties"].items():
        samples += [{key: v} for v in ("text", 7, -1, 10**9, 1.5, True, [], {}, None, "zero")]
        samples += [{key: v} for v in prop.get("enum", [])]
        if "maximum" in prop:
            samples += [{key: prop["maximum"]}, {key: prop["maximum"] + 1}]
        if "minimum" in prop:
            samples += [{key: prop["minimum"]}, {key: prop["minimum"] - 1}]
    for sample in samples:
        ours = bool(validate_params(schema, sample))
        # null means "the default" for us, as in the drivers; JSON Schema only allows it where declared
        value = next(iter(sample.values()))
        theirs = not full.is_valid(sample) and value is not None
        assert ours == theirs, (name, sample)


def test_errors_name_the_key_and_never_echo_the_value():
    schema = get_driver_class("modbus_tcp").params_schema
    errors = validate_params(schema, {"unit_id": 999, "max_holding": 0, "zero_based": "secret-looking"})
    assert {tuple(e["loc"]) for e in errors} == {("params", "unit_id"), ("params", "max_holding"),
                                                 ("params", "zero_based")}
    assert "secret-looking" not in json.dumps(errors)
    assert validate_params(schema, [1]) == [{"loc": ["params"], "msg": "params must be a JSON object",
                                             "type": "dict_type"}]
    assert validate_params(schema, {"zero_based": True}, ("devices", 3, "params")) == []


def test_booleans_are_not_integers_and_integers_are_not_booleans():
    schema = get_driver_class("modbus_tcp").params_schema
    assert validate_params(schema, {"unit_id": True}) and validate_params(schema, {"zero_based": 1})


def test_array_items_and_required_keys_are_checked():
    sim = get_driver_class("simulator").params_schema
    assert validate_params(sim, {"bad_decode_tags": ["A", 2]})[0]["loc"] == ["params", "bad_decode_tags", 1]
    assert validate_params({**sim, "required": ["epoch"]}, {})[0]["type"] == "missing"


def test_unknown_params_are_allowed_and_secrets_stay_out_of_the_schemas():
    assert validate_params(get_driver_class("s7").params_schema, {"vendor": "x", "rack": 1}) == []
    opcua = get_driver_class("opcua_client").params_schema["properties"]
    assert {"username", "password", "user", "private_key_password"}.isdisjoint(opcua)
    assert opcua["password_env"]["description"].startswith("NAME")


def _devices(doc: dict):
    for channel in doc["channels"]:
        yield from channel["devices"]


def test_every_conformance_fixture_still_validates():
    count = 0
    for path in sorted(FIXTURES.glob("devices*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture["document"] != "devices":
            continue
        for dev in _devices(fixture["data"]):
            count += 1
            assert catalogue.check_params(dev["driver"], dev.get("params", {})) == [], (path.name, dev["id"])
    assert count >= 4


def test_the_importer_output_validates():
    from opencap.config.kep_import import import_kep

    sample = Path(__file__).parents[1] / "fixtures" / "project_sample.json"
    result = import_kep(sample)
    count = 0
    for channel in result.devices_file.channels:
        for dev in channel.devices:
            count += 1
            assert catalogue.check_params(dev.driver, dev.params) == [], dev.id
    assert count


_ADDRESS_SKIP_DTYPES = {"string"}


@pytest.mark.parametrize("name", NAMES)
def test_every_address_example_is_accepted_by_the_driver_for_some_dtype(name):
    cls = get_driver_class(name)
    params = {"endpoint": "opc.tcp://192.0.2.10:4840"} if name == "opcua_client" else {}
    dev = Device(id="C/D", channel="C", name="D", driver=name, host="192.0.2.10", port=1, params=params, tags=[])
    for address in cls.address_examples:
        ok = False
        for dtype in cls.dtypes:
            plan = cls(dev).plan([Tag(name="T", address=address, dtype=dtype)])
            if plan.blocks and not plan.invalid:
                ok = True
                break
        assert ok, f"{name}: example {address!r} is not accepted for any dtype"


def test_catalogue_text_has_no_site_data():
    text = json.dumps([catalogue.entry(n) for n in NAMES])
    for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
        assert ip.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), ip


def test_identity_and_browse_flags_follow_the_class():
    from opencap.drivers.base import Driver

    assert get_driver_class("modbus_tcp").supports_identity() is True
    assert Driver.supports_identity() is False
    browsing = {d["name"] for d in catalogue.listing()["drivers"] if d["supports_browse"]}
    assert browsing == {"ab_logix"}                  # the only driver with a browse() (#11)
