# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-11-17
- Deciders: technical lead

## Context
opencap is built by several AI engineer sessions that cannot talk to each other. Decisions
that live only in a PR thread or in one session's context are lost to the others.
`docs/decisions.md` already records implementation findings (D-NNN), but not system-level
choices with their alternatives.

## Decision
Architecture decisions are recorded as ADRs in `docs/adr/` using the template in
`docs/adr/README.md`. A PR that makes or changes an architecture decision adds or updates an
ADR. Implementation findings stay in `docs/decisions.md`. Larger designs that need
discussion before a decision start as an RFC in `docs/rfc/NNNN-*.md` and end as an ADR.

## Consequences
- New engineers (sessions) read `docs/architecture/` and `docs/adr/` before starting.
- Reviews can point at an ADR instead of re-arguing a choice.

## Alternatives considered
Decisions in issues only (not versioned with the code); a wiki (not reviewable in PRs).

## Revisit when
Never; the format itself may evolve.
