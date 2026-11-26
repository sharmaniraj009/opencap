"""L1 SCADA compatibility: the OPC UA server against Eclipse Milo, open62541 and asyncua clients.

Every test runs one scenario client per OPC UA stack against the engine (a child process
serving simulated Modbus devices) and judges the client's JSON report by the same rules.
The matrix in tests/scada/README.md lists the scenarios; ADR-0009 and
docs/architecture/scada-compatibility.md say why these features matter to SCADA clients.
"""

from __future__ import annotations

import itertools
import json
import math

import pytest

pytest.importorskip("asyncua")

from opencap.north.opcua_server import SYSTEM_VARS  # noqa: E402
from tests.scada import harness  # noqa: E402

pytestmark = [pytest.mark.scada]

GOOD = "0x00000000"
BAD_NO_MATCH = "0x806F0000"
WRITE_REFUSALS = {"0x803B0000": "BadNotWritable", "0x801F0000": "BadUserAccessDenied",
                  "0x80730000": "BadWriteNotSupported"}
SERVER_STATE_RUNNING = 0
# The reference implementation's per-device diagnostics (KEPServerEX `_System` names, D-046).
SYSTEM_NAMES = [name for name, _t, _f in SYSTEM_VARS]


def ok(run: harness.ClientRun) -> dict:
    assert run.returncode == 0 and "error" not in run.result, run.describe()
    return run.result


def record(record_property, result: dict) -> None:
    """Small scenario results go into the JUnit XML (<property name="result">)."""
    record_property("result", json.dumps(result, sort_keys=True))


def plc(l1, name: str):
    return next(d for d in l1.plant.devices if d.name == name)


def nid(l1, device: str, tag: str) -> str:
    return harness.node_id(l1.ns, plc(l1, device), tag)


# --- discovery and sessions ------------------------------------------------------------------

@pytest.mark.l1("GetEndpoints")
def test_get_endpoints(l1, client, run_scenario):
    eps = ok(run_scenario(client, "endpoints"))["endpoints"]
    assert {(e["policy"], e["mode"]) for e in eps} == harness.expected_endpoints()
    for e in eps:
        assert e["url"] == l1.endpoint, e
        assert {0, 1} <= set(e["tokens"]), f"anonymous and username tokens on every endpoint: {e}"
        if e["mode"] != 1:
            assert e["server_cert"], f"secure endpoint without a server certificate: {e}"


@pytest.mark.l1("Connect: every security policy x anonymous/username; wrong password refused")
def test_connect_each_policy_and_token(l1, client, run_scenario):
    attempts = ok(run_scenario(client, "connect", timeout=180))["attempts"]
    tried = {(a["policy"], a["mode"]) for a in attempts}
    assert tried == harness.expected_endpoints(), "the client must try every endpoint"
    failures = []
    for a in attempts:
        if a["token"] == "bad_password":
            if a["ok"]:
                failures.append(f"wrong password accepted on {a['policy']} mode {a['mode']}")
        elif not (a["ok"] and a["state"] == SERVER_STATE_RUNNING):
            failures.append(f"{a['token']} on {a['policy']} mode {a['mode']}: {a['error']}")
    assert not failures, "\n".join(failures)
    assert {a["token"] for a in attempts} == {"anonymous", "username", "bad_password"}


# --- address space ---------------------------------------------------------------------------

@pytest.mark.l1("Browse the full tree")
def test_browse_full_tree(l1, client, run_scenario):
    result = ok(run_scenario(client, "browse", "--max-refs", "50", timeout=180))
    got = {harness.Node(harness.canon(n["id"]), n["name"], n["class"], harness.canon(n["parent"]))
           for n in result["nodes"]}
    want = harness.expected_tree(l1.ns, l1.plant.devices, SYSTEM_NAMES)
    assert len(result["nodes"]) == len(got), "a reference was reported twice"
    missing, extra = sorted(want - got, key=str)[:10], sorted(got - want, key=str)[:10]
    assert got == want, f"missing (first 10): {missing}\nunexpected (first 10): {extra}"
    assert len(want) > 1000


