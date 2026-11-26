"""JSON Schema declarations for `Device.params` (the driver catalogue, API-16).

Each driver declares its schema next to its params code, with these helpers, so the catalogue
is generated from the drivers and cannot drift from the docs by hand-copying. The validator
implements only the subset the schemas use (`type`, `enum`, `minimum`, `maximum`, `minLength`,
`items`): the config API checks a device write with it, with no extra dependency, and the Rust
engine can port it as is. Tests check every schema against a full JSON Schema validator.

Rules shared by all drivers (stated once in the schema's `description`):
- unknown keys are allowed (drivers ignore them; the importer keeps provenance keys);
- `null` means "the default" for every key, as in the drivers.
"""

from __future__ import annotations

from typing import Any

SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
PARAMS_NOTE = ("Unknown keys are ignored by the driver; a null value means the default. "
               "See docs/driver_params.md.")


def _prop(type_: str | list[str], description: str, default: Any = None, **extra: Any) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": type_, "description": description}
    if default is not None:
        prop["default"] = default
    prop.update(extra)
    return prop


def boolean(description: str, default: bool | None = None) -> dict[str, Any]:
    return _prop("boolean", description, default)


def integer(description: str, default: int | None = None, *, minimum: int | None = None,
            maximum: int | None = None, nullable: bool = False,
            enum: list[int] | tuple[int, ...] | None = None) -> dict[str, Any]:
    prop = _prop(["integer", "null"] if nullable else "integer", description, default)
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    if enum is not None:
        prop["enum"] = list(enum)
    return prop


def number(description: str, default: float | None = None, *, minimum: float | None = None,
           nullable: bool = False, enum: list[float] | tuple[float, ...] | None = None) -> dict[str, Any]:
    prop = _prop(["number", "null"] if nullable else "number", description, default)
    if minimum is not None:
        prop["minimum"] = minimum
    if enum is not None:
        prop["enum"] = list(enum)
    return prop


def string(description: str, default: str | None = None, *, nullable: bool = False,
           enum: list[Any] | tuple[Any, ...] | None = None, non_empty: bool = False) -> dict[str, Any]:
    prop = _prop(["string", "null"] if nullable else "string", description, default)
    if enum is not None:
        prop["enum"] = list(enum)
    if non_empty:
        prop["minLength"] = 1
    return prop


def array_of_strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def obj(description: str) -> dict[str, Any]:
    return {"type": "object", "description": description}


def params_schema(title: str, properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"$schema": SCHEMA_URI, "title": title, "type": "object",
                              "description": PARAMS_NOTE, "properties": properties,
                              "additionalProperties": True}
    if required:
        schema["required"] = required
    return schema


# --- the validator ---------------------------------------------------------------------

def _type_ok(value: Any, type_: str) -> bool:
    if type_ == "null":
        return value is None
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "array":
        return isinstance(value, list)
    if type_ == "object":
        return isinstance(value, dict)
    return True


def _error(loc: tuple, msg: str, type_: str) -> dict[str, Any]:
    return {"loc": list(loc), "msg": msg, "type": type_}


def _check(schema: dict[str, Any], value: Any, loc: tuple) -> list[dict[str, Any]]:
    declared = schema.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or [])
    if types and not any(_type_ok(value, t) for t in types):
        words = " or ".join("a whole number" if t == "integer" else "true or false" if t == "boolean"
                            else f"a {t}" for t in types if t != "null")
        return [_error(loc, f"expected {words}", "type_error")]
    if "enum" in schema and value not in schema["enum"]:
        return [_error(loc, "expected one of " + ", ".join(repr(v) for v in schema["enum"]), "enum")]
    out: list[dict[str, Any]] = []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(_error(loc, f"must be >= {schema['minimum']}", "greater_than_equal"))
        if "maximum" in schema and value > schema["maximum"]:
            out.append(_error(loc, f"must be <= {schema['maximum']}", "less_than_equal"))
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        out.append(_error(loc, "must not be empty", "string_too_short"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out.extend(_check(schema["items"], item, (*loc, i)))
    return out


def validate_params(schema: dict[str, Any], params: Any, loc: tuple = ("params",)) -> list[dict[str, Any]]:
    """Errors (pydantic style `{loc, msg, type}`) of `params` against a schema from `params_schema`.
    The input values are never echoed."""
    if not isinstance(params, dict):
        return [_error(loc, "params must be a JSON object", "dict_type")]
    errors: list[dict[str, Any]] = []
    for key in schema.get("required", []):
        if params.get(key) is None:
            errors.append(_error((*loc, key), "this param is required", "missing"))
    for key, prop in schema.get("properties", {}).items():
        value = params.get(key)
        if value is not None:                       # null = the default
            errors.extend(_check(prop, value, (*loc, key)))
    return errors
