# Licence gate

Enforces [ADR-0007](../docs/adr/0007-oss-licence-policy.md) in CI (`.github/workflows/licenses.yml`).

- **Python:** `scripts/check_licenses.py` lists every distribution installed from `.[all,dev]` (`pip-licenses`, MIT)
  and compares it with `licenses/python-allow.toml`. Unknown or disallowed licences fail the job and name the
  package. `A OR B` passes if either side is allowed; `A AND B` needs both. Run locally:
  `pip install -e '.[all,dev]' pip-licenses && python scripts/check_licenses.py`.
- **Rust:** `cargo deny check licenses bans sources` with the root `deny.toml` (skipped with a notice when
  `engine/Cargo.toml` does not exist). `engine/deny.toml` remains the engine's own stricter gate.
- **Test-only tools** are listed under `[test_only]` in the allow file and checked against their own rules.

## Requesting an exception

1. Open a PR that adds the package to `[exceptions]` (Python) or `deny.toml` `exceptions` (Rust) with a one-line
   reason and the ADR-0007 clause that permits it (e.g. LGPL imported unmodified and replaceable).
2. Update `docs/architecture/oss-reuse.md` (licence, Verified column).
3. The technical lead must approve; strong copyleft or non-commercial licences also need owner approval and
   are not granted for the default build.
