# Plan: KEPServerEX feature parity

This folder turns the **KEPServerEX V6 server manual** (© 2022 PTC, 331 pages) into a build
plan for opencap: every feature the manual describes, where opencap stands today, what we
decide to do about it, testable requirements, and one checklist.

The manual itself is **not** in this repository (it is PTC's copyrighted document). Everything
here is summarised in our own words with page references such as `(manual p. 88)`, so anyone
with the manual can check the source.

## What is in here

| File | What it is |
|---|---|
| [`architecture.md`](architecture.md) | The architecture every feature follows: KEPServerEX concepts mapped to opencap components, invariants, where a feature goes, definition of done, build order |
| [`checklist.md`](checklist.md) | Every feature with its status, generated from the feature files |
| [`build_checklist.py`](build_checklist.py) | Regenerates `checklist.md` (`python plan/build_checklist.py`, `--check` in CI) |
| [`features/`](features/) | One file per feature area, below |

| Feature file | Area | IDs | Manual pages |
|---|---|---|---|
| [01](features/01-runtime-and-process-modes.md) | Runtime, process modes, system requirements, application data, service ports | `RT` | 17–22, 30–35, 57–60, 74–75 |
| [02](features/02-client-interfaces.md) | Client interfaces: OPC UA, DA, AE, HDA, .NET, DDE, FastDDE/SuiteLink, iFIX, ThingWorx, and their project properties | `IF` | 23–29, 36–47 |
| [03](features/03-projects-and-configuration-files.md) | Projects and configuration files: new/save/backup, file formats, encryption, naming rules, configuration how-tos | `PRJ` | 144–174 |
| [04](features/04-channels.md) | Channels: properties, serial/Ethernet, encapsulation, serialization, network interface, write optimizations, device discovery | `CH` | 76–85 |
| [05](features/05-devices.md) | Devices: operating mode, scan mode, auto-demotion, timing, tag generation, time sync, redundancy | `DEV` | 86–95 |
| [06](features/06-tags-groups-aliases.md) | Tags, tag groups, scaling, dynamic and static tags, multiple tag generation, alias map | `TAG` | 95–106 |
| [07](features/07-tag-management-and-system-tags.md) | CSV import/export; system, property, statistics, modem and serialization tags | `SYS` | 109–138 |
| [08](features/08-communications-management.md) | Modems, phonebook, auto-dial | `COM` | 139–143 |
| [09](features/09-administration-and-security.md) | Administration and settings, user manager, certificate store, ProgID redirect, Config API service settings | `ADM` | 56–75 |
| [10](features/10-configuration-api.md) | Configuration API service (gap analysis against `/api/v1`) | `API` | 175–220 |
| [11](features/11-store-and-forward.md) | Store and forward | `SAF` | 47–55, 232 |
| [12](features/12-diagnostics.md) | Event log, OPC diagnostics, communication diagnostics | `DIAG` | 107–108, 233–249 |
| [13](features/13-ifix.md) | Configuring from iFIX applications | `IFIX` | 221–231 |
| [14](features/14-ui-and-tools.md) | Designing a project in the UI: wizards, browsing, tag generation, testing (Quick Client) | `UI` | 144–164 |
| [15](features/15-event-log-messages.md) | The event-log message catalogue, grouped into event classes, and logging requirements | `LOG` | 250–331 |

Every feature file has the same shape: what the manual describes, opencap today, one section
per feature (what, decision, status, requirements, acceptance), open questions, and the
**Checklist rows** table that `checklist.md` is built from.

## Legend

**Decision**

- **Adopt:** same behaviour as KEPServerEX.
- **Adapt:** same goal, done the opencap way (the feature says how).
- **Defer:** later, with the issue or milestone named.
- **Out of scope:** not planned, with the reason (usually ADR-0008 read-only, ADR-0009 closed
  or COM-only interfaces, or the "Out of scope" list in `docs/architecture/01-requirements.md`).

**Status:** `Done` (on `main`), `In review (#PR)`, `Planned (#issue)`, `Not started`, `Out of scope`.

**Milestone:** M1–M5 from [`docs/architecture/06-roadmap-and-migration.md`](../docs/architecture/06-roadmap-and-migration.md).

## How to use it

1. Pick an unchecked row in [`checklist.md`](checklist.md), highest milestone first.
2. Find its issue on [`docs/BOARD.md`](../docs/BOARD.md). If it has none, ask the lead to file
   one: the feature section is the issue's acceptance criteria.
3. Build it the way [`architecture.md`](architecture.md) says (§4 where it goes, §5 definition
   of done), one issue per PR (CONTRIBUTING).
4. In the same PR, set the feature's **Status** in its file and in its Checklist row, then run
   `python plan/build_checklist.py`.

## Scope of this plan

This manual covers the **server** only. Each device driver (Modbus, Allen-Bradley, Siemens, …)
and each plug-in (Advanced Tags, Alarms & Events, DataLogger, IoT Gateway, Redundancy, …) has
its own manual. Add them as `features/16-…` onwards, with the same template.
