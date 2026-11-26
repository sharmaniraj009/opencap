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
from opencap.drivers import ab_df1, ab_logix, ab_pccc, melsec_mc, s7
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


def omron_fins_addresses(inp: dict[str, Any]) -> dict[str, Any]:
    """input {address, dtype?, model?} -> {area_code, word, bit, kind, dims, str_len, str_order, bcd,
    words}, or {error: "syntax" | "range" | "unsupported"}. The reference is the FINS simulator's own
    parser (there is no Python FINS driver, ADR-0002); scripts/gen_conformance_fins.py also carries a
    hand-written expectation for every case, which tests/unit/test_fins_sim.py checks."""
    from tests.helpers import fins_sim

    dtype = inp.get("dtype")
    try:
        a = fins_sim.parse_address(inp["address"], dtype, model=inp.get("model", "CJ1"))
    except fins_sim.AddressError as exc:
        return {"error": exc.reason}
    code, word = a.codes()
    return {"area_code": f"0x{code:02X}", "word": word, "bit": a.bit, "kind": a.kind, "dims": list(a.dims),
            "str_len": a.str_len, "str_order": a.str_order, "bcd": a.bcd,
            "words": a.words(dtype if dtype not in ("bool", "string") else None)}
def _mc_driver(inp: dict[str, Any]) -> tuple[melsec_mc.MelsecMcDriver, list[Tag]]:
    device, tags = _device("melsec_mc", 5000, inp)
    return melsec_mc.MelsecMcDriver(device), tags      # ValueError on bad params


def melsec_mc_plan(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags} -> {blocks: [{device, start, count, head_number, last_number, describe, tags,
    items}], invalid}, or {error} when the params are refused (the driver cannot be built)."""
    try:
        driver, tags = _mc_driver(inp)
    except ValueError as exc:
        return {"error": str(exc)}
    plan = driver.plan(tags)
    blocks = []
    for b in plan.blocks:
        assert isinstance(b, melsec_mc.McBlock)
        blocks.append({
            "device": b.device, "start": b.start, "count": b.count,
            "head_number": b.head_number, "last_number": b.last_number, "describe": b.describe(),
            "tags": list(b.tags),
            "items": [{"names": list(it.names), "offset": it.offset, "text": it.addr.text, "dtype": it.addr.dtype,
                       "words": it.addr.words, "bit": it.addr.bit, "dims": list(it.addr.dims),
                       "str_len": it.addr.str_len, "str_order": it.addr.str_order} for it in b.items],
        })
    return {"blocks": blocks, "invalid": dict(plan.invalid)}


def melsec_mc_decode(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, reply: {words: [u16...]}} -> {values: {tag: value}}. The tags plan to
    exactly one block; `words` are the reply's words, unsigned, lowest device number first."""
    driver, tags = _mc_driver(inp)
    block = _one_block(driver.plan(tags), melsec_mc.McBlock)
    decoded = driver.decode(block, inp["reply"]["words"])
    return {"values": {name: encode_value(decoded[name]) for name in sorted(decoded)}}


