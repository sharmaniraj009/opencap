"""asyncua service overrides for the northbound OPC UA server (docs/decisions.md D-088).

Everything here is per server instance (subclasses installed on our `Server` only), not a
global patch of asyncua, so other asyncua servers and clients in the process are unchanged:

- **Limits:** `max_sessions` (BadTooManySessions at CreateSession), `max_subscriptions_per_session`
  (BadTooManySubscriptions), `max_monitored_items` server-wide (BadTooManyMonitoredItems per
  item), publishing and sampling intervals revised up to the configured minimums.
- **Writes:** every Write is answered per node with Bad_NotWritable (Bad_NodeIdUnknown for an
  unknown node) instead of a ServiceFault; nothing is written (ADR-0008).
- **Browse paging:** at most `max_references_per_browse` references (or fewer, if the client
  asks for fewer) per node; the rest via a continuation point and BrowseNext. asyncua's server
  ignores RequestedMaxReferencesPerNode and has no BrowseNext.
- **Data-change filters:** the deadband is checked against the last *reported* value (Part 4,
  7.22.2), not the previous sample as asyncua does; a status change is always reported; a
  deadband on a non-numeric variable is refused (BadFilterNotAllowed) instead of raising in the
  value copy loop; percent deadband uses the variable's EURange (BadDeadbandFilterInvalid
  without one).

asyncua internals used here are pinned by the `opcua` extra (`asyncua>=2.1,<2.2`, D-045).
"""

from __future__ import annotations

import copy
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from asyncua import Server, ua
from asyncua.common.utils import ServiceError
from asyncua.crypto import uacrypto
from asyncua.server.binary_server_asyncio import BinaryServer, OPCUAProtocol
from asyncua.server.internal_session import InternalSession
from asyncua.server.monitored_item_service import MonitoredItemService
from asyncua.server.uaprocessor import UaProcessor
from asyncua.ua.ua_binary import struct_from_binary

log = structlog.get_logger("opencap.north.opcua_server")

MAX_CONTINUATION_POINTS = 16            # per session (Part 4 5.8.2: Bad_NoContinuationPoints beyond)
_NUMERIC_TYPES = frozenset(range(2, 12))   # SByte .. Double (ns=0 DataType ids)
_BROWSE_NEXT = ua.NodeId(ua.ObjectIds.BrowseNextRequest_Encoding_DefaultBinary)


@dataclass(frozen=True)
class Limits:
    max_sessions: int = 100
    max_subscriptions_per_session: int = 50
    max_monitored_items: int = 100_000
    min_publishing_interval_ms: float = 100.0
    min_sampling_interval_ms: float = 100.0
    max_references_per_browse: int = 1000

    @classmethod
    def from_cfg(cls, cfg: Any) -> Limits:
        return cls(cfg.max_sessions, cfg.max_subscriptions_per_session, cfg.max_monitored_items,
                   float(cfg.min_publishing_interval_ms), float(cfg.min_sampling_interval_ms),
                   cfg.max_references_per_browse)


# --- pure helpers ---------------------------------------------------------------------------

def revise_sampling(requested: float, publishing_ms: float, minimum: float) -> float:
    """RevisedSamplingInterval: -1 = the publishing interval; anything else at least `minimum`."""
    if requested < 0:
        return publishing_ms
    return max(float(requested), minimum)


def _numbers(value: Any) -> list[float] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        out: list[float] = []
        for v in value:
            inner = _numbers(v)
            if inner is None:
                return None
            out += inner
        return out
    return None


def deadband_report(current: Any, reference: Any, deadband_type: int, deadband: float,
                    span: float | None = None) -> bool:
    """Whether `current` (a DataValue) is reported, against `reference`, the last DataValue the
    monitored item reported (None: nothing yet). Part 4 7.22.2: a status change is always
    reported; Absolute: |value - last reported| > deadband; Percent: > deadband/100 * EURange
    span; arrays: any element (a change of length always). Non-numeric values: any change."""
    if reference is None or current is None:
        return True
    if current.StatusCode != reference.StatusCode:
        return True
    if deadband_type == ua.DeadbandType.None_:
        return True
    cur = _numbers(current.Value.Value if current.Value is not None else None)
    ref = _numbers(reference.Value.Value if reference.Value is not None else None)
    if cur is None or ref is None or len(cur) != len(ref):
        return (current.Value.Value if current.Value else None) != (reference.Value.Value if reference.Value else None)
    limit = deadband
    if deadband_type == ua.DeadbandType.Percent:
        if not span:
            return True
        limit = deadband / 100.0 * span
    return any(abs(a - b) > limit for a, b in zip(cur, ref, strict=True))


