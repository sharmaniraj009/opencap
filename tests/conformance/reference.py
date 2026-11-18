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
from opencap.drivers import ab_logix, ab_pccc
from opencap.drivers.modbus_tcp import ModbusBlock, ModbusTcpDriver
from opencap.north import payload as payload_mod
from tests.conformance.vectors import decode_value, encode_value

_HOST = "192.0.2.1"


def _device(driver: str, port: int, inp: dict[str, Any]) -> tuple[Device, list[Tag]]:
    tags = [Tag.model_validate(t) for t in inp["tags"]]
    device = Device(id="SITE/PLC", channel="SITE", name="PLC", driver=driver, host=_HOST, port=port,  # type: ignore[arg-type]
                    params=inp.get("params", {}), tags=tags)
    return device, tags


def _modbus_driver(inp: dict[str, Any]) -> tuple[ModbusTcpDriver, list[Tag]]:
    device, tags = _device("modbus_tcp", 502, inp)
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


def _one_block(plan: Any, kind: type) -> Any:
    if len(plan.blocks) != 1 or plan.invalid:
        raise ValueError(f"decode vector must plan to one block, got {len(plan.blocks)} block(s), "
                         f"invalid {plan.invalid}")
    block = plan.blocks[0]
    assert isinstance(block, kind)
    return block


def ab_logix_plan(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags} -> {blocks: [{reads, items, micro800, connection_size, wire_requests, tags}],
    invalid}, or {error} when the params are refused (when the driver is built or the plan made)."""
    device, tags = _device("ab_logix", 44818, inp)
    try:
        plan = ab_logix.AbLogixDriver(device).plan(tags)
    except ValueError as exc:
        return {"error": str(exc)}
    blocks = []
    for b in plan.blocks:
        assert isinstance(b, ab_logix.LogixBlock)
        blocks.append({
            "reads": [{"name": r.name, "data_size": r.data_size, "path_size": r.path_size} for r in b.reads],
            "items": [{"tag": it.tag, "index": it.index, "bit": it.bit} for it in b.items],
            "tags": list(b.tags),
            "micro800": b.micro800,
            "connection_size": b.connection_size,
            "wire_requests": b.wire_requests(),
        })
    return {"blocks": blocks, "invalid": dict(plan.invalid)}


def ab_logix_decode(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, values: list of values | none} -> {values: {tag: value}}.

    The tags plan to exactly one block; `values` is what `plc.read(*names)` returned, aligned
    with the block's reads (a none element: pycomm3 gave no value for that name)."""
    device, tags = _device("ab_logix", 44818, inp)
    block = _one_block(ab_logix.AbLogixDriver(device).plan(tags), ab_logix.LogixBlock)
    decoded = ab_logix.decode_block(block, decode_value(inp["values"]))
    return {"values": {name: encode_value(decoded[name]) for name in sorted(decoded)}}


def ab_pccc_plan(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags} -> {blocks: [{address, file_type, file_number, slot, field, start, count,
    elem_size, tags, items}], invalid}, or {error} when the params are refused."""
    device, tags = _device("ab_pccc", 44818, inp)
    try:
        plan = ab_pccc.AbPcccDriver(device).plan(tags)
    except ValueError as exc:
        return {"error": str(exc)}
    blocks = []
    for b in plan.blocks:
        assert isinstance(b, ab_pccc.PcccBlock)
        blocks.append({
            "address": b.address, "file_type": b.file_type, "file_number": b.file_number, "slot": b.slot,
            "field": b.field, "start": b.start, "count": b.count, "elem_size": b.elem_size,
            "tags": list(b.tags),
            "items": [{"tag": it.tag, "offset": it.offset, "bit": it.bit} for it in b.items],
        })
    return {"blocks": blocks, "invalid": dict(plan.invalid)}


def ab_pccc_decode(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, value} -> {values: {tag: value}}. The tags plan to exactly one block;
    `value` is the `.value` pycomm3 returned for its address (scalar, list for `{n}`, or none)."""
    device, tags = _device("ab_pccc", 44818, inp)
    block = _one_block(ab_pccc.AbPcccDriver(device).plan(tags), ab_pccc.PcccBlock)
    decoded = ab_pccc.decode_block(block, decode_value(inp["value"]))
    return {"values": {name: encode_value(decoded[name]) for name in sorted(decoded)}}


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "modbus/plan": modbus_plan,
    "modbus/decode": modbus_decode,
    "scaling": scaling,
    "payload": payload,
    "ab_logix/plan": ab_logix_plan,
    "ab_logix/decode": ab_logix_decode,
    "ab_pccc/plan": ab_pccc_plan,
    "ab_pccc/decode": ab_pccc_decode,
}


def run(area: str, inp: dict[str, Any]) -> dict[str, Any]:
    return ADAPTERS[area](inp)