@pytest.mark.l1("Browse with RequestedMaxReferencesPerNode=50 (BrowseNext)")
def test_browse_continuation_points(l1, client, run_scenario):
    result = ok(run_scenario(client, "browse", "--max-refs", "50", timeout=180))
    folders = sum(1 for n in result["nodes"] if n["class"] == 1)
    # Every node is browsed once; the four `Values` folders hold 250 variables each, so a server
    # that honours the limit hands out continuation points: at least 4 x 4 BrowseNext calls.
    assert result["browse_calls"] >= 1 + folders + 4 * 4, result["browse_calls"]


@pytest.mark.l1("TranslateBrowsePathsToNodeIds")
def test_translate_browse_paths(l1, client, run_scenario):
    devices = l1.plant.devices
    cases = {}
    for dev in devices[:2]:
        for tag in dev.tags:
            path = "/".join([dev.channel, dev.name, *tag.name.split(".")])
            cases[path] = harness.node_id(l1.ns, dev, tag.name)
        cases["/".join([dev.channel, dev.name, "_System", "_Error"])] = harness.node_id(l1.ns, dev, "_System._Error")
    missing = f"{harness.CHANNEL}/PLC1/NoSuchTag"
    paths = l1.write_lines("translate_paths.txt", [*cases, missing])
    results = {r["path"]: r for r in ok(run_scenario(client, "translate", "--paths", str(paths)))["results"]}
    for path, want in cases.items():
        r = results[path]
        assert r["status"] == GOOD and [harness.canon(t) for t in r["targets"]] == [want], r
    assert results[missing]["status"] == BAD_NO_MATCH, results[missing]


# --- reads -----------------------------------------------------------------------------------

def read_static(l1, client, run_scenario) -> dict[str, dict]:
    names = [*harness.STATIC, "Level"]
    nodes = l1.write_lines("read_nodes.txt", [nid(l1, "PLC1", n) for n in names])
    result = ok(run_scenario(client, "read", "--nodes", str(nodes)))
    return {n: r for n, r in zip(names, result["nodes"], strict=True)}


def same(got, want) -> bool:
    if isinstance(want, list):
        return isinstance(got, list) and len(got) == len(want) and all(map(same, got, want))
    if isinstance(want, float):
        return isinstance(got, (int, float)) and math.isclose(got, want, rel_tol=1e-6)
    return got == want and type(got) is type(want)


@pytest.mark.l1("Read Value, DataType, AccessLevel")
def test_read_value_datatype_access(l1, client, run_scenario):
    by_name = read_static(l1, client, run_scenario)
    tags = {t.name: t for t in plc(l1, "PLC1").tags}
    for name, r in by_name.items():
        want = harness.LEVEL[2] / 10 if name == "Level" else harness.STATIC[name][2]
        assert r["status"] == GOOD, (name, r)
        assert same(r["value"], float(want) if name == "Level" else want), (name, r["value"], want)
        assert harness.canon(r["datatype"]) == f"i={harness.DATATYPE_ID[harness.tag_type(tags[name])]}", (name, r)
        assert r["accessLevel"] == 1 and r["userAccessLevel"] == 1, f"{name}: CurrentRead only (ADR-0008): {r}"
        assert r["source_ts"], (name, r)


@pytest.mark.l1("Arrays and strings")
def test_array_and_string_values(l1, client, run_scenario):
    by_name = read_static(l1, client, run_scenario)
    for name in ("Static.Int16Array", "Static.FloatArray"):
        r = by_name[name]
        assert r["valueRank"] == 1, (name, r)
        assert same(r["value"], harness.STATIC[name][2]), (name, r)
    text = by_name["Static.Text"]
    assert text["value"] == "AUTO MODE" and text["valueRank"] == -1, text


@pytest.mark.l1("ArrayDimensions of arrays")
def test_array_dimensions(l1, client, run_scenario):
    by_name = read_static(l1, client, run_scenario)
    assert by_name["Static.Int16Array"]["arrayDims"] == [4]
    assert by_name["Static.FloatArray"]["arrayDims"] == [3]


