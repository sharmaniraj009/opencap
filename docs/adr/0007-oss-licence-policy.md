# 0007. OSS reuse and licence policy

- Status: Accepted (the copyleft-plugin rule needs owner confirmation, see board)
- Date: 2026-11-17
- Deciders: technical lead; owner for anything that changes the project licence

## Context
The owner wants to reuse existing OSS wherever licences allow, so less is built from
scratch. opencap is Apache-2.0 (`LICENSE`, `pyproject.toml`). Several
of the best industrial stacks are GPL (lib60870, libiec61850) or non-commercial
(some DNP3 libraries). A wrong dependency could force a licence change on everyone who
ships opencap.

## Decision
| Licence class | Examples | Rule |
|---|---|---|
| Permissive | MIT, BSD-2/3, Apache-2.0, ISC, Zlib, Unicode, public domain | Allowed |
| Weak copyleft, file level | MPL-2.0, EPL-2.0 (for unmodified files) | Allowed unmodified; modifications to those files are published under their licence |
| Weak copyleft, library | LGPL-2.1/3.0 | Allowed only dynamically linked or imported, unmodified, replaceable (Python imports; shared libraries). Not statically linked into the Rust binary |
| Strong copyleft | GPL-2.0/3.0, AGPL | Never in the default build. Allowed only as a **separately distributed out-of-process plugin** under its own licence, and only with owner approval. Allowed as a test-only tool that is installed in CI and never vendored or shipped |
| Non-commercial, source-available, custom | "non-commercial", BSL, SSPL, Commons Clause | Never, not even test-only if it is vendored |
| Unknown / no licence | — | Never |

- Every new dependency PR states the licence and links its `LICENSE` file at the pinned
  version; the reviewer checks it; `docs/architecture/oss-reuse.md` is updated.
- CI enforces it: `cargo-deny` for `engine/`, an allow-list check over installed Python
  distributions (licence-gate issue on the board).
- Vendored files keep their headers and are listed in `NOTICE`.

## Consequences
Some mature C stacks are only reachable as optional plugins. For S7comm, MC, FINS we build
native Rust implementations of the read subset, tested against OSS simulators.

## Alternatives considered
Allow GPL dependencies and relicense under GPL (blocks embedding by integrators, owner
decision we do not presume).

## Revisit when
The owner decides on the GPL-plugin question, or a dependency changes licence.
