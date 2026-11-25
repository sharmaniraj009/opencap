"""asyncua scenario client for the L1 SCADA suite (the same argv/JSON protocol as the Milo and
open62541 clients; see tests/scada/README.md). Prints JSON lines; the last is {"result": ...}.

    python tests/scada/asyncua_client.py --scenario browse --endpoint opc.tcp://127.0.0.1:4840/opencap \
        --ns 2 --cert c.der --key k.pem --user u --password p [--policy Basic256Sha256 --mode SignAndEncrypt]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from typing import Any

from asyncua import Client, ua
from asyncua.crypto import security_policies as sp

POLICIES = {
    "None": None,
    "Basic256Sha256": sp.SecurityPolicyBasic256Sha256,
    "Aes128_Sha256_RsaOaep": sp.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256_Sha256_RsaPss": sp.SecurityPolicyAes256Sha256RsaPss,
}
MODES = {"None": ua.MessageSecurityMode.None_, "Sign": ua.MessageSecurityMode.Sign,
         "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt}


def emit(**obj: Any) -> None:
    print(json.dumps(obj, default=str), flush=True)


def hexcode(code: int | ua.StatusCode) -> str:
    value = code.value if isinstance(code, ua.StatusCode) else int(code)
    return f"0x{value & 0xFFFFFFFF:08X}"


def jsonable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


async def make_client(a: argparse.Namespace, policy: str | None = None, mode: str | None = None,
                      user: str | None = None, password: str | None = None) -> Client:
    client = Client(a.endpoint, timeout=a.request_timeout)
    client.application_uri = a.app_uri
    client.name = "opencap scada-l1 asyncua client"
    policy = policy or a.policy
    mode = mode or a.mode
    if policy != "None":
        await client.set_security(POLICIES[policy], a.cert, a.key, mode=MODES[mode])
    if user is not None:
        client.set_user(user)
        client.set_password(password or "")
    return client


def policy_name(uri: str) -> str:
    return uri.rsplit("#", 1)[-1]


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


# --- scenarios --------------------------------------------------------------------------------

async def s_endpoints(a: argparse.Namespace) -> dict[str, Any]:
    client = Client(a.endpoint, timeout=a.request_timeout)
    eps = await client.connect_and_get_server_endpoints()
    return {"endpoints": [{
        "url": e.EndpointUrl, "policy": e.SecurityPolicyUri, "mode": int(e.SecurityMode),
        "tokens": sorted({int(t.TokenType) for t in e.UserIdentityTokens}),
        "server_cert": bool(e.ServerCertificate),
    } for e in eps]}


async def s_connect(a: argparse.Namespace) -> dict[str, Any]:
    eps = (await s_endpoints(a))["endpoints"]
    attempts = []
    for ep in eps:
        policy = policy_name(ep["policy"])
        mode = {1: "None", 2: "Sign", 3: "SignAndEncrypt"}[ep["mode"]]
        for token, user, password in (("anonymous", None, None), ("username", a.user, a.password),
                                      ("bad_password", a.user, a.password + "-wrong")):
            entry: dict[str, Any] = {"policy": ep["policy"], "mode": ep["mode"], "token": token,
                                     "ok": False, "state": None, "error": None}
            client = await make_client(a, policy, mode, user, password)
            try:
                await client.connect()
            except Exception as exc:          # refused: a status code or a transport error
                entry["error"] = hexcode(exc.code) if isinstance(exc, ua.UaStatusCodeError) else repr(exc)
            else:
                try:
                    state = await client.get_node(ua.ObjectIds.Server_ServerStatus_State).read_value()
                    entry.update(ok=True, state=int(state))
                finally:
                    with contextlib.suppress(Exception):
                        await client.disconnect()
            attempts.append(entry)
    return {"attempts": attempts}


async def s_browse(a: argparse.Namespace) -> dict[str, Any]:
    client = await make_client(a, user=a.user, password=a.password)
    nodes: list[dict[str, Any]] = []
    calls = 0
    async with client:
        queue = [ua.NodeId(ua.ObjectIds.ObjectsFolder)]
        seen: set[str] = set()
        while queue:
            parent = queue.pop(0)
            desc = ua.BrowseDescription()
            desc.NodeId = parent
            desc.BrowseDirection = ua.BrowseDirection.Forward
            desc.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
            desc.IncludeSubtypes = True
            desc.NodeClassMask = int(ua.NodeClass.Object | ua.NodeClass.Variable)
            desc.ResultMask = int(ua.BrowseResultMask.All)
            params = ua.BrowseParameters()
            params.NodesToBrowse = [desc]
            params.RequestedMaxReferencesPerNode = a.max_refs
            result = (await client.uaclient.browse(params))[0]
            calls += 1
            refs = list(result.References)
            while result.ContinuationPoint:
                result = (await client.uaclient.browse_next(ua.BrowseNextParameters(
                    ReleaseContinuationPoints=False, ContinuationPoints=[result.ContinuationPoint])))[0]
                calls += 1
                refs += result.References
            for ref in refs:
                nid = ref.NodeId.NodeId if isinstance(ref.NodeId, ua.ExpandedNodeId) else ref.NodeId
                if nid.NamespaceIndex != a.ns:
                    continue                   # the standard Server object etc.: not ours
                text = nid.to_string()
                nodes.append({"id": text, "name": ref.BrowseName.to_string(), "class": int(ref.NodeClass),
                              "parent": parent.to_string()})
                if text not in seen:
                    seen.add(text)
                    queue.append(nid)
    return {"nodes": nodes, "browse_calls": calls}


async def s_translate(a: argparse.Namespace) -> dict[str, Any]:
    paths = read_lines(a.paths)
    client = await make_client(a, user=a.user, password=a.password)
    async with client:
        bps = []
        for p in paths:
            bp = ua.BrowsePath()
            bp.StartingNode = ua.NodeId(ua.ObjectIds.ObjectsFolder)
            bp.RelativePath = ua.RelativePath(Elements=[ua.RelativePathElement(
                ReferenceTypeId=ua.NodeId(ua.ObjectIds.HierarchicalReferences), IsInverse=False,
                IncludeSubtypes=True, TargetName=ua.QualifiedName(seg, a.ns)) for seg in p.split("/")])
            bps.append(bp)
        results = await client.uaclient.translate_browsepaths_to_nodeids(bps)
    return {"results": [{"path": p, "status": hexcode(r.StatusCode),
                         "targets": [t.TargetId.NodeId.to_string() if isinstance(t.TargetId, ua.ExpandedNodeId)
                                     else t.TargetId.to_string() for t in r.Targets]}
                        for p, r in zip(paths, results, strict=True)]}


async def _property(client: Client, node: Any, name: str) -> Any:
    for child in await node.get_properties():
        if (await child.read_browse_name()).Name == name:
            return await child.read_value()
    return None


async def s_read(a: argparse.Namespace) -> dict[str, Any]:
    ids = read_lines(a.nodes)
    client = await make_client(a, user=a.user, password=a.password)
    out = []
    async with client:
        attrs = [ua.AttributeIds.Value, ua.AttributeIds.DataType, ua.AttributeIds.ValueRank,
                 ua.AttributeIds.ArrayDimensions, ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel,
                 ua.AttributeIds.Description]
        for text in ids:
            node = client.get_node(text)
            params = ua.ReadParameters()
            params.NodesToRead = [ua.ReadValueId(NodeId=node.nodeid, AttributeId=attr) for attr in attrs]
            params.TimestampsToReturn = ua.TimestampsToReturn.Both
            dvs = await client.uaclient.read(params)
            value, dtype, rank, dims, access, user_access, descr = dvs
            eu_range = await _property(client, node, "EURange")
            units = await _property(client, node, "EngineeringUnits")
            out.append({
                "id": text, "status": hexcode(value.StatusCode),
                "value": jsonable(value.Value.Value if value.Value is not None else None),
                "source_ts": value.SourceTimestamp.isoformat() if value.SourceTimestamp else None,
                "datatype": dtype.Value.Value.to_string() if dtype.Value and dtype.Value.Value else None,
                "valueRank": rank.Value.Value if rank.StatusCode.is_good() else None,
                "arrayDims": list(dims.Value.Value or []) if dims.StatusCode.is_good() and dims.Value else None,
                "accessLevel": access.Value.Value if access.StatusCode.is_good() else None,
                "userAccessLevel": user_access.Value.Value if user_access.StatusCode.is_good() else None,
                "description": descr.Value.Value.Text if descr.StatusCode.is_good() and descr.Value.Value else None,
                "euRange": {"low": eu_range.Low, "high": eu_range.High} if eu_range is not None else None,
                "engineeringUnits": ({"display": units.DisplayName.Text, "unitId": units.UnitId,
                                      "namespace": units.NamespaceUri} if units is not None else None),
            })
    return {"nodes": out}


class _Collector:
    def __init__(self) -> None:
        self.values: dict[int, list[Any]] = {}
        self.event = asyncio.Event()
        self.count = 0

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        handle = data.monitored_item.ClientHandle
        self.values.setdefault(handle, []).append(jsonable(val))
        self.count += 1
        self.event.set()

    def status_change_notification(self, status: Any) -> None:
        pass


async def s_subscribe(a: argparse.Namespace) -> dict[str, Any]:
    ids = read_lines(a.nodes)
    client = await make_client(a, user=a.user, password=a.password)
    started = time.monotonic()
    async with client:
        coll = _Collector()
        sub = await client.create_subscription(ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=500, RequestedLifetimeCount=600, RequestedMaxKeepAliveCount=20,
            MaxNotificationsPerPublish=0, PublishingEnabled=True, Priority=0), coll)
        reqs = []
        for i, text in enumerate(ids):
            params = ua.MonitoringParameters(ClientHandle=i, SamplingInterval=250, QueueSize=10, DiscardOldest=True)
            reqs.append(ua.MonitoredItemCreateRequest(
                ItemToMonitor=ua.ReadValueId(NodeId=ua.NodeId.from_string(text), AttributeId=ua.AttributeIds.Value),
                MonitoringMode=ua.MonitoringMode.Reporting, RequestedParameters=params))
        results = await sub.create_monitored_items(reqs)
        bad = [hexcode(r) for r in results if isinstance(r, ua.StatusCode)]
        handles = {i for i, r in enumerate(results) if not isinstance(r, ua.StatusCode)}
        deadline = time.monotonic() + a.timeout

        def changed() -> int:
            return sum(1 for v in coll.values.values() if len(set(map(json.dumps, v))) >= 2)

        while time.monotonic() < deadline and changed() < len(handles):
            await asyncio.sleep(0.1)
        result = {"requested": len(ids), "created_ok": len(handles), "created_bad": bad,
                  "initial": sum(1 for v in coll.values.values() if v), "changed": changed(),
                  "notifications": coll.count, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        with contextlib.suppress(Exception):
            await sub.delete()
    return result


async def s_deadband(a: argparse.Namespace) -> dict[str, Any]:
    client = await make_client(a, user=a.user, password=a.password)
    async with client:
        coll = _Collector()
        sub = await client.create_subscription(ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=100, RequestedLifetimeCount=600, RequestedMaxKeepAliveCount=20,
            MaxNotificationsPerPublish=0, PublishingEnabled=True, Priority=0), coll)
        reqs = []
        nid = ua.NodeId.from_string(a.node)
        for handle, deadband in ((0, None), (1, a.deadband)):
            params = ua.MonitoringParameters(ClientHandle=handle, SamplingInterval=0, QueueSize=100,
                                             DiscardOldest=True)
            if deadband is not None:
                params.Filter = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValue,
                                                    DeadbandType=int(ua.DeadbandType.Absolute),
                                                    DeadbandValue=deadband)
            reqs.append(ua.MonitoredItemCreateRequest(
                ItemToMonitor=ua.ReadValueId(NodeId=nid, AttributeId=ua.AttributeIds.Value),
                MonitoringMode=ua.MonitoringMode.Reporting, RequestedParameters=params))
        results = await sub.create_monitored_items(reqs)
        await asyncio.sleep(a.duration)
        with contextlib.suppress(Exception):
            await sub.delete()
    return {"create_status": [hexcode(r) if isinstance(r, ua.StatusCode) else "0x00000000" for r in results],
            "unfiltered": coll.values.get(0, []), "filtered": coll.values.get(1, [])}


async def s_write(a: argparse.Namespace) -> dict[str, Any]:
    client = await make_client(a, user=a.user, password=a.password)
    async with client:
        node = client.get_node(a.node)
        before = await node.read_value()
        variant = await node.read_data_type_as_variant_type()
        params = ua.WriteParameters()
        params.NodesToWrite = [ua.WriteValue(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value,
                                             Value=ua.DataValue(ua.Variant(7, variant)))]
        try:
            codes = await client.uaclient.write(params)
            status, fault = hexcode(codes[0]), False
        except ua.UaStatusCodeError as exc:
            status, fault = hexcode(exc.code), True
        after = await node.read_value()
    return {"status": status, "service_fault": fault, "before": jsonable(before), "after": jsonable(after)}


async def s_reconnect(a: argparse.Namespace) -> dict[str, Any]:
    """Subscribe, signal ready, survive a server restart: asyncua has no automatic session
    recovery, so this is the application-level loop an asyncua-based SCADA would run
    (reconnect with backoff, then re-create the subscription)."""
    ids = read_lines(a.nodes)
    coll = _Collector()
    state: dict[str, Any] = {"disconnect_seen": False, "recovered": False, "mechanism": "recreate",
                             "reconnect_attempts": 0, "notifications_after": 0}

    async def subscribe(client: Client) -> None:
        sub = await client.create_subscription(250, coll)
        await sub.subscribe_data_change([client.get_node(i) for i in ids])

    client = await make_client(a, user=a.user, password=a.password)
    await client.connect()
    await subscribe(client)
    deadline = time.monotonic() + a.timeout
    while coll.count == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    emit(event="ready")
    lost_at = None
    while time.monotonic() < deadline:
        try:
            await client.check_connection()
            await asyncio.sleep(0.2)
            if state["disconnect_seen"] and coll.count > state["notifications_after"]:
                state["recovered"] = True
                state["recovery_ms"] = int((time.monotonic() - lost_at) * 1000)
                state["notifications_after"] = coll.count
                emit(event="recovered")
                break
            continue
        except Exception:
            if not state["disconnect_seen"]:
                emit(event="disconnected")
                lost_at = time.monotonic()
            state["disconnect_seen"] = True
        with contextlib.suppress(Exception):
            await client.disconnect()
        while time.monotonic() < deadline:
            state["reconnect_attempts"] += 1
            try:          # set_security() fetches the server certificate: it can fail too
                client = await make_client(a, user=a.user, password=a.password)
                await client.connect()
                state["notifications_after"] = coll.count
                await subscribe(client)
                break
            except Exception:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                await asyncio.sleep(0.5)
    with contextlib.suppress(Exception):
        await client.disconnect()
    return state


SCENARIOS = {"endpoints": s_endpoints, "connect": s_connect, "browse": s_browse, "translate": s_translate,
             "read": s_read, "subscribe": s_subscribe, "deadband": s_deadband, "write": s_write,
             "reconnect": s_reconnect}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    p.add_argument("--endpoint", required=True)
    p.add_argument("--ns", type=int, default=2)
    p.add_argument("--app-uri", default="urn:opencap:scada-l1:client")
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--key-der")
    p.add_argument("--user")
    p.add_argument("--password", default="")
    p.add_argument("--policy", default="Basic256Sha256", choices=sorted(POLICIES))
    p.add_argument("--mode", default="SignAndEncrypt", choices=sorted(MODES))
    p.add_argument("--nodes")
    p.add_argument("--paths")
    p.add_argument("--node")
    p.add_argument("--deadband", type=float, default=5.0)
    p.add_argument("--duration", type=float, default=6.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--max-refs", type=int, default=0)
    p.add_argument("--request-timeout", type=float, default=10.0)
    a = p.parse_args()
    try:
        result = asyncio.run(SCENARIOS[a.scenario](a))
    except Exception as exc:
        emit(result={"error": repr(exc)})
        return 1
    emit(result=result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
