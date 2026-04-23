# OpenKP

A patient-directed MCP server that bridges Claude and Kaiser Permanente's patient portal.

This is the workspace root. The actual code lives in `openkp/`.

## Layout

```
OpenKP/
├── README.md               ← you are here
├── DESIGN.md               ← vision, architecture, roadmap, principles
├── docs/
│   ├── recon/              ← research before we build
│   ├── adr/                ← architecture decision records
│   └── research/           ← scratch notes, endpoint captures, HAR files
├── openkp/                 ← the Python package
└── scripts/                ← workspace-level helper scripts
```

## Where to start

1. **Read the design doc.** `DESIGN.md` is the north star. It captures what we're building, why, and how. Read it before you write any code.
2. **Read the current phase.** Look at `DESIGN.md` Section 5 (Roadmap) to see which phase is active.
3. **Read relevant ADRs.** `docs/adr/` records every major architectural decision with rationale. If something surprises you, check whether there's an ADR explaining it.

## Working on the code

The Python package has its own README with setup steps. Start there:

```
cd openkp
cat README.md
```

## Principles

See `DESIGN.md` Section 2 for the full list. The three you should never forget:

1. **Local-first by default.** PHI never leaves the user's machine except on requests to Kaiser.
2. **Writes require confirmation.** Every state-changing tool must preview before acting.
3. **The user owns the keys.** Credentials live in the OS keychain. OpenKP never exfiltrates them.

## License

MIT. See `openkp/LICENSE` when created.
