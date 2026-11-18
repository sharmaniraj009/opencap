Closes #

## What changed

## How it was tested
<!-- simulator tests, conformance vectors, acceptance suite, manual steps -->

## Checklist
- [ ] One issue, small PR; rebased on `main`
- [ ] Tests exercise behaviour (simulator / vectors / fault paths), `pytest -m "not acceptance"` green
- [ ] Acceptance suite run if the core, a driver or north changed
- [ ] New dependencies: licence named and linked at the pinned version (ADR-0007), `docs/architecture/oss-reuse.md` updated
- [ ] No real site names, real IPs (use 192.0.2.x), credentials or exports; `test_no_private_data.py` passes
- [ ] Read-only toward PLCs (ADR-0008): no write path, write services refused with a test
- [ ] Protocol findings in `docs/decisions.md`; contract changes in `docs/architecture/` or an ADR
- [ ] `needs-review` label set
