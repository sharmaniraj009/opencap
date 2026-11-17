"""Driver registry. Classes are imported lazily so a missing protocol library
only breaks the driver that needs it."""

from __future__ import annotations

import importlib

from opencap.drivers.base import Driver

DRIVERS: dict[str, str] = {
    "modbus_tcp": "opencap.drivers.modbus_tcp:ModbusTcpDriver",
    "ab_logix": "opencap.drivers.ab_logix:AbLogixDriver",
    "ab_pccc": "opencap.drivers.ab_pccc:AbPcccDriver",
    "s7": "opencap.drivers.s7:S7Driver",
    "melsec_mc": "opencap.drivers.melsec_mc:MelsecMcDriver",
    "opcua_client": "opencap.drivers.opcua_client:OpcUaClientDriver",
}


def get_driver_class(name: str) -> type[Driver]:
    """Return the driver class, or raise LookupError if it is not implemented yet."""
    try:
        target = DRIVERS[name]
    except KeyError:
        raise LookupError(f"unknown driver {name!r}") from None
    module_name, _, cls_name = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise LookupError(f"driver {name!r} is not implemented yet") from None
        raise
    return getattr(module, cls_name)
