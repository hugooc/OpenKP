# Architecture Decision Records

One file per significant decision. Numbered sequentially. Never deleted, only superseded.

## Format

Every ADR has the same five sections:

1. **Context.** What situation prompted the decision?
2. **Decision.** What did we decide?
3. **Alternatives considered.** What else did we look at, and why didn't we pick it?
4. **Consequences.** What does this commit us to? What does it rule out?
5. **Status.** Active, superseded by ADR-NNN, or deprecated.

## Index

- [ADR-001](001-build-fresh-vs-fork-open-record.md) — Build fresh, don't fork Open Record
- [ADR-002](002-python-fastmcp-playwright-stack.md) — Python + FastMCP + Playwright as the core stack
- [ADR-003](003-local-first-no-hosted-service.md) — Local-first only, never a hosted service
- [ADR-004](004-writes-require-confirm-token.md) — Writes require a confirmation token at the tool layer
- [ADR-005](005-interactive-first-run-auth.md) — Interactive first-run auth, silent subsequent runs
- [ADR-006](006-user-endpoint-piggyback.md) — Piggyback on the pharmacy consumer identity for `/mycare/v1.0/user`

## When to write a new ADR

Write one whenever you make a decision you'd have to re-explain six months from now. Examples:

- Choosing one library over another
- Picking an architectural pattern
- Defining a policy (e.g., "we never log PHI")
- Saying no to a feature for good reasons

You don't need an ADR for small choices like variable naming.
