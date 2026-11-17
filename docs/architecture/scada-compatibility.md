# SCADA compatibility matrix

Goal: every SCADA system that talks to KEPServerEX today can talk to opencap, with no
change on the SCADA side beyond pointing it at a new server. Strategy and rationale:
[ADR-0009](../adr/0009-scada-connectivity-strategy.md).

**Rows marked *verify* are the lead's current understanding and must be confirmed by the
SCADA research issue on the board before they are documented to users.** Vendor product
names are used only to describe compatibility.

## Matrix

| SCADA / consumer | Primary path to opencap | Alternatives | Not possible | Notes |
|---|---|---|---|---|
| Inductive Automation Ignition | OPC UA server (Ignition's OPC UA client connection) | MQTT Sparkplug B through a third-party MQTT Engine module; Modbus TCP server | — | Ignition's OPC UA stack is Eclipse Milo, so Milo is our L1 test client |
| AVEVA InTouch / System Platform (Wonderware) | OPC UA through AVEVA's OI Gateway (*verify* version support) | OPC DA through OI Gateway / FSGateway (needs the OPC DA bridge) | SuiteLink, FastDDE (proprietary, undocumented) | Most InTouch sites using KEPServerEX use SuiteLink: their tags must be repointed to an OI Gateway topic |
| Siemens WinCC V7/V8 | OPC UA (WinCC OPC UA channel, *verify* minimum version) | OPC DA (OPC channel, needs the bridge) | — | |
| Siemens WinCC Unified, WinCC OA | OPC UA | MQTT (WinCC OA) | — | |
| Rockwell FactoryTalk View SE/ME | OPC DA ("OPC data server", needs the bridge) | OPC UA through FactoryTalk Linx Gateway or FactoryTalk Optix (*verify*) | — | The OPC DA bridge matters most here |
| GE Vernova Proficy iFIX | OPC UA (iFIX OPC UA client, *verify* version) | OPC DA (OPC client driver, needs the bridge) | iFIX native IGS interface (proprietary) | GE's IGS is an OEM KEPServerEX; IGS sites are a primary audience |
| GE CIMPLICITY | OPC UA | OPC DA | — | *verify* |
| AVEVA Plant SCADA (Citect) | OPC UA driver (*verify* version) | OPC DA; Modbus TCP server | — | |
| Schneider Geo SCADA (ClearSCADA) | DNP3 outstation | OPC DA; Modbus TCP server | — | Utilities often prefer DNP3 |
| Trihedral VTScada | OPC UA | DNP3 outstation; Modbus TCP server | — | |
| COPA-DATA zenon | OPC UA | IEC 104 server (energy) | — | |
| AVEVA Edge (InduSoft) | OPC UA | OPC DA; MQTT | — | |
| AVEVA PI (OSIsoft) | PI Adapter / Connector for OPC UA | PI Adapter for MQTT; PI Interface for OPC DA | — | |
| AWS IoT SiteWise, Azure IoT | OPC UA (gateway / OPC Publisher) | MQTT | — | |
| Node-RED, Grafana, IT systems | MQTT, REST | OPC UA | — | |
| Energy SCADA / RTU masters | IEC 60870-5-104 server, DNP3 outstation | — | — | |

## North servers needed, by number of SCADA rows they unlock

1. **OPC UA server** — almost every row. In flight (`opcua`, Python), Rust port in M3.
2. **OPC DA bridge (Windows)** — FactoryTalk View, older WinCC, iFIX, AVEVA via OI Gateway.
3. **MQTT Sparkplug B** — Ignition MQTT architectures, cloud.
4. **Modbus TCP server** — universal fallback; every SCADA has a Modbus master.
5. **DNP3 outstation / IEC 104 server** — utilities and energy.

## OPC UA features SCADA clients depend on

The OPC UA server must support, and the L1 suite must test: GetEndpoints with the policies
of [03 §3.7](03-deep-dive.md#37-security); anonymous and username tokens; Browse and
TranslateBrowsePathsToNodeIds on a 100k-node address space; Read of `Value`, `DataType`,
`AccessLevel`, `EURange`; subscriptions with data-change filters (status, status-value,
status-value-timestamp; absolute deadband); arrays and strings; `ServerStatus`, `ServiceLevel`
(redundancy); limits on sessions and monitored items; KEPServerEX-compatible NodeIds
`ns=<idx>;s=Channel.Device.Tag`.

## Test levels

| Level | What | Where | Who |
|---|---|---|---|
| L1 | Automated conformance with OSS clients: Eclipse Milo (Ignition's stack), open62541 client, asyncua; Eclipse Tahu / Sparkplug TCK; pymodbus client; DNP3/IEC 104 masters from OSS test tools | CI | engineers |
| L2 | Manual connection test with the real SCADA product (trial or owner licence), using a recipe in the docs | owner's lab | owner (or delegated) |
| L3 | Formal certification (OPC Foundation CTT/certification lab) | external, paid | owner decision |

## Recipes

One page per SCADA under `docs/scada/<product>.md` (created by the SCADA research issue):
connection type, endpoint and security settings, how tag paths map
(`Channel.Device.Tag`), what changes when migrating from KEPServerEX, known limits.
