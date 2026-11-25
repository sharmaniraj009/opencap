"""L1 SCADA compatibility harness: simulators, the engine as a child process, scenario clients.

    plant = Plant.start(workdir)                 # Modbus simulators + value ticker (own thread)
    engine = EngineProcess(config_dir, kind)     # `opencap run -c <dir>` (or the Rust binary)
    engine.start(); engine.restart(); engine.stop()
    result = run_client("milo", "browse", engine, plant, extra_args)

Everything a scenario client needs comes from argv; the client prints JSON lines on stdout
and its last line is `{"result": {...}}`. The assertions live in `test_l1.py`, so the three
clients (Eclipse Milo, open62541, asyncua) are judged by exactly the same rules. See
tests/scada/README.md for the client protocol.

No site data: devices, channels and tags are invented; every simulator listens on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opencap.config.models import Device, PublishCfg, Scale, Tag
from tests.helpers.configdir import write_config
from tests.helpers.modbus_sim import SIM_PARAMS, ModbusSim
from tests.helpers.netcut import stable_port

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHANNEL = "Plant A"
NS_URI = "urn:opencap:scada-l1"
CLIENT_APP_URI = "urn:opencap:scada-l1:client"
USER, PASSWORD = "scada", "l1-test-password"        # test-only credentials, local simulator
TICK_S = 0.25                                        # simulator value change period
DYN_DEVICES = 4                                      # PLC2..PLC5
DYN_TAGS = 250                                       # per dynamic device: 4 x 250 = 1,000 items

# Every policy the server offers in the suite. Secure ones use the harness client certificate.
POLICIES = [
    "None",
    "Basic256Sha256_Sign", "Basic256Sha256_SignAndEncrypt",
    "Aes128_Sha256_RsaOaep_Sign", "Aes128_Sha256_RsaOaep_SignAndEncrypt",
    "Aes256_Sha256_RsaPss_Sign", "Aes256_Sha256_RsaPss_SignAndEncrypt",
]
POLICY_URI = {
    "None": "http://opcfoundation.org/UA/SecurityPolicy#None",
    "Basic256Sha256": "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
    "Aes128_Sha256_RsaOaep": "http://opcfoundation.org/UA/SecurityPolicy#Aes128_Sha256_RsaOaep",
    "Aes256_Sha256_RsaPss": "http://opcfoundation.org/UA/SecurityPolicy#Aes256_Sha256_RsaPss",
}
MODE = {"None": 1, "Sign": 2, "SignAndEncrypt": 3}   # MessageSecurityMode


def expected_endpoints() -> set[tuple[str, int]]:
    """(SecurityPolicyUri, MessageSecurityMode) of every endpoint the server must offer."""
    out = set()
    for p in POLICIES:
        if p == "None":
            out.add((POLICY_URI["None"], MODE["None"]))
        else:
            policy, _, mode = p.rpartition("_")
            out.add((POLICY_URI[policy], MODE[mode]))
    return out


# --- the simulated plant ---------------------------------------------------------------------

# PLC1: static values (read scenarios compare them exactly), a ramp (deadband), and a string
# and an array that change (subscriptions on non-scalar, non-numeric values).
STATIC: dict[str, tuple[str, str, Any]] = {          # name: (address, dtype, value)
    "Static.Int16": ("400001", "int16", -1234),
    "Static.UInt16": ("400002", "uint16", 54321),
    "Static.Int32": ("400003", "int32", -123456),
    "Static.UInt32": ("400005", "uint32", 3_000_000_000),
    "Static.Float": ("400007", "float32", 12.5),
    "Static.Double": ("400009", "float64", 3.25),
    "Static.Bool": ("400013.3", "bool", True),
    "Static.Text": ("400020.16H", "string", "AUTO MODE"),
    "Static.Int16Array": ("400040[4]", "int16", [1, -2, 3, -4]),
    "Static.FloatArray": ("400050[3]", "float32", [0.5, 1.5, -2.25]),
    "Line1.Motor.Speed": ("400060", "float32", 1450.0),
}
LEVEL = ("400070", "uint16", 500)                     # scaled: raw 0..1000 -> 0..100 % (EURange)
DYNAMIC = {
    "Ramp": ("400080", "int16"),
    "Dyn.Text": ("400090.16H", "string"),
    "Dyn.Int16Array": ("400100[4]", "int16"),
}
WRITE_TARGET = "Static.Int16"


def plc1() -> Device:
    tags = [Tag(name=n, address=a, dtype=t) for n, (a, t, _v) in STATIC.items()]
    tags.append(Tag(name="Level", address=LEVEL[0], dtype=LEVEL[1], description="Tank level",
                    scale=Scale(raw_lo=0, raw_hi=1000, eng_lo=0, eng_hi=100, units="%")))
    tags += [Tag(name=n, address=a, dtype=t) for n, (a, t) in DYNAMIC.items()]
    return _device("PLC1", tags, scan_ms=250)


def dyn_device(k: int) -> Device:
    tags = [Tag(name=f"Values.V{i:03d}", address=f"4{i + 1:05d}", dtype="uint16") for i in range(DYN_TAGS)]
    return _device(f"PLC{k + 2}", tags, scan_ms=500)


def _device(name: str, tags: list[Tag], scan_ms: int) -> Device:
    return Device(id=f"{CHANNEL}/{name}", channel=CHANNEL, name=name, driver="modbus_tcp", host="127.0.0.1",
                  port=0, params={**SIM_PARAMS, "max_holding": 120}, scan_ms=scan_ms, min_scan_ms=scan_ms,
                  timeout_ms=1000, retries=1, publish=PublishCfg(sub_device_id=f"L1_{name}"), tags=tags)


def plant_devices() -> list[Device]:
    return [plc1(), *(dyn_device(k) for k in range(DYN_DEVICES))]


def dyn_value(tick: int, i: int) -> int:
    return (i * 101 + tick) % 65536


def text_value(tick: int) -> str:
    return f"STEP {tick % 1000:03d}"


def array_value(tick: int) -> list[int]:
    return [(tick + k) % 30000 for k in range(4)]


class Plant:
    """Modbus simulators for `plant_devices()` plus a ticker, on an event loop in a thread, so
    they keep serving while the test blocks on a client subprocess."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="scada-plant", daemon=True)
        self.sims: dict[str, ModbusSim] = {}
        self.devices: list[Device] = []
        self.tick = 0
        self._ticker: asyncio.Task | None = None

    @classmethod
    def start(cls) -> Plant:
        plant = cls()
        plant.thread.start()
        plant._call(plant._start())
        return plant

    def _call(self, coro, timeout: float = 30.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _start(self) -> None:
        for dev in plant_devices():
            sim = await ModbusSim.start(dev)
            self.sims[dev.name] = sim
            self.devices.append(sim.device)
        p1 = self.sims["PLC1"]
        for name, (_a, _t, value) in STATIC.items():
            p1.set_value(name, value)
        p1.set_value("Level", LEVEL[2])
        self._apply(0)
        self._ticker = asyncio.create_task(self._run())

    def _apply(self, tick: int) -> None:
        p1 = self.sims["PLC1"]
        p1.set_value("Ramp", tick % 30000)
        p1.set_value("Dyn.Text", text_value(tick))
        p1.set_value("Dyn.Int16Array", array_value(tick))
        for k in range(DYN_DEVICES):
            sim = self.sims[f"PLC{k + 2}"]
            for i in range(DYN_TAGS):
                sim.set_value(f"Values.V{i:03d}", dyn_value(tick, k * DYN_TAGS + i))

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.tick += 1
            self._apply(self.tick)

    def stop(self) -> None:
        async def _stop() -> None:
            if self._ticker is not None:
                self._ticker.cancel()
                await asyncio.gather(self._ticker, return_exceptions=True)
            for sim in self.sims.values():
                await sim.stop()
        with contextlib.suppress(Exception):
            self._call(_stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


# --- expectations derived from the config (the reference address-space rules, D-046) --------

VARIANT_TYPES = {"bool": "Boolean", "int16": "Int16", "uint16": "UInt16", "int32": "Int32", "uint32": "UInt32",
                 "int64": "Int64", "uint64": "UInt64", "float32": "Float", "float64": "Double", "string": "String"}
DATATYPE_ID = {"Boolean": 1, "Int16": 4, "UInt16": 5, "Int32": 6, "UInt32": 7, "Int64": 8, "UInt64": 9,
               "Float": 10, "Double": 11, "String": 12, "DateTime": 13}


def tag_type(tag: Tag) -> str:
    return "Double" if tag.scale is not None else VARIANT_TYPES[tag.dtype]


def canon(nodeid: str | None) -> str | None:
    """A NodeId string in one spelling: stacks differ on whether namespace 0 is written
    ("ns=0;i=85" from Milo, "i=85" from asyncua and open62541)."""
    if nodeid and nodeid.startswith("ns=0;"):
        return nodeid[len("ns=0;"):]
    return nodeid


def node_id(ns: int, device: Device, name: str) -> str:
    return f"ns={ns};s={device.channel}.{device.name}.{name}"


@dataclass(frozen=True)
class Node:
    id: str
    name: str            # "<ns>:<BrowseName>"
    cls: int             # NodeClass: 1 Object, 2 Variable
    parent: str          # NodeId of the parent ("i=85" = Objects)


def expected_tree(ns: int, devices: Iterable[Device], system_vars: Iterable[str]) -> set[Node]:
    """Folders per channel/device/tag group, a variable per enabled tag, the `_System` folder."""
    nodes: set[Node] = set()
    for dev in devices:
        nodes.add(Node(f"ns={ns};s={dev.channel}", f"{ns}:{dev.channel}", 1, "i=85"))
        base = f"{dev.channel}.{dev.name}"
        nodes.add(Node(f"ns={ns};s={base}", f"{ns}:{dev.name}", 1, f"ns={ns};s={dev.channel}"))
        for tag in dev.tags:
            if not tag.enabled:
                continue
            parent = base
            parts = tag.name.split(".")
            for part in parts[:-1]:
                path = f"{parent}.{part}"
                nodes.add(Node(f"ns={ns};s={path}", f"{ns}:{part}", 1, f"ns={ns};s={parent}"))
                parent = path
            nodes.add(Node(f"ns={ns};s={parent}.{parts[-1]}", f"{ns}:{parts[-1]}", 2, f"ns={ns};s={parent}"))
        system = f"{base}._System"
        nodes.add(Node(f"ns={ns};s={system}", f"{ns}:_System", 1, f"ns={ns};s={base}"))
        for name in system_vars:
            nodes.add(Node(f"ns={ns};s={system}.{name}", f"{ns}:{name}", 2, f"ns={ns};s={system}"))
    return nodes


# --- the engine as a child process -----------------------------------------------------------

@dataclass
class Pki:
    """A client application instance certificate shared by the three clients (trusted by the
    server), as DER cert + PEM and DER (PKCS#8) key."""
    cert_der: Path
    key_pem: Path
    key_der: Path


def make_client_pki(directory: Path) -> Pki:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "opencap scada-l1 client"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "opencap tests")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(CLIENT_APP_URI),
                                                    x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                     data_encipherment=True, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    pki = Pki(directory / "client_cert.der", directory / "client_key.pem", directory / "client_key.der")
    pki.cert_der.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    pki.key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    pki.key_der.write_bytes(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    return pki


def write_engine_config(workdir: Path, devices: list[Device], port: int, pki: Pki) -> Path:
    trusted = workdir / "pki" / "trusted"
    trusted.mkdir(parents=True, exist_ok=True)
    shutil.copy(pki.cert_der, trusted / "scada-l1-client.der")
    return write_config(workdir, devices, mqtt_port=stable_port(), poller={
        "log_level": "WARNING",
        "opcua_server": {
            "enabled": True,
            "endpoint": f"opc.tcp://127.0.0.1:{port}/opencap",
            "namespace_uri": NS_URI,
            "security_policies": POLICIES,
            "certificate": str(workdir / "pki" / "own" / "server_cert.der"),
            "private_key": str(workdir / "pki" / "own" / "server_key.pem"),
            "trusted_certs_dir": str(trusted),
            "anonymous": True,
            "username_env": "OPENCAP_L1_USER",
            "password_env": "OPENCAP_L1_PASSWORD",
            "update_ms": 100,
        },
    })


class EngineProcess:
    """`opencap run -c <config_dir>` (Python reference) or `opencap-engine run --config <dir>`
    (Rust), with the OPC UA server enabled. Only the config dir is shared with the test."""

    def __init__(self, config_dir: Path, kind: str, workdir: Path, endpoint: str) -> None:
        self.config_dir = config_dir
        self.kind = kind
        self.workdir = workdir
        self.endpoint = endpoint
        self.proc: subprocess.Popen | None = None
        self.log_path = workdir / "engine.log"
        self.starts = 0

    def command(self) -> list[str]:
        if self.kind == "python":
            return [sys.executable, "-m", "opencap", "run", "-c", str(self.config_dir)]
        from tests.helpers.engine import rust_binary

        binary = rust_binary()
        if binary is None:
            raise FileNotFoundError("Rust engine binary not found (build engine/ or set OPENCAP_ENGINE_BIN)")
        return [str(binary), "run", "--config", str(self.config_dir)]

    def start(self, timeout: float = 60.0) -> None:
        env = {**os.environ, "OPENCAP_L1_USER": USER, "OPENCAP_L1_PASSWORD": PASSWORD, "PYTHONUNBUFFERED": "1"}
        log = self.log_path.open("ab")
        log.write(f"\n--- start {self.starts + 1} ---\n".encode())
        log.flush()
        self.proc = subprocess.Popen(self.command(), stdout=log, stderr=subprocess.STDOUT, env=env,
                                     cwd=str(self.workdir))
        log.close()
        self.starts += 1
        self.wait_ready(timeout)

    def wait_ready(self, timeout: float) -> None:
        """Until GetEndpoints answers and every PLC1 static tag has its simulator value."""
        host, port = _host_port(self.endpoint)
        deadline = time.monotonic() + timeout
        while True:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"engine exited with {self.proc.returncode}:\n{self.log_tail()}")
            with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=0.5):
                if asyncio.run(_values_ready(self.endpoint)):
                    return
            if time.monotonic() > deadline:
                raise RuntimeError(f"OPC UA server not ready after {timeout}s:\n{self.log_tail()}")
            time.sleep(0.2)

    def stop(self, timeout: float = 20.0) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM if os.name != "nt" else signal.CTRL_BREAK_EVENT)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError(f"engine did not stop within {timeout}s:\n{self.log_tail()}") from None

    def restart(self) -> None:
        self.stop()
        self.start()

    def log_tail(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no engine log)"


