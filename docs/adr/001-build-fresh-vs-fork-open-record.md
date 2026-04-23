# ADR-001: Build fresh, don't fork Open Record

**Date:** 2026-04-22
**Status:** Active
**Authors:** Test Patient

## Context

Open Record (Fan Pier Labs / Ryan Hughes) is the prior art for OpenKP. It does for vanilla Epic MyChart what we want to do for Kaiser. The natural question was whether to fork it and adapt, or implement OpenKP from scratch with Open Record as reference material.

Three considerations drove the decision:

1. **Scope mismatch.** Open Record is a full web app with Postgres, user accounts, BetterAuth, Google OAuth, a billing cap, a Gemini AI proxy, Next.js 15 front-end, Docker/Railway deployment, and an Expo mobile app. OpenKP is a single-user local MCP server. Most of Open Record's surface is overhead we don't need.

2. **Auth architecture mismatch.** Open Record targets standard Epic MyChart endpoints (`/MyChart/Authentication/Login/DoLogin`, CSRF form posts, direct TOTP validation). Kaiser sits behind PingFederate OAuth2 with device trust and WebAuthn. Open Record's 896-line `login.ts` is unusable on Kaiser. We'd be rewriting the auth layer anyway.

3. **License.** Open Record is source-available (not OSI open source). It prohibits commercial use, SaaS offerings, and competing products without written permission. A fork inherits these restrictions. We want OpenKP to be fully MIT so CAIHL researchers, other patient-advocacy projects, and future maintainers have no friction.

## Decision

Build OpenKP from scratch. Use Open Record as a reference for architecture patterns, tool surface, and domain modeling, but copy no code.

## Alternatives considered

**Fork Open Record's codebase.** Rejected because it would require spending weeks stripping features we don't need, fighting a TypeScript/Bun/Next.js stack we don't want, and accepting a source-available license we can't use.

**Extract just Open Record's `openclaw-plugin` (local variant).** Considered. It's the closest to what we want structurally. But it's ~200 lines of TypeScript that we'd rewrite in Python anyway, and it would still inherit the license restriction.

**Start with Open Record's TypeScript types only as a reference.** We're doing this informally. The types inform our pydantic models but we're not importing or copying them.

## Consequences

**We commit to:**
- Writing every line of OpenKP ourselves
- Maintaining parity with Open Record's tool surface by independent reimplementation
- MIT licensing, so OpenKP can be freely reused and redistributed
- Reading Open Record's `CLAUDE.md` and `docs/` as reference, not treating them as specs

**We give up:**
- Any head start from Open Record's code
- Feature parity on day one

**We gain:**
- A codebase we fully understand
- A license posture that fits the CAIHL mission
- Freedom to make architectural choices Open Record didn't (e.g., confirm-token pattern at the tool layer)

## Status

Active. Revisit only if Open Record relicenses to a permissive license, or if we hit a component complex enough to warrant attribution-based reuse.