def page_references(refs: list[Any], requested: int, maximum: int) -> tuple[list[Any], list[Any]]:
    """(first page, rest) of a node's references: `requested` 0 means the server's maximum."""
    limit = min(requested, maximum) if requested else maximum
    return refs[:limit], refs[limit:]


# --- monitored items ------------------------------------------------------------------------

class _MonitoredItems(MonitoredItemService):
    """One subscription's monitored items with limits, filter validation and the deadband fix."""

    def __init__(self, isub: Any, aspace: Any, state: ServerState) -> None:
        super().__init__(isub, aspace)
        self._state = state

    async def create_monitored_items(self, params: ua.CreateMonitoredItemsParameters) -> list[Any]:
        room = self._state.limits.max_monitored_items - self._state.monitored_item_count()
        results = []
        for item in params.ItemsToCreate:
            if item.ItemToMonitor.AttributeId == ua.AttributeIds.EventNotifier:
                results.append(self._create_events_monitored_item(item))
                continue
            if room <= 0:
                results.append(ua.MonitoredItemCreateResult(
                    StatusCode=ua.StatusCode(ua.StatusCodes.BadTooManyMonitoredItems)))
                continue
            refused, span = self._check_filter(item.ItemToMonitor, item.RequestedParameters.Filter)
            if refused is not None:
                results.append(ua.MonitoredItemCreateResult(StatusCode=ua.StatusCode(refused)))
                continue
            result = await self._create_data_change_monitored_item(item)
            if result.StatusCode.is_good():
                room -= 1
                result.RevisedSamplingInterval = revise_sampling(
                    item.RequestedParameters.SamplingInterval, self.isub.data.RevisedPublishingInterval,
                    self._state.limits.min_sampling_interval_ms)
                self._monitored_items[result.MonitoredItemId].mvalue.eu_span = span
            results.append(result)
        if room <= 0:
            log.warning("opcua_server.monitored_item_limit", limit=self._state.limits.max_monitored_items)
        return results

    def modify_monitored_items(self, params: ua.ModifyMonitoredItemsParameters) -> list[Any]:
        results = []
        for item in params.ItemsToModify:
            mdata = self._monitored_items.get(item.MonitoredItemId)
            if mdata is None:
                results.append(ua.MonitoredItemModifyResult(
                    StatusCode=ua.StatusCode(ua.StatusCodes.BadMonitoredItemIdInvalid)))
                continue
            refused, span = self._check_filter(mdata.read_value_id, item.RequestedParameters.Filter)
            if refused is not None:
                results.append(ua.MonitoredItemModifyResult(StatusCode=ua.StatusCode(refused)))
                continue
            mdata.filter = item.RequestedParameters.Filter
            mdata.queue_size = self._revise_queue_size(item.RequestedParameters.QueueSize)
            mdata.mvalue.eu_span = span
            results.append(ua.MonitoredItemModifyResult(
                RevisedSamplingInterval=revise_sampling(item.RequestedParameters.SamplingInterval,
                                                        self.isub.data.RevisedPublishingInterval,
                                                        self._state.limits.min_sampling_interval_ms),
                RevisedQueueSize=mdata.queue_size))
        return results

    def _check_filter(self, rvid: Any, flt: Any) -> tuple[int | None, float | None]:
        """(refusal StatusCode or None, EURange span for a percent deadband)."""
        # Duck-typed: asyncua re-creates structure classes from the nodeset, so a decoded filter
        # is not always an instance of ua.DataChangeFilter.
        if getattr(flt, "DeadbandType", None) in (None, ua.DeadbandType.None_):
            return None, None
        if rvid.AttributeId != ua.AttributeIds.Value or rvid.NodeId not in self.aspace:
            return None, None              # asyncua reports the bad node / attribute itself
        dtype = self.aspace.read_attribute_value(rvid.NodeId, ua.AttributeIds.DataType).Value
        dtype = dtype.Value if dtype is not None else None
        if not (isinstance(dtype, ua.NodeId) and dtype.NamespaceIndex == 0 and dtype.Identifier in _NUMERIC_TYPES):
            return ua.StatusCodes.BadFilterNotAllowed, None
        if flt.DeadbandValue < 0:
            return ua.StatusCodes.BadDeadbandFilterInvalid, None
        if flt.DeadbandType == ua.DeadbandType.Percent:
            span = eu_span(self.aspace, rvid.NodeId)
            if not span or flt.DeadbandValue > 100:
                return ua.StatusCodes.BadDeadbandFilterInvalid, None
            return None, span
        if flt.DeadbandType != ua.DeadbandType.Absolute:
            return ua.StatusCodes.BadDeadbandFilterInvalid, None
        return None, None

    async def datachange_callback(self, handle: int, value: ua.DataValue, error: ua.StatusCode | None = None) -> None:
        """asyncua's callback with a shallow copy of the value instead of a deep copy (the server
        writes a new DataValue for every change; deepcopy was over half of the CPU at 20k
        changes/s, D-088). Same trigger, filter and queue logic."""
        mid = self._monitored_datachange.get(handle)
        mdata = self._monitored_items.get(mid) if mid is not None else None
        if error or mdata is None:
            await super().datachange_callback(handle, value, error)
            return
        if mdata.mode == ua.MonitoringMode.Disabled:
            return
        values = mdata.mvalue
        values.old_dvalue, values.current_dvalue = values.current_dvalue, copy.copy(value)
        if mdata.filter:
            passes = self._is_data_changed(values, mdata.filter.Trigger) and self._is_deadband_exceeded(values,
                                                                                                    mdata.filter)
        else:
            passes = self._is_data_changed(values, ua.DataChangeTrigger.StatusValue)
        if passes:
            event = ua.MonitoredItemNotification(ClientHandle=mdata.client_handle, Value=value)
            await self.isub.enqueue_datachange_event(mid, event, mdata.queue_size)

    def _is_deadband_exceeded(self, values: Any, flt: ua.DataChangeFilter) -> bool:
        # Called only after asyncua's trigger check passed; `values.reported` is ours.
        current = values.get_current_datavalue()
        report = deadband_report(current, getattr(values, "reported", None), flt.DeadbandType, flt.DeadbandValue,
                                 getattr(values, "eu_span", None))
        if report:
            values.reported = current
        return report