def _host_port(endpoint: str) -> tuple[str, int]:
    rest = endpoint.split("://", 1)[1].split("/", 1)[0]
    host, _, port = rest.rpartition(":")
    return host, int(port)


async def _values_ready(endpoint: str) -> bool:
    import logging

    from asyncua import Client

    logging.getLogger("asyncua").setLevel(logging.ERROR)     # one session-timeout warning per poll

    client = Client(endpoint, timeout=3)
    try:
        await client.connect()
    except Exception:
        return False
    try:
        ns = (await client.get_namespace_array()).index(NS_URI)
        for name in STATIC:
            dv = await client.get_node(f"ns={ns};s={CHANNEL}.PLC1.{name}").read_data_value(raise_on_bad_status=False)
            if not dv.StatusCode.is_good():
                return False
        return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


# --- scenario clients ------------------------------------------------------------------------

MILO_JAR = HERE / "milo-client" / "target" / "scada-l1-milo-client.jar"
OPEN62541_BIN = Path(os.environ.get("OPENCAP_OPEN62541_CLIENT",
                                    HERE / "open62541-client" / "build" / "scada_l1_open62541"))
CLIENTS = ("milo", "open62541", "asyncua")


def client_command(client: str) -> list[str] | None:
    """argv prefix of a scenario client, or None when it is not built here."""
    if client == "asyncua":
        return [sys.executable, str(HERE / "asyncua_client.py")]
    if client == "milo":
        java = shutil.which("java")
        return [java, "-jar", str(MILO_JAR)] if java and MILO_JAR.exists() else None
    if client == "open62541":
        return [str(OPEN62541_BIN)] if OPEN62541_BIN.exists() else None
    raise ValueError(client)


