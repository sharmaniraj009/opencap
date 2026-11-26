"""OPC UA server hardening (#28, docs/decisions.md D-088) against a real asyncua client:
address space metadata, limits, browse paging, data-change filters, PKI folders with rejected
and CA-signed client certificates, several users, X.509 user tokens, the advertised URL.
(Cross-stack checks with Eclipse Milo and open62541: tests/scada, the L1 suite.)"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("asyncua")

from asyncua import Client, ua  # noqa: E402
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from opencap.config.models import Device, OpcUaServerCfg, Scale, Tag  # noqa: E402
from opencap.core.cache import TagValue, ValueCache  # noqa: E402
from opencap.north.opcua_security import thumbprint  # noqa: E402
from opencap.north.opcua_server import OpcUaNorth  # noqa: E402
from opencap.north.opcua_services import MAX_CONTINUATION_POINTS  # noqa: E402
from tests.helpers.modbus_sim import free_port  # noqa: E402
from tests.helpers.opcua_sim import shelf_file  # noqa: E402

T0 = datetime(2026, 11, 25, 6, 0, 0, tzinfo=UTC)
DEV = "Plant A/PLC1"
APP_URI = "urn:opencap:test:client"
SECURE = "Basic256Sha256_SignAndEncrypt"


def device() -> Device:
    tags = [Tag(name=f"Group.T{i:02d}", address=f"4{i + 1:05d}", dtype="int16") for i in range(10)]
    tags += [
        Tag(name="Ramp", address="400100", dtype="float32"),
        Tag(name="Level", address="400101", dtype="uint16", description="Tank level",
            scale=Scale(raw_lo=0, raw_hi=1000, eng_lo=0, eng_hi=100, units="%")),
        Tag(name="Temp", address="400102", dtype="int16", scale=Scale(eng_lo=-20, eng_hi=80, units="°C")),
        Tag(name="Text", address="400110.16H", dtype="string"),
        Tag(name="Row", address="400130[4]", dtype="int16"),
        Tag(name="Grid", address="400140[2][3]", dtype="int16"),
    ]
    return Device(id=DEV, channel="Plant A", name="PLC1", driver="modbus_tcp", host="192.0.2.11", port=502,
                  tags=tags)


def status(_device_id: str) -> dict[str, Any]:
    return {"state": "ok", "connected": True, "errors": 0}


async def start_north(**cfg: Any) -> tuple[OpcUaNorth, ValueCache]:
    dev = device()
    cache = ValueCache()
    cache.register(dev)
    cache.update(DEV, {"Ramp": TagValue(0.0, "good", T0), "Level": TagValue(50.0, "good", T0),
                       "Text": TagValue("AUTO", "good", T0), "Row": TagValue([1, 2, 3, 4], "good", T0),
                       "Grid": TagValue([[1, 2, 3], [4, 5, 6]], "good", T0)})
    cfg.setdefault("endpoint", f"opc.tcp://127.0.0.1:{free_port()}/opencap")
    cfg.setdefault("anonymous", True)
    north = OpcUaNorth(OpcUaServerCfg(enabled=True, update_ms=50, **cfg), [dev], cache, status,
                       shelf_file=await shelf_file())
    await north.start()
    return north, cache


@pytest.fixture
async def north():
    srv, cache = await start_north(min_publishing_interval_ms=50, min_sampling_interval_ms=0)
    try:
        yield srv, cache
    finally:
        await srv.stop()


async def wait_for(pred, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = pred()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


class Collector:
    def __init__(self) -> None:
        self.values: dict[int, list[Any]] = {}
        self.status: dict[int, list[int]] = {}

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        handle = data.monitored_item.ClientHandle
        self.values.setdefault(handle, []).append(val)
        self.status.setdefault(handle, []).append(data.monitored_item.Value.StatusCode.value)

    def status_change_notification(self, status: Any) -> None:
        pass


def item(nodeid: ua.NodeId, handle: int, flt: Any = None, sampling: float = 0.0) -> ua.MonitoredItemCreateRequest:
    params = ua.MonitoringParameters(ClientHandle=handle, SamplingInterval=sampling, QueueSize=100,
                                     DiscardOldest=True, Filter=flt)
    return ua.MonitoredItemCreateRequest(ItemToMonitor=ua.ReadValueId(NodeId=nodeid, AttributeId=ua.AttributeIds.Value),
                                         MonitoringMode=ua.MonitoringMode.Reporting, RequestedParameters=params)


def deadband(kind: ua.DeadbandType, value: float) -> ua.DataChangeFilter:
    return ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValue, DeadbandType=int(kind), DeadbandValue=value)


# --- address space ---------------------------------------------------------------------------

async def test_scaled_tags_are_analog_items_with_eurange_and_units(north):
    srv, _cache = north
    async with Client(srv.cfg.endpoint, timeout=5) as client:
        level = client.get_node(srv.node_id(DEV, "Level"))
        assert (await level.read_type_definition()) == ua.NodeId(ua.ObjectIds.AnalogItemType)
        props = {(await p.read_browse_name()).Name: await p.read_value() for p in await level.get_properties()}
        assert props["EURange"] == ua.Range(Low=0.0, High=100.0)
        units = props["EngineeringUnits"]
        assert units.DisplayName.Text == "%" and units.UnitId == (ord("P") << 8 | ord("1"))
        assert units.NamespaceUri == "http://www.opcfoundation.org/UA/units/un/cefact"
        assert (await level.read_description()).Text == "Tank level"
        temp = client.get_node(srv.node_id(DEV, "Temp"))
        temp_props = {(await p.read_browse_name()).Name: await p.read_value() for p in await temp.get_properties()}
        assert temp_props["EURange"] == ua.Range(Low=-20.0, High=80.0)
        assert temp_props["EngineeringUnits"].UnitId == (ord("C") << 16 | ord("E") << 8 | ord("L"))
        plain = client.get_node(srv.node_id(DEV, "Ramp"))
        assert (await plain.read_type_definition()) == ua.NodeId(ua.ObjectIds.BaseDataVariableType)
        assert await plain.get_properties() == []
        assert srv.namespace_index == 2 and (await client.get_namespace_array())[2] == srv.cfg.namespace_uri


async def test_arrays_have_their_dimensions_before_the_first_value(north):
    srv, cache = north
    async with Client(srv.cfg.endpoint, timeout=5) as client:
        row, grid = client.get_node(srv.node_id(DEV, "Row")), client.get_node(srv.node_id(DEV, "Grid"))
        assert (await row.read_value_rank(), await row.read_array_dimensions()) == (1, [4])
        assert (await grid.read_value_rank(), await grid.read_array_dimensions()) == (2, [2, 3])
        await wait_for(lambda: grid.read_value(), what="grid value")
        assert await row.read_value() == [1, 2, 3, 4]
        assert await grid.read_value() == [[1, 2, 3], [4, 5, 6]]
        scalar = client.get_node(srv.node_id(DEV, "Ramp"))
        assert await scalar.read_value_rank() == ua.ValueRank.Scalar


# --- limits ----------------------------------------------------------------------------------

async def test_session_limit():
    srv, _cache = await start_north(max_sessions=2)
    clients = [Client(srv.cfg.endpoint, timeout=5) for _ in range(3)]
    try:
        await clients[0].connect()
        await clients[1].connect()
        with pytest.raises(ua.UaStatusCodeError, match="BadTooManySessions"):
            await clients[2].connect()
        await clients[0].disconnect()
        await wait_for(lambda: len(srv._state.sessions) < 2, what="the session to close")
        third = Client(srv.cfg.endpoint, timeout=5)
        await third.connect()                     # room again
        await third.disconnect()
    finally:
        for c in clients[1:]:
            with contextlib_suppress():
                await c.disconnect()
        await srv.stop()


class contextlib_suppress:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


async def test_subscription_and_monitored_item_limits_and_revised_intervals():
    srv, _cache = await start_north(max_subscriptions_per_session=1, max_monitored_items=3,
                                    min_publishing_interval_ms=400, min_sampling_interval_ms=300)
    try:
        async with Client(srv.cfg.endpoint, timeout=5) as client:
            result = await client.uaclient.create_subscription(
                ua.CreateSubscriptionParameters(RequestedPublishingInterval=50, RequestedLifetimeCount=600,
                                                RequestedMaxKeepAliveCount=20, PublishingEnabled=True),
                callback=lambda *_: None)
            assert result.RevisedPublishingInterval == 400.0
            with pytest.raises(ua.UaStatusCodeError, match="BadTooManySubscriptions"):
                await client.create_subscription(500, Collector())
            nodes = [ua.NodeId.from_string(srv.node_id(DEV, f"Group.T0{i}")) for i in range(5)]
            created = await client.uaclient.create_monitored_items(ua.CreateMonitoredItemsParameters(
                SubscriptionId=result.SubscriptionId, TimestampsToReturn=ua.TimestampsToReturn.Both,
                ItemsToCreate=[item(n, i, sampling=10.0) for i, n in enumerate(nodes)]))
            codes = [r.StatusCode.value for r in created]
            assert codes == [0, 0, 0, ua.StatusCodes.BadTooManyMonitoredItems, ua.StatusCodes.BadTooManyMonitoredItems]
            assert [r.RevisedSamplingInterval for r in created[:3]] == [300.0, 300.0, 300.0]
    finally:
        await srv.stop()


# --- browse paging ---------------------------------------------------------------------------

def browse_params(nodeid: ua.NodeId, max_refs: int = 0) -> ua.BrowseParameters:
    desc = ua.BrowseDescription(NodeId=nodeid, BrowseDirection=ua.BrowseDirection.Forward,
                                ReferenceTypeId=ua.NodeId(ua.ObjectIds.HierarchicalReferences), IncludeSubtypes=True,
                                NodeClassMask=0, ResultMask=int(ua.BrowseResultMask.All))
    return ua.BrowseParameters(NodesToBrowse=[desc], RequestedMaxReferencesPerNode=max_refs)


async def test_browse_hands_out_continuation_points():
    srv, _cache = await start_north(max_references_per_browse=3)
    group = ua.NodeId.from_string(srv.node_id(DEV, "Group"))
    try:
        async with Client(srv.cfg.endpoint, timeout=5) as client:
            first = (await client.uaclient.browse(browse_params(group)))[0]
            names = [r.BrowseName.Name for r in first.References]
            assert len(names) == 3 and first.ContinuationPoint
            cp = first.ContinuationPoint
            while cp:
                page = (await client.uaclient.browse_next(ua.BrowseNextParameters(
                    ReleaseContinuationPoints=False, ContinuationPoints=[cp])))[0]
                assert page.StatusCode.is_good() and len(page.References) <= 3
                names += [r.BrowseName.Name for r in page.References]
                cp = page.ContinuationPoint
            assert names == [f"T{i:02d}" for i in range(10)]
            # the client asks for fewer than the server's cap
            two = (await client.uaclient.browse(browse_params(group, max_refs=2)))[0]
            assert len(two.References) == 2
            # release, then the point is gone
            released = (await client.uaclient.browse_next(ua.BrowseNextParameters(
                ReleaseContinuationPoints=True, ContinuationPoints=[two.ContinuationPoint])))[0]
            assert released.StatusCode.is_good() and not released.References
            gone = (await client.uaclient.browse_next(ua.BrowseNextParameters(
                ReleaseContinuationPoints=False, ContinuationPoints=[two.ContinuationPoint])))[0]
            assert gone.StatusCode.value == ua.StatusCodes.BadContinuationPointInvalid
            # a session holds a bounded number of points
            results = [(await client.uaclient.browse(browse_params(group)))[0]
                       for _ in range(MAX_CONTINUATION_POINTS + 1)]
            assert all(r.ContinuationPoint for r in results[:MAX_CONTINUATION_POINTS])
            assert results[-1].StatusCode.value == ua.StatusCodes.BadNoContinuationPoints
    finally:
        await srv.stop()


# --- data-change filters ---------------------------------------------------------------------

async def push(srv: OpcUaNorth, cache: ValueCache, client: Client, name: str, value: Any, quality: str = "good"):
    cache.update(DEV, {name: TagValue(value, quality, T0)})
    srv.notify(DEV)
    node = client.get_node(srv.node_id(DEV, name))

    async def arrived():
        dv = await node.read_data_value(raise_on_bad_status=False)
        return dv.Value.Value == value and (quality != "good") == (not dv.StatusCode.is_good())

    await wait_for(arrived, what=f"{name}={value}")


async def test_absolute_deadband_is_checked_against_the_last_reported_value(north):
    srv, cache = north
    async with Client(srv.cfg.endpoint, timeout=5) as client:
        coll = Collector()
        sub = await client.create_subscription(50, coll)
        ramp = ua.NodeId.from_string(srv.node_id(DEV, "Ramp"))
        await sub.create_monitored_items([item(ramp, 0), item(ramp, 1, deadband(ua.DeadbandType.Absolute, 5.0))])
        await wait_for(lambda: coll.values.get(1), what="initial value")
        for value in range(1, 20):
            await push(srv, cache, client, "Ramp", float(value))
        await wait_for(lambda: coll.values.get(0, [None])[-1] == 19.0, what="every sample")
        assert coll.values[1] == [0.0, 6.0, 12.0, 18.0]           # asyncua alone: [0.0] (D-088)
        # a status change inside the deadband is reported
        await push(srv, cache, client, "Ramp", 19.0, quality="stale")
        await wait_for(lambda: len(coll.status[1]) == 5, what="the status change")
        assert coll.status[1][-1] == ua.StatusCodes.UncertainLastUsableValue


async def test_percent_deadband_uses_eurange_and_bad_filters_are_refused(north):
    srv, cache = north
    async with Client(srv.cfg.endpoint, timeout=5) as client:
        coll = Collector()
        sub = await client.create_subscription(50, coll)
        node = lambda name: ua.NodeId.from_string(srv.node_id(DEV, name))  # noqa: E731
        created = await sub.create_monitored_items([
            item(node("Level"), 0, deadband(ua.DeadbandType.Percent, 10.0)),   # 10 % of 0..100 = 10
            item(node("Text"), 1, deadband(ua.DeadbandType.Absolute, 1.0)),    # not numeric
            item(node("Ramp"), 2, deadband(ua.DeadbandType.Percent, 10.0)),    # no EURange
            item(node("Ramp"), 3, deadband(ua.DeadbandType.Absolute, -1.0)),
        ])
        assert not isinstance(created[0], ua.StatusCode)
        assert [c.value for c in created[1:]] == [ua.StatusCodes.BadFilterNotAllowed,
                                                  ua.StatusCodes.BadDeadbandFilterInvalid,
                                                  ua.StatusCodes.BadDeadbandFilterInvalid]
        for value in (55.0, 59.0, 61.0, 65.0, 72.0):
            await push(srv, cache, client, "Level", value)
        await wait_for(lambda: coll.values.get(0, [])[-1:] == [72.0], what="the last level")
        assert coll.values[0] == [50.0, 61.0, 72.0]
        # the string node still updates for everyone (a deadband on it used to raise in the copy loop)
        await push(srv, cache, client, "Text", "MANUAL")


# --- PKI, users, X.509 -----------------------------------------------------------------------

def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def issue(directory: Path, cn: str, *, issuer: tuple[Any, x509.Certificate] | None = None, ca: bool = False,
          app_uri: str = APP_URI) -> tuple[Path, Path, Any, x509.Certificate]:
    """(cert.der, key.pem, key, cert): self-signed, or signed by `issuer` = (key, cert)."""
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    builder = (x509.CertificateBuilder().subject_name(_name(cn))
               .issuer_name(issuer[1].subject if issuer else _name(cn)).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
               .not_valid_after(now + timedelta(days=30))
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if issuer:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer[0].public_key()), critical=False)
    if ca:
        builder = (builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                                crl_sign=True, encipher_only=False, decipher_only=False), critical=True))
    else:
        builder = (builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(app_uri),
                                                               x509.DNSName("localhost")]), critical=False)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                                data_encipherment=True, key_agreement=False, key_cert_sign=False,
                                                crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
    cert = builder.sign(issuer[0] if issuer else key, hashes.SHA256())
    cert_path, key_path = directory / f"{cn}.der", directory / f"{cn}.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, key, cert


async def secure_client(endpoint: str, cert: Path, key: Path, **login: str) -> Client:
    client = Client(endpoint, timeout=5)
    client.application_uri = APP_URI
    await client.set_security(SecurityPolicyBasic256Sha256, str(cert), str(key),
                              mode=ua.MessageSecurityMode.SignAndEncrypt)
    if login:
        client.set_user(login["user"])
        client.set_password(login["password"])
    return client


async def test_pki_rejects_unknown_clients_into_rejected_and_trusts_them_without_restart(tmp_path):
    pki = tmp_path / "pki"
    srv, _cache = await start_north(security_policies=[SECURE], pki_dir=str(pki))
    try:
        assert (pki / "own" / "cert.der").exists() and (pki / "own" / "private" / "key.pem").exists()
        assert {p.name for p in pki.iterdir()} >= {"own", "trusted", "issuers", "crl", "rejected", "users"}
        cert, key, _k, client_cert = issue(tmp_path / "client", "scada client")
        client = await secure_client(srv.cfg.endpoint, cert, key)
        with pytest.raises(Exception):  # noqa: B017 - refused at CreateSession (BadCertificateUntrusted)
            await client.connect()
        with contextlib_suppress():
            await client.disconnect()
        der = client_cert.public_bytes(serialization.Encoding.DER)
        rejected = list((pki / "rejected").iterdir())
        assert [p.name for p in rejected] == [f"scada client [{thumbprint(der)}].der"]
        assert rejected[0].read_bytes() == der
        shutil.move(rejected[0], pki / "trusted" / rejected[0].name)     # what an admin does
        client = await secure_client(srv.cfg.endpoint, cert, key)
        await client.connect()                                           # same server, no restart
        await client.disconnect()
    finally:
        await srv.stop()


async def test_pki_builds_the_chain_through_issuers(tmp_path):
    pki = tmp_path / "pki"
    srv, _cache = await start_north(security_policies=[SECURE], pki_dir=str(pki))
    try:
        ca = tmp_path / "ca"
        _c, _k, root_key, root = issue(ca, "Plant A root CA", ca=True)
        _c, _k, mid_key, mid = issue(ca, "Plant A issuing CA", ca=True, issuer=(root_key, root))
        leaf_cert, leaf_key, _lk, _leaf = issue(tmp_path / "client", "hmi 1", issuer=(mid_key, mid))
        (pki / "trusted" / "root.der").write_bytes(root.public_bytes(serialization.Encoding.DER))
        (pki / "issuers" / "issuing.der").write_bytes(mid.public_bytes(serialization.Encoding.DER))
        client = await secure_client(srv.cfg.endpoint, leaf_cert, leaf_key)
        await client.connect()
        await client.disconnect()
        # the same chain without the trusted root is refused
        (pki / "trusted" / "root.der").unlink()
        client = await secure_client(srv.cfg.endpoint, leaf_cert, leaf_key)
        with pytest.raises(Exception):  # noqa: B017
            await client.connect()
        with contextlib_suppress():
            await client.disconnect()
    finally:
        await srv.stop()


async def test_several_users_from_env(monkeypatch, tmp_path):
    for k, v in {"T_U1": "scada", "T_P1": "pw-one", "T_U2": "historian", "T_P2": "pw-two"}.items():
        monkeypatch.setenv(k, v)
    srv, _cache = await start_north(anonymous=False, username_env="T_U1", password_env="T_P1",
                                    users=[{"username_env": "T_U2", "password_env": "T_P2"},
                                           {"username_env": "T_UNSET", "password_env": "T_UNSET_P"}])
    try:
        for user, password in (("scada", "pw-one"), ("historian", "pw-two")):
            client = Client(srv.cfg.endpoint, timeout=5)
            client.set_user(user)
            client.set_password(password)
            await client.connect()
            await client.disconnect()
        for user, password in (("historian", "pw-one"), ("nobody", "x")):
            client = Client(srv.cfg.endpoint, timeout=5)
            client.set_user(user)
            client.set_password(password)
            with pytest.raises(ua.UaStatusCodeError, match="BadUserAccessDenied"):
                await client.connect()
            with contextlib_suppress():
                await client.disconnect()
        anonymous = Client(srv.cfg.endpoint, timeout=5)
        with pytest.raises(ua.UaError):
            await anonymous.connect()
        with contextlib_suppress():
            await anonymous.disconnect()
    finally:
        await srv.stop()


async def test_x509_user_tokens_for_certificates_in_users(tmp_path):
    pki = tmp_path / "pki"
    srv, _cache = await start_north(security_policies=[SECURE], pki_dir=str(pki), anonymous=False, x509_users=True)
    try:
        app_cert, app_key, _k, _c = issue(tmp_path / "app", "scada app")
        shutil.copy(app_cert, pki / "trusted" / "app.der")
        user_cert, user_key, _uk, _uc = issue(tmp_path / "user", "operator 7")
        stranger_cert, stranger_key, _sk, _sc = issue(tmp_path / "user", "stranger")
        shutil.copy(user_cert, pki / "users" / "operator.der")
        for cert, key, ok in ((user_cert, user_key, True), (stranger_cert, stranger_key, False)):
            client = await secure_client(srv.cfg.endpoint, app_cert, app_key)
            await client.load_client_certificate(str(cert))
            await client.load_private_key(str(key))
            if ok:
                await client.connect()
                assert await client.get_node(srv.node_id(DEV, "Level")).read_value() == 50.0
                await client.disconnect()
            else:
                with pytest.raises(ua.UaStatusCodeError, match="BadUserAccessDenied"):
                    await client.connect()
                with contextlib_suppress():
                    await client.disconnect()
        endpoints = await Client(srv.cfg.endpoint, timeout=5).connect_and_get_server_endpoints()
        assert all(ua.UserTokenType.Certificate in {t.TokenType for t in e.UserIdentityTokens} for e in endpoints)
    finally:
        await srv.stop()


async def test_advertised_endpoint_and_certificate_host(tmp_path):
    port = free_port()
    advertised = "opc.tcp://opencap.example:14840/opencap"
    srv, _cache = await start_north(endpoint=f"opc.tcp://127.0.0.1:{port}/opencap", advertised_endpoint=advertised,
                                    security_policies=["None", SECURE], pki_dir=str(tmp_path / "pki"))
    try:
        endpoints = await Client(srv.cfg.endpoint, timeout=5).connect_and_get_server_endpoints()
        assert {e.EndpointUrl for e in endpoints} == {advertised}
        assert all(e.Server.DiscoveryUrls == [advertised] for e in endpoints)
        cert = x509.load_der_x509_certificate((tmp_path / "pki" / "own" / "cert.der").read_bytes())
        dns = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.DNSName)
        assert "opencap.example" in dns
        async with Client(srv.cfg.endpoint, timeout=5) as client:          # still reached on the bind address
            assert await client.get_node(srv.node_id(DEV, "Level")).read_value() == 50.0
    finally:
        await srv.stop()