@pytest.mark.l1("EURange, EngineeringUnits and Description")
def test_read_eurange(l1, client, run_scenario):
    by_name = read_static(l1, client, run_scenario)
    level = by_name["Level"]
    assert level["euRange"] == {"low": 0.0, "high": 100.0}, level
    units = level["engineeringUnits"]
    assert units and units["display"] == "%", level
    # UNECE common code "P1" (percent), packed as OPC UA Part 8 says
    assert units["unitId"] == (ord("P") << 8 | ord("1")), units
    assert units["namespace"] == "http://www.opcfoundation.org/UA/units/un/cefact", units
    assert level["description"] == "Tank level", level
    assert by_name["Static.Int16"]["euRange"] is None, "only scaled tags carry an EURange"


# --- subscriptions ---------------------------------------------------------------------------

@pytest.mark.l1("Subscribe 1,000 items, changes from the simulators")
def test_subscribe_1000_items(l1, client, run_scenario):
    ids = [harness.node_id(l1.ns, d, t.name) for d in l1.plant.devices[1:] for t in d.tags]
    assert len(ids) == 1000
    nodes = l1.write_lines("subscribe_nodes.txt", ids)
    r = ok(run_scenario(client, "subscribe", "--nodes", str(nodes), "--timeout", "45", timeout=120))
    assert r["created_ok"] == 1000 and not r["created_bad"], r
    assert r["changed"] == 1000, f"every item must report a value change driven by the simulator: {r}"


@pytest.mark.l1("Data-change filter, absolute deadband")
def test_deadband_absolute(l1, client, run_scenario):
    deadband = 5.0
    r = ok(run_scenario(client, "deadband", "--node", nid(l1, "PLC1", "Ramp"), "--deadband", str(deadband),
                        "--duration", "8"))
    assert r["create_status"] == [GOOD, GOOD], r
    filtered, unfiltered = r["filtered"], r["unfiltered"]
    assert len(filtered) >= 3, f"a ramp of {len(unfiltered)} steps must cross the deadband: {r}"
    steps = [b - a for a, b in itertools.pairwise(filtered)]
    assert all(abs(s) > deadband for s in steps), f"a notification inside the deadband: {filtered}"
    assert len(unfiltered) >= 3 * len(filtered), f"the filter must suppress notifications: {r}"


# --- read-only (ADR-0008) --------------------------------------------------------------------

@pytest.mark.l1("Write attempt rejected")
def test_write_rejected(l1, client, run_scenario, record_property):
    r = ok(run_scenario(client, "write", "--node", nid(l1, "PLC1", harness.WRITE_TARGET)))
    record(record_property, r)
    assert r["status"] in WRITE_REFUSALS, f"write must be refused with one of {WRITE_REFUSALS}: {r}"
    assert r["before"] == r["after"] == harness.STATIC[harness.WRITE_TARGET][2], r


# --- session recovery (runs last: it restarts the engine) -----------------------------------

@pytest.mark.l1("Reconnect after server restart")
def test_reconnect_after_server_restart(l1, client, run_scenario, record_property):
    ids = [nid(l1, "PLC1", "Ramp"), nid(l1, "PLC1", "Dyn.Text"), nid(l1, "PLC2", "Values.V000")]
    nodes = l1.write_lines("reconnect_nodes.txt", ids)
    restarted = []

    def on_event(event: dict) -> None:
        if event["event"] == "ready" and not restarted:
            l1.engine.restart()
            restarted.append(True)

    r = ok(run_scenario(client, "reconnect", "--nodes", str(nodes), "--timeout", "90", cache=False,
                        on_event=on_event, timeout=150))
    record(record_property, r)
    assert restarted, "the client never reported ready"
    assert r["disconnect_seen"], f"the client did not notice the restart: {r}"
    assert r["recovered"], f"no data change after the restart: {r}"