def eu_span(aspace: Any, nodeid: Any) -> float | None:
    """High - Low of a variable's EURange property, None without one."""
    node = aspace.get(nodeid)
    if node is None:
        return None
    for ref in node.references:
        if ref.IsForward and ref.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasProperty) \
                and ref.BrowseName.Name == "EURange":
            value = aspace.read_attribute_value(ref.NodeId, ua.AttributeIds.Value).Value
            rng = value.Value if value is not None else None
            if isinstance(rng, ua.Range):
                return float(rng.High) - float(rng.Low)
    return None


# --- sessions -------------------------------------------------------------------------------

class _Session(InternalSession):
    """A client session with the limits, refused writes and browse paging above."""

    def __init__(self, state: ServerState, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state = state
        self._continuations: dict[bytes, list[Any]] = {}

    async def create_session(self, params: ua.CreateSessionParameters, sockname: Any = None) -> Any:
        if len(self._state.sessions) >= self._state.limits.max_sessions:
            log.warning("opcua_server.session_limit", limit=self._state.limits.max_sessions)
            raise ServiceError(ua.StatusCodes.BadTooManySessions)
        result = await super().create_session(params, sockname=sockname)
        self._state.sessions.add(self)
        return result

    def activate_session(self, params: ua.ActivateSessionParameters, peer_certificate: bytes | None) -> Any:
        token = params.UserIdentityToken
        self._state.token_kind = type(token)          # read by the user manager (sync call below)
        try:
            return super().activate_session(params, peer_certificate)
        finally:
            self._state.token_kind = None

    async def close_session(self, delete_subs: bool = True) -> None:
        self._state.sessions.discard(self)
        self._continuations.clear()
        await super().close_session(delete_subs)

    async def create_subscription(self, params: ua.CreateSubscriptionParameters, callback: Callable[..., Any],
                                  request_callback: Callable[..., Any] | None = None) -> Any:
        own = sum(1 for sub in self.subscription_service.subscriptions.values() if sub.session_id == self.session_id)
        if own >= self._state.limits.max_subscriptions_per_session:
            raise ServiceError(ua.StatusCodes.BadTooManySubscriptions)
        params.RequestedPublishingInterval = max(float(params.RequestedPublishingInterval or 0),
                                                 self._state.limits.min_publishing_interval_ms)
        result = await super().create_subscription(params, callback, request_callback=request_callback)
        sub = self.subscription_service.subscriptions.get(result.SubscriptionId)
        if sub is not None:
            sub.monitored_item_srv = _MonitoredItems(sub, self.aspace, self._state)
        return result

    def modify_subscription(self, params: ua.ModifySubscriptionParameters) -> Any:
        params.RequestedPublishingInterval = max(float(params.RequestedPublishingInterval or 0),
                                                 self._state.limits.min_publishing_interval_ms)
        return super().modify_subscription(params)

    async def write(self, params: ua.WriteParameters) -> list[ua.StatusCode]:
        log.info("opcua_server.write_refused", nodes=len(params.NodesToWrite), user=getattr(self.user, "name", None))
        return [ua.StatusCode(ua.StatusCodes.BadNotWritable if wv.NodeId in self.aspace
                              else ua.StatusCodes.BadNodeIdUnknown) for wv in params.NodesToWrite]

    async def browse(self, params: ua.BrowseParameters) -> list[ua.BrowseResult]:
        results = self.iserver.view_service.browse(params)
        requested = int(params.RequestedMaxReferencesPerNode or 0)
        for result in results:
            page, rest = page_references(list(result.References), requested, self._state.limits.max_references_per_browse)
            if rest:
                result.References = page
                result.ContinuationPoint = self._keep(rest, requested)
                if result.ContinuationPoint is None:
                    result.References = []
                    result.StatusCode = ua.StatusCode(ua.StatusCodes.BadNoContinuationPoints)
        return results

    def browse_next_pages(self, params: ua.BrowseNextParameters) -> list[ua.BrowseResult]:
        results = []
        for cp in params.ContinuationPoints:
            entry = self._continuations.pop(bytes(cp or b""), None)
            if entry is None:
                results.append(ua.BrowseResult(StatusCode=ua.StatusCode(ua.StatusCodes.BadContinuationPointInvalid)))
                continue
            if params.ReleaseContinuationPoints:
                results.append(ua.BrowseResult())
                continue
            refs, requested = entry
            page, rest = page_references(refs, requested, self._state.limits.max_references_per_browse)
            results.append(ua.BrowseResult(References=page, ContinuationPoint=self._keep(rest, requested) if rest else None))
        return results

    def _keep(self, rest: list[Any], requested: int) -> bytes | None:
        if len(self._continuations) >= MAX_CONTINUATION_POINTS:
            return None
        cp = os.urandom(12)
        self._continuations[cp] = [rest, requested]
        return cp


class ServerState:
    """Per-server state the overrides share: limits, live sessions, the user token kind."""

    def __init__(self, iserver: Any, limits: Limits) -> None:
        self.iserver = iserver
        self.limits = limits
        self.sessions: set[_Session] = set()
        self.token_kind: type | None = None

    def create_session(self, name: str, user: Any = None, external: bool = False) -> _Session:
        from asyncua.crypto.permission_rules import User, UserRole

        return _Session(self, self.iserver, self.iserver.aspace, self.iserver.subscription_service, name,
                        user=user if user is not None else User(role=UserRole.Anonymous), external=external)

    def monitored_item_count(self) -> int:
        return sum(len(sub.monitored_item_srv._monitored_items)
                   for sub in self.iserver.subscription_service.subscriptions.values())


# --- transport: BrowseNext ------------------------------------------------------------------

class _Processor(UaProcessor):
    async def _process_message(self, typeid: Any, requesthdr: Any, seqhdr: Any, body: Any) -> bool:
        if typeid == _BROWSE_NEXT and isinstance(self.session, _Session):
            perms = self._connection.security_policy.permissions
            if perms is not None and perms.check_validity(self.session.user, typeid, body) is False:
                raise ua.uaerrors.BadUserAccessDenied
            self.session_last_activity = time.monotonic()
            self.session.touch()
            params = struct_from_binary(ua.BrowseNextParameters, body)
            response = ua.BrowseNextResponse()
            response.Parameters = ua.BrowseNextResult(Results=self.session.browse_next_pages(params))
            self.send_response(requesthdr.RequestHandle, seqhdr, response)
            return True
        return await super()._process_message(typeid, requesthdr, seqhdr, body)


class _Protocol(OPCUAProtocol):
    def connection_made(self, transport: Any) -> None:
        super().connection_made(transport)
        if self.processor is not None and type(self.processor) is UaProcessor:
            self.processor.__class__ = _Processor


class _BinaryServer(BinaryServer):
    def _make_protocol(self) -> OPCUAProtocol:
        proto = super()._make_protocol()
        proto.__class__ = _Protocol
        return proto


class UaServer(Server):
    """asyncua's Server with our per-connection protocol. `start()` is asyncua 2.1's, with
    `_BinaryServer` in place of `BinaryServer` (the only change)."""

    async def start(self) -> None:
        if self.iserver.certificate is not None:
            uacrypto.check_certificate(self.iserver.certificate, self._application_uri, socket.gethostname())
        await self._setup_server_nodes()
        await self.iserver.start()
        try:
            host, port = self._get_bind_socket_info()
            self.bserver = _BinaryServer(self.iserver, host, port, self.limits)
            self.bserver.set_policies(self._policies)
            await self.bserver.start()
        except Exception:
            await self.iserver.stop()
            raise


def install(srv: Server, limits: Limits) -> ServerState:
    """Hook our sessions into `srv` (before it starts)."""
    state = ServerState(srv.iserver, limits)
    srv.iserver.create_session = state.create_session
    # asyncua's own server-wide subscription cap must not be the tighter one
    srv.iserver.max_subscriptions = max(srv.iserver.max_subscriptions,
                                        limits.max_sessions * limits.max_subscriptions_per_session)
    return state