def build_hint(client: str) -> str:
    return {
        "milo": "build it with `mvn -B -q -f tests/scada/milo-client/pom.xml package` (Java 17+)",
        "open62541": "build it with `tests/scada/open62541-client/build.sh` (cmake, a C compiler, OpenSSL)",
        "asyncua": "install the `opcua` extra",
    }[client]


@dataclass
class ClientRun:
    client: str
    scenario: str
    argv: list[str]
    returncode: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr: str = ""
    elapsed_s: float = 0.0

    def describe(self) -> str:
        return (f"{self.client} {self.scenario} exited {self.returncode} after {self.elapsed_s:.1f}s\n"
                f"argv: {' '.join(self.argv)}\nresult: {json.dumps(self.result)[:2000]}\n"
                f"stderr (tail):\n{self.stderr[-3000:]}")


def common_args(endpoint: str, pki: Pki, ns: int) -> list[str]:
    return ["--endpoint", endpoint, "--ns", str(ns), "--app-uri", CLIENT_APP_URI,
            "--cert", str(pki.cert_der), "--key", str(pki.key_pem), "--key-der", str(pki.key_der),
            "--user", USER, "--password", PASSWORD]


def run_client(client: str, scenario: str, args: list[str], *, timeout: float = 120.0,
               on_event=None) -> ClientRun:
    """Run one scenario client to completion. `on_event(event_dict)` is called for every
    `{"event": ...}` line as it arrives (the restart scenario uses it)."""
    prefix = client_command(client)
    if prefix is None:
        raise FileNotFoundError(f"{client} client not built: {build_hint(client)}")
    argv = [*prefix, "--scenario", scenario, *args]
    run = ClientRun(client, scenario, argv)
    start = time.monotonic()
    err_chunks: list[str] = []
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                          cwd=str(HERE)) as proc:
        err_thread = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
        err_thread.start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "event" in msg:
                    run.events.append(msg)
                    if on_event is not None:
                        on_event(msg)
                elif "result" in msg:
                    run.result = msg["result"]
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            err_thread.join(5)
    run.returncode = proc.returncode
    run.stderr = "".join(err_chunks)
    run.elapsed_s = time.monotonic() - start
    if time.monotonic() - start >= timeout:
        run.stderr += f"\n(harness: killed after {timeout}s)"
    return run
