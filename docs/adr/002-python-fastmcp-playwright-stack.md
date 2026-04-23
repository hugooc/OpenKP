# ADR-002: Python + FastMCP + Playwright as the core stack

**Date:** 2026-04-22
**Status:** Active
**Authors:** Test Patient

## Context

OpenKP is a small, single-maintainer project. Stack choice affects iteration speed, packaging options, and how easy it is for collaborators to contribute.

Three layers need a stack decision:

1. **Language and MCP framework.**
2. **Browser automation** (needed for Kaiser's Ping OAuth login).
3. **HTTP client** for authenticated portal requests after login.

## Decision

- **Language:** Python 3.11+
- **MCP framework:** FastMCP (the `mcp` package from Anthropic)
- **Browser automation:** Playwright
- **HTTP client:** httpx (async)
- **HTML parsing:** BeautifulSoup4 + lxml
- **Data modeling:** Pydantic 2
- **Credential storage:** `keyring` (OS keychain wrapper)

## Alternatives considered

**TypeScript + Bun + MCP SDK.** Matches Open Record's stack. Would ease lifting architectural patterns (if we did lift them). Rejected because Python has stronger library support for HTTP/HTML scraping and the CAIHL-adjacent research community writes more Python than TypeScript.

**TypeScript + Node + MCP SDK.** Same as above, minus Bun's weirdness. Same reasoning applies.

**Rust.** Single-binary distribution is appealing, and it would handle the eventual `.dxt` packaging elegantly. Rejected for now because iteration speed matters more than distribution polish at this stage. Revisit if/when we need to ship a sealed binary.

**Pure `requests` or `urllib` instead of Playwright.** Considered for the auth layer because it's simpler. Rejected because Kaiser's login uses PingFederate with likely device fingerprinting, risk scoring, and WebAuthn. Pure HTTP would be fragile and would miss MFA pathways. Playwright pays a reliability tax upfront in exchange for a working login flow.

**Selenium instead of Playwright.** Rejected. Playwright has better defaults, cleaner async, and active modern maintenance.

**Bring your own LLM proxy (OpenAI, Gemini, local model).** Out of scope. OpenKP is the MCP server. The LLM client is Claude, managed by the user via Claude Desktop.

## Consequences

**We commit to:**
- Python 3.11+ as the minimum version (match statements, exception groups, better asyncio)
- Playwright Chromium as a shipped dependency (adds ~300 MB on first run)
- Pydantic for all cross-layer data shapes

**We give up:**
- Zero-dependency distribution. OpenKP will always need Python + Playwright installed.
- Direct code portability from Open Record.

**We gain:**
- Fast iteration for a solo developer
- Mature scraping ecosystem
- Reasonable path to `.dxt` packaging via `pyinstaller` + bundled Chromium

## Status

Active. Revisit if:
- Packaging for non-technical users becomes a blocker and Rust/Go would unblock it
- Kaiser's auth evolves to require something Playwright can't handle
