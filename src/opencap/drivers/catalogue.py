"""The driver catalogue (API-16): what a client needs to know to configure a driver.

Generated from the driver classes (`Driver.transport`, `.dtypes`, `.address_examples`,
`.params_schema`, `.supports_identity()`, `.supports_browse`), never kept as a second copy.
`GET /api/v1/drivers` lists it, `GET /api/v1/drivers/{name}` returns one entry with the
JSON Schema of `Device.params`, and the config store checks every device write against that
schema (422 with `loc` under `params`).
"""

from __future__ import annotations

from typing import Any

from opencap.drivers import DRIVERS, get_driver_class
from opencap.drivers._schema import validate_params


def summary(name: str) -> dict[str, Any]:
    """One row of the list: name, transport, and what the driver can do beyond reading."""
    cls = get_driver_class(name)
    return {"name": name, "transport": cls.transport, "supports_identity": cls.supports_identity(),
            "supports_browse": cls.supports_browse}


def entry(name: str) -> dict[str, Any]:
    """The detail of one driver. Raises LookupError for an unknown name."""
    cls = get_driver_class(name)
    return {**summary(name), "params_schema": cls.params_schema, "dtypes": list(cls.dtypes),
            "address_examples": list(cls.address_examples)}


def listing() -> dict[str, Any]:
    drivers = []
    for name in DRIVERS:
        try:
            drivers.append(summary(name))
        except LookupError:        # registered but not implemented in this build
            continue
    return {"drivers": drivers}


def check_params(driver: str, params: Any, loc: tuple = ("params",)) -> list[dict[str, Any]]:
    """Schema errors of a device's `params` for its driver (empty when valid or the driver has no schema)."""
    try:
        schema = get_driver_class(driver).params_schema
    except LookupError:
        return []
    return validate_params(schema, params, loc) if schema else []