def melsec_mc_frame(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, timeout_ms} -> {requests: [hex per block], cpu_model: hex}: the request frame
    pymcprotocol (through the driver's subclass) puts on the wire for each planned block, and for
    "read CPU model". Needs pymcprotocol."""
    driver, tags = _mc_driver(inp)
    p = driver.params
    timer = melsec_mc.monitoring_timer(inp["timeout_ms"])

    class _Sent(Exception):
        pass

    def captured(call: Callable[[Any], Any]) -> str:
        client = melsec_mc.mc_client_class()(p.plc_type)
        client.oo_configure(protocol=p.protocol, timeout_s=1.0, timer=timer, network=p.network, pc=p.pc,
                            dest_moduleio=p.dest_moduleio, dest_modulesta=p.dest_modulesta)
        sent: list[bytes] = []

        def send(data: bytes) -> None:
            sent.append(bytes(data))
            raise _Sent

        client._send = send              # type: ignore[method-assign]
        try:
            call(client)
        except _Sent:
            pass
        return sent[0].hex()

    plan = driver.plan(tags)
    requests = []
    for b in plan.blocks:
        assert isinstance(b, melsec_mc.McBlock)
        requests.append(captured(lambda c, b=b: c.oo_read_words(b.head, b.count)))
    return {"requests": requests, "cpu_model": captured(lambda c: c.oo_read_cpu_model())}


def melsec_mc_reply(inp: dict[str, Any]) -> dict[str, Any]:
    """input {frame: hex, words: n} -> {frame_error} (not a well-formed 3E reply, or not carrying exactly
    `words` words), {end_code, kind, message} (an error end code: kind "config" is a BlockConfigError,
    "comm" a CommError) or {words: [u16...]}."""
    frame = bytes.fromhex(inp["frame"])
    try:
        melsec_mc.check_reply_frame(frame)
    except melsec_mc.McFrameError as exc:
        return {"frame_error": str(exc)}
    code = int.from_bytes(frame[melsec_mc.REPLY_HEADER_LEN:melsec_mc.REPLY_HEADER_LEN + melsec_mc.END_CODE_LEN], "little")
    if code:
        kind = "config" if code in melsec_mc.CONFIG_END_CODES else "comm"
        return {"end_code": code, "kind": kind, "message": melsec_mc.end_code_text(code)}
    data = frame[melsec_mc.REPLY_HEADER_LEN + melsec_mc.END_CODE_LEN:]
    if len(data) != 2 * inp["words"]:
        return {"frame_error": f"reply carries {len(data)} data bytes for {inp['words']} words"}
    return {"words": [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]}


def _s7_driver(inp: dict[str, Any]) -> tuple[s7.S7Driver, list[Tag]]:
    device, tags = _device("s7", 102, inp)
    return s7.S7Driver(device), tags      # ValueError on bad params


def s7_plan(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags} -> {blocks: [{area, db, start, count, describe, tags, items}], invalid}, or
    {error} when the params are refused (the driver cannot be built)."""
    try:
        driver, tags = _s7_driver(inp)
    except ValueError as exc:
        return {"error": str(exc)}
    plan = driver.plan(tags)
    blocks = []
    for b in plan.blocks:
        assert isinstance(b, s7.S7Block)
        blocks.append({
            "area": b.area, "db": b.db, "start": b.start, "count": b.count, "describe": b.describe(),
            "tags": list(b.tags),
            "items": [{"names": list(it.names), "offset": it.offset, "text": it.addr.text, "dtype": it.addr.dtype,
                       "kind": it.addr.kind, "size": it.addr.size, "elements": it.addr.count, "bit": it.addr.bit,
                       "dims": list(it.addr.dims), "str_len": it.addr.str_len} for it in b.items],
        })
    return {"blocks": blocks, "invalid": dict(plan.invalid)}


def s7_decode(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, reply: {bytes: hex}} -> {values: {tag: value}}. The tags plan to exactly one
    block; `bytes` is that block's data."""
    driver, tags = _s7_driver(inp)
    block = _one_block(driver.plan(tags), s7.S7Block)
    decoded = driver.decode(block, bytes.fromhex(inp["reply"]["bytes"]))
    return {"values": {name: encode_value(decoded[name]) for name in sorted(decoded)}}


def s7_frame(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, pdu} -> the frames python-snap7 builds: {connection_request, setup, szl: [3],
    requests: [[read var PDU hex per request] per block]}. `pdu` (default max_pdu) is the negotiated PDU the
    blocks are split for. Read var references count 1.. per block, as a fresh connection numbers them."""
    from snap7.connection import ISOTCPConnection
    from snap7.datatypes import S7Area, S7WordLen
    from snap7.s7protocol import S7Protocol

    driver, tags = _s7_driver(inp)
    p = driver.params
    pdu = inp["pdu"] or p.max_pdu
    tsap = (p.connection_type << 8) | (p.rack << 5) | p.slot
    conn = ISOTCPConnection(host="192.0.2.1", port=102, local_tsap=0x0100, remote_tsap=tsap)
    out: dict[str, Any] = {"connection_request": conn._build_tpkt(conn._build_cotp_cr()).hex(),
                           "setup": S7Protocol().build_setup_communication_request(pdu_length=p.max_pdu).hex(),
                           "szl": [S7Protocol().build_read_szl_request(i, 0).hex() for i in (0x001C, 0x0011, 0x0424)]}
    requests = []
    for b in driver.plan(tags).blocks:
        assert isinstance(b, s7.S7Block)
        proto = S7Protocol()
        unit_bytes = 2 if b.area in s7.TC_AREAS else 1
        chunk = max(1, (pdu - s7.READ_OVERHEAD) // unit_bytes)
        parts = []
        for start, count in s7._split(b.start, b.count, chunk):
            parts.append(proto.build_read_request(S7Area(b.area_code), b.db, start, S7WordLen(b.word_len), count).hex())
        requests.append(parts)
    out["requests"] = requests
    return out


def s7_reply(inp: dict[str, Any]) -> dict[str, Any]:
    """input {pdu: hex, word_len, count} (a read var reply to one item) -> {return_code, data: hex} (an item
    return code other than 0xFF has no data), {header_error: code, message}, or {bad_reply: reason}."""
    from snap7.datatypes import S7WordLen
    from snap7.error import S7ProtocolError
    from snap7.s7protocol import S7Protocol

    proto = S7Protocol()
    try:
        resp = proto.parse_response(bytes.fromhex(inp["pdu"]))
        item = resp.get("data")
        if not item:
            return {"bad_reply": "No data in response"}
        rc = item.get("return_code")
        if rc != 0xFF:
            return {"return_code": rc, "data": ""}
        data = bytes(proto.extract_read_data(resp, S7WordLen(inp["word_len"]), inp["count"]))
        return {"return_code": 0xFF, "data": data.hex()}
    except S7ProtocolError as exc:
        code = getattr(exc, "error_code", None)
        if code:
            return {"header_error": code, "message": str(exc)}
        return {"bad_reply": str(exc)}

def _hex(data: bytes) -> str:
    return data.hex(" ")


def ab_df1_frame(inp: dict[str, Any]) -> dict[str, Any]:
    """input {check, app} -> {frame}: DLE STX, the stuffed application bytes, DLE ETX, then the BCC or
    the CRC-16 (low byte first, never stuffed). Bytes are lower-case hex pairs separated by spaces."""
    try:
        return {"frame": _hex(ab_df1.encode_frame(bytes.fromhex(inp["app"]), inp["check"]))}
    except ValueError as exc:
        return {"error": str(exc)}


def ab_df1_receive(inp: dict[str, Any]) -> dict[str, Any]:
    """input {check, chunks: [hex, ...]} -> {events: [{kind, data, reason}], noise}: one receiver fed
    the chunks in order (frames may span chunks); `noise` counts the bytes skipped outside frames."""
    try:
        decoder = ab_df1.FrameDecoder(inp["check"])
    except ValueError as exc:
        return {"error": str(exc)}
    events = [ev for chunk in inp["chunks"] for ev in decoder.feed(bytes.fromhex(chunk))]
    return {"events": [{"kind": ev.kind, "data": _hex(ev.data), "reason": ev.reason} for ev in events],
            "noise": decoder.noise}


def _df1_device(inp: dict[str, Any]) -> tuple[Device, list[Tag]]:
    device, tags = _device("ab_df1", 4001, inp)
    return device.model_copy(update={"params": {"transport": "tcp", **device.params}}), tags


def ab_df1_request(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags} -> {requests: [{address, tags, fnc}], invalid}: the PCCC function code and
    function data of the typed read for each planned block (ab_pccc's planner), or {error}."""
    device, tags = _df1_device(inp)
    try:
        params = ab_df1.Df1Params.from_mapping(device.params)
        plan = ab_df1.AbDf1Driver(device).plan(tags)
        requests = [{"address": b.address, "tags": list(b.tags),
                     "fnc": _hex(ab_df1.typed_read_function(b, params.read_fnc))} for b in plan.blocks]
    except ValueError as exc:
        return {"error": str(exc)}
    return {"requests": requests, "invalid": dict(plan.invalid)}


def ab_df1_values(inp: dict[str, Any]) -> dict[str, Any]:
    """input {params, tags, reply} -> {raw, values}: the tags plan to one block; `reply` is the reply's
    data bytes (after STS/TNS). `raw` is what pycomm3 would return for the block's address, `values`
    each tag decoded from it (ab_pccc's decode), or a decode_error."""
    device, tags = _df1_device(inp)
    block = _one_block(ab_df1.AbDf1Driver(device).plan(tags), ab_pccc.PcccBlock)
    raw = ab_df1.pccc_values(block, bytes.fromhex(inp["reply"]))
    decoded = ab_pccc.decode_block(block, raw)
    return {"raw": encode_value(raw), "values": {name: encode_value(decoded[name]) for name in sorted(decoded)}}


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


    "omron_fins/addresses": omron_fins_addresses,
    "melsec_mc/plan": melsec_mc_plan,
    "melsec_mc/decode": melsec_mc_decode,
    "melsec_mc/frame": melsec_mc_frame,
    "melsec_mc/reply": melsec_mc_reply,
    "s7/plan": s7_plan,
    "s7/decode": s7_decode,
    "s7/frame": s7_frame,
    "s7/reply": s7_reply,
    "ab_df1/frame": ab_df1_frame,
    "ab_df1/receive": ab_df1_receive,
    "ab_df1/request": ab_df1_request,
    "ab_df1/values": ab_df1_values,
}


def run(area: str, inp: dict[str, Any]) -> dict[str, Any]:
    return ADAPTERS[area](inp)
