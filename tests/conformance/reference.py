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
from opencap.core import quality as quality_mod
from opencap.core.cache import TagValue
from opencap.core.channel_worker import scale_value
from opencap.drivers import ab_logix, ab_pccc
from opencap.drivers.modbus_tcp import ModbusBlock, ModbusTcpDriver
from opencap.north import payload as payload_mod
from opencap.north import payload_iotgw as payload_iotgw_mod
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


def payload_iotgw(inp: dict[str, Any]) -> dict[str, Any]:
    """input {device, values, cfg, now} -> {bytes}: the UTF-8 text of
    `encode(build_iotgw_payload(...))` (publish.format iotgw_json)."""
    device = Device.model_validate(inp["device"])
    values = {name: TagValue(decode_value(tv["value"]), tv["quality"], _ts(tv["source_ts"]))
              for name, tv in inp["values"].items()}
    cfg = PayloadCfg.model_validate(inp["cfg"])
    built = payload_iotgw_mod.build_iotgw_payload(device, values, cfg, now=_ts(inp["now"]))
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


# --- system_tags ---------------------------------------------------------------------------------

def _tagged(dtype_value: tuple[str, Any]) -> dict[str, Any]:
    dtype, value = dtype_value
    return {"dtype": dtype, "value": encode_value(value)}


def system_tags(inp: dict[str, Any]) -> dict[str, Any]:
    """Two kinds of input (docs/spec/system-tags.md §13).

    `snapshot`: `{kind, state, paths, absent}` -> `{tags: {path: {dtype, value}}, absent: [paths]}`:
    the listed tags of `opencap.core.system_tags.evaluate(state)`, and those of `absent` that really
    are absent (a path also counts as present when a tag lives below it).

    `device_sequence`: `{kind, device, start, steps: [{at, event, error?, cycle_ms?, observe?}]}` ->
    `{steps: {"<index>": {name: {dtype, value}}}}`: feed the events into `DeviceHealth` plus the two
    status fields of §9, and read the device's status tags after each step that has `observe`.
    """
    from opencap.core import system_tags as st
    from opencap.core.health import DeviceHealth, iso_utc

    if inp["kind"] == "snapshot":
        got = st.evaluate(inp["state"])
        absent = [p for p in inp.get("absent", []) if not any(g == p or g.startswith(p + ".") for g in got)]
        return {"tags": {p: _tagged(got[p]) for p in inp["paths"]}, "absent": absent}

    dev = inp["device"]
    start = st.parse_ts(inp["start"])
    assert start is not None
    health = DeviceHealth(int(dev.get("demote_after", 3)), int(dev.get("demote_for_ms", 30_000)))
    flags: dict[str, Any] = {"connected": False, "last_connect_failed": False, "last_error": None, "cycle_ms": 0.0}
    steps: dict[str, Any] = {}
    for index, step in enumerate(inp["steps"]):
        at = st.parse_ts(step["at"])
        assert at is not None
        mono = (at - start).total_seconds()
        event = step["event"]
        if event == "connect_ok":
            flags.update(connected=True, last_connect_failed=False)
        elif event == "connect_failed":
            flags.update(connected=False, last_connect_failed=True)
        elif event == "cycle_good":
            health.record_good(mono, at)
            flags.update(last_error=step.get("error"), cycle_ms=step.get("cycle_ms", 0.0))
        elif event == "cycle_failed":
            ev = health.record_failure(mono, at)
            flags.update(last_error=step["error"], cycle_ms=step.get("cycle_ms", 0.0))
            if ev in ("demoted", "redemoted"):
                flags["connected"] = False          # the worker drops the connection on demotion
        elif event == "cycle_neutral":
            flags["cycle_ms"] = step.get("cycle_ms", 0.0)
            if step.get("error") is not None:
                flags["last_error"] = step["error"]
        elif event != "tick":
            raise ValueError(f"step {index}: unknown event {event!r}")
        if step.get("observe"):
            status = {**flags, "state": health.state, "consecutive_failures": health.consecutive_failures,
                      "error_since": iso_utc(health.error_since), "last_good": iso_utc(health.last_good),
                      "errors": health.errors}
            tags = st.device_status_tags(dev, status, at, disabled=False)
            steps[str(index)] = {n: _tagged((st.DTYPES[("device", n)], tags[n])) for n in step["observe"]}
    return {"steps": steps}


def quality(inp: dict[str, Any]) -> dict[str, Any]:
    """input {op: "map", quality, ever_good} -> the quality in every north model (ADR-0006), or
    {op: "worst", qualities} -> {worst}."""
    if inp["op"] == "worst":
        return {"worst": quality_mod.worst(inp["qualities"])}
    q, ever_good = inp["quality"], inp["ever_good"]
    return {
        "opcua": quality_mod.to_opcua(q, ever_good),
        "opcua_name": quality_mod.opcua_name(q, ever_good),
        "opcda": quality_mod.to_opcda(q, ever_good),
        "sparkplug": quality_mod.to_sparkplug(q, ever_good),
        "subdevice": quality_mod.to_subdevice(q),
    }


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "modbus/plan": modbus_plan,
    "modbus/decode": modbus_decode,
    "scaling": scaling,
    "payload": payload,
    "payload/iotgw": payload_iotgw,
    "ab_logix/plan": ab_logix_plan,
    "ab_logix/decode": ab_logix_decode,
    "ab_pccc/plan": ab_pccc_plan,
    "ab_pccc/decode": ab_pccc_decode,
    "system_tags": system_tags,
    "quality": quality,
}


def run(area: str, inp: dict[str, Any]) -> dict[str, Any]:
    return ADAPTERS[area](inp)
