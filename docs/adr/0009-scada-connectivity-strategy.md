# 0009. SCADA connectivity: OPC UA first, KEPServerEX-compatible naming

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead

## Context
The owner wants northbound support for Ignition, AVEVA/Wonderware, WinCC, FactoryTalk,
iFIX, Citect and others. KEPServerEX reaches them through OPC UA, OPC DA, and proprietary
links (SuiteLink, iFIX native). See [scada-compatibility.md](../architecture/scada-compatibility.md).

## Decision
1. **OPC UA server is the primary SCADA interface** (in flight in Python; Rust in M3).
2. **Naming is KEPServerEX-compatible**: tag path `Channel.Device[.Group…].Tag`; OPC UA
   NodeId `ns=<idx>;s=<path>` with a configurable namespace URI, and an option to serve the
   address space at namespace index 2 so SCADA projects that reference KEPServerEX NodeIds
   keep working after repointing. OPC DA item ids use the same path.
3. **OPC DA** comes through a separate Windows bridge process that implements an OPC DA
   2.05a/3.0 server over the engine's API or OPC UA (RFC 0004; the OPC Foundation
   redistributables question is reserved to the owner).
4. **Order after OPC UA:** Sparkplug B (Ignition/MQTT), Modbus TCP server (universal),
   DNP3 outstation and IEC 104 server (utilities).
5. **Not offered:** SuiteLink, FastDDE/DDE, iFIX native interface, ThingWorx native: closed
   protocols. The docs give each affected SCADA its OPC UA (or DA) recipe instead.
6. Compatibility is tested at level L1 in CI with OSS clients (Eclipse Milo, the stack
   Ignition uses; open62541; asyncua; Sparkplug TCK); L2/L3 are owner-run.

## Consequences
The OPC UA server must scale to the full address space (100k+ nodes) and implement the
features SCADA clients rely on (scada-compatibility.md). The opcua PR is reviewed against
the naming rule above.

## Alternatives considered
Reverse-engineering SuiteLink (legal and maintenance risk); building OPC DA first
(Windows-only, COM/DCOM complexity, shrinking market).

## Revisit when
A large user base needs a proprietary link that has a published specification.

## As built (#96, 2026-11-18)
- The Python OPC UA server (`north/opcua_server.py`, D-046) follows the naming rule: NodeIds are
  `ns=2;s=<channel>.<device>[.<group>].<tag>`, with tag groups as folders. The configured
  `namespace_uri` is registered first, so it is always index 2; set it to `KEPServerEX` for
  clients that resolve the namespace by URI. Each device has a `_System` folder (`_Error`, `_State`,
  `_LastGood`, ...), and `_System` is a reserved tag-group name in both engines.
- Read-only in three layers (CurrentRead access, users never Admin, a permission ruleset that
  refuses Write, HistoryUpdate, Call and node management). Values are copied from the cache, so no
  client action reaches a PLC.
- Secure by default: off unless enabled, bound to 127.0.0.1 by default, anonymous off, and
  policy `None` refused on an endpoint that is not loopback unless `allow_insecure_remote`.
- Still open: PKI folders, X.509 and several users, limits, EURange and units, a separate
  advertised URL, and the 100k-node scale test (#28); the L1 suite with Milo and open62541
  (#29); the Rust server (#50).
