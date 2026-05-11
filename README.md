# OpenKP

[![CI](https://github.com/hugooc/OpenKP/actions/workflows/ci.yml/badge.svg)](https://github.com/hugooc/OpenKP/actions/workflows/ci.yml)

A patient-directed MCP server that bridges Claude and **Kaiser Permanente Northern California's** patient portal — letting you act on your own medical record, with your own credentials, on your own Mac.

```
You ─►  Claude Desktop ─►  OpenKP (local) ─►  kp.org
```

The kind of question OpenKP makes possible — one a patient portal structurally cannot answer:

> *"Read every visit note from the last two years. Find every instance where I raised a concern, asked a question, or pushed back. How was it documented? Look for patterns in how my engagement gets characterized."*

Kaiser's portal shows you plans, orders, and results. It doesn't show you how *you* show up in the chart. OpenKP can.

This is **critical AI health literacy** in practice — patient-directed AI on patient-owned data, surfacing what institutional systems are not built to make legible. Background: ["Critical AI Health Literacy as Liberation Technology"](https://nam.edu/perspectives/critical-ai-health-literacy-as-liberation-technology-a-new-skill-for-patient-empowerment) (NAM Perspectives) and [aipatients.org](https://aipatients.org).

OpenKP exposes 17 read tools and 2 write tools covering appointments, labs, messages, medications, problems, allergies, demographics, visit notes, and after-visit summaries. Other questions it can handle:

- *"How many appointments did I have last year, split by virtual vs in-person?"*
- *"Which lab values have drifted in the last 18 months?"*
- *"Compare what my cardiologist and primary-care doctor have each written about my condition over the last three years."*
- *"Refill my blood pressure medication."*

Everything stays on your machine. There is no OpenKP server, no shared database, no remote credential store. Every Kaiser request is made by you, as you, using the same web session you'd get logging into kp.org by hand.

## Who this is for

Technically curious **Kaiser Permanente Northern California** members who:

- Have Claude Desktop installed (or are willing to install it).
- Are comfortable running a few terminal commands, or have Claude Code on hand to walk them through it.
- Want a richer, more agentic interface to their own health record than the kp.org website or Kaiser app provide.

It is **not** a packaged consumer product. The path from "I want to install this" to "it works" goes through a Python venv, an MCP config file, and a one-time interactive browser login. If that sentence felt opaque, OpenKP isn't for you yet — but the pieces that would make it consumer-grade (single-click install, GUI credential entry, signed binary) are sketched in `DESIGN.md` §5 (Phase 4.5) and waiting on real demand.

## Regional support

**OpenKP is only tested against Kaiser's Northern California region.** Kaiser operates 8 regions; they share a portal front door but differ in region codes, pharmacy backends, and field shapes. SoCal / Northwest / Hawaii / etc. members will hit breakage on at least the medication and refill tools. Issues and HAR captures from other regions are welcome.

## Get started

Install steps live in [`openkp/README.md`](openkp/README.md). It walks through venv setup, credentials, the Claude Desktop config block, and a first-things-to-try list.

If you have Claude Code installed, the easiest path is to clone this repo and ask Claude Code to walk you through the install — `openkp/README.md` is structured for exactly that flow.

## What's inside

```
OpenKP/
├── README.md                    ← you are here
├── DESIGN.md                    ← vision, architecture, roadmap, principles
├── docs/
│   ├── adr/                     ← architecture decision records (ADR-001 onward)
│   ├── research/endpoints/      ← per-endpoint Kaiser API maps
│   └── release-checklist.md     ← pre-public-release todos
├── openkp/                      ← the Python package + tests + install README
└── scripts/
    └── setup-dev.sh             ← one-shot venv + Playwright setup
```

## Principles

The full list lives in `DESIGN.md` §2. The three that matter most:

1. **Local-first by default.** PHI never leaves your machine except on direct requests to Kaiser.
2. **Writes require confirmation.** Every state-changing tool previews before acting and refuses to commit without an explicit `confirm=True`.
3. **You own the keys.** Credentials live in your OS keychain. OpenKP never uploads them, never logs them, and never shares them between accounts.

## Status

Phase 2 (read-only) is closed. Phase 3 (writes) is in progress. As of 2026-05-04: 22 MCP tools registered, 527 tests passing, run with `cd openkp && .venv/bin/pytest -q`. Per-tool status (live-verified, preview-only, deferred) is documented in `openkp/README.md`.

## License

MIT. See [`openkp/LICENSE`](openkp/LICENSE).

## Credits

Inspired by [Open Record](https://github.com/Fan-Pier-Labs/openrecord) by Ryan Hughes / Fan Pier Labs (vanilla Epic MyChart). OpenKP implements the same idea against Kaiser's Ping-fronted portal, with independently written code.
