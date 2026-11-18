"""The Python reference engine's adapter for each conformance area (ADR-0014, README.md here).

`run(area, input)` computes what the Python reference makes of one vector's `input`, in the
shape of the vector's `expected`. The generator writes these results into the files; the
unit test re-runs them against the files. Another engine writes the same adapters around its
own code and compares with `expected`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from opencap.config.models import Device, PayloadCfg, Scale, Tag
from opencap.core.cache import TagValue
from opencap.core.channel_worker import scale_value
from opencap.drivers.modbus_tcp import ModbusBlock, ModbusTcpDriver
from opencap.north import payload as payload_mod
from tests.conformance.vectors import decode_value, encode_value

_HOST = "192.0.2.1"


def _modbus_driver(inp: dict[str, Any]) -> tuple[ModbusTcpDriver, list[Tag]]:
    tags = [Tag.model_validate(t) for t in inp["tags"]]
    device = Device(id="SITE/PLC", channel="SITE", name="PLC", driver="modbus_tcp", host=_HOST, port=502,
                    params=inp.get("params", {}), tags=tags)
    return ModbusTcpDriver(device), tags        # ValueError on bad params


def modbus_plan(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags} -> {blocks: [{fc, start, count, tags}], invalid: {tag: reason}}, or
    {error} when the params are refused (the driver cannot be built)."""
    try:
        driver, tags = _modbus_driver(inp)
    except ValueError as exc:
        return {"error": str(exc)}
    plan = driver.plan(tags)
    blocks = []
    for b in plan.blocks:
        assert isinstance(b, ModbusBlock)
        blocks.append({"fc": b.fc, "start": b.start, "count": b.count, "tags": list(b.tags)})
    return {"blocks": blocks, "invalid": dict(plan.invalid)}


def modbus_decode(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, reply: {registers: [u16...]} | {bits: [bool...]}} -> {values: {tag: value}}.

    The tags must plan to exactly one block with no invalid tag; `reply` is that block's reply
    (FC03/04 registers as unsigned 16-bit numbers in wire order, FC01/02 bits, possibly padded).
    """
    driver, tags = _modbus_driver(inp)
    plan = driver.plan(tags)
    if len(plan.blocks) != 1 or plan.invalid:
        raise ValueError(f"decode vector must plan to one block, got {len(plan.blocks)} block(s), "
                         f"invalid {plan.invalid}")
    block = plan.blocks[0]
    assert isinstance(block, ModbusBlock)
    reply = inp["reply"]
    data = reply["bits"] if block.is_bits else reply["registers"]
    decoded = driver.decode(block, data)
    return {"values": {name: encode_value(decoded[name]) for name in sorted(decoded)}}


def scaling(inp: dict[str, Any]) -> dict[str, Any]:
    """input {scale, raw} -> {value} or {error} (the tag then gets quality bad_decode)."""
    scale = Scale.model_validate(inp["scale"])
    try:
        return {"value": encode_value(scale_value(scale, decode_value(inp["raw"])))}
    except (ValueError, TypeError, ArithmeticError) as exc:   # what the ChannelWorker catches
        return {"error": str(exc)}


def _ts(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def payload(inp: dict[str, Any]) -> dict[str, Any]:
    """input {firm_version, device, values, cfg, is_secure, now} -> {bytes}: the UTF-8 text of
    `encode(build_payload(...))`, FirmVersion included as `firm_version`."""
    if inp["firm_version"] != payload_mod.FIRM_VERSION:
        raise ValueError(f"vector made with {inp['firm_version']}, this is {payload_mod.FIRM_VERSION}: regenerate")
    device = Device.model_validate(inp["device"])
    values = {name: TagValue(decode_value(tv["value"]), tv["quality"], _ts(tv["source_ts"]))
              for name, tv in inp["values"].items()}
    cfg = PayloadCfg.model_validate(inp["cfg"])
    built = payload_mod.build_payload(device, values, cfg, is_secure=inp["is_secure"], now=_ts(inp["now"]))
    return {"bytes": payload_mod.encode(built).decode("utf-8")}


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "modbus/plan": modbus_plan,
    "modbus/decode": modbus_decode,
    "scaling": scaling,
    "payload": payload,
}


def run(area: str, inp: dict[str, Any]) -> dict[str, Any]:
    return ADAPTERS[area](inp)
