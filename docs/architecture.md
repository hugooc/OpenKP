# OpenKP architecture at a glance

A single ASCII diagram that shows every software component involved in a
single tool call, from Hugo typing a question in Claude Desktop to the HTTPS
request landing at `healthy.kaiserpermanente.org`.

Useful when:

- Onboarding a new contributor (or re-onboarding future-you).
- Explaining to non-technical folks why OpenKP is local-only.
- Debugging where a failure happened in the stack (LLM turn vs. MCP
  dispatch vs. HTTP call vs. Kaiser-side).

For the "why" behind the architecture, see `DESIGN.md` and the ADRs. This
file is just the "what fits where."

## The stack

```
┌─────────────────────────────────────────────────────────────────────────┐
│ HUGO'S MAC                                                              │
│                                                                         │
│    ┌─────────────────────────────────────────────────────────┐          │
│    │  You                                                    │          │
│    │  "Summarize my messages from my cardiologist"           │          │
│    └──────────────────────────┬──────────────────────────────┘          │
│                               ▼                                         │
│    ┌─────────────────────────────────────────────────────────┐          │
│    │  Claude Desktop  (Cowork surface)                       │          │
│    │  - LLM chat UI                                          │          │
│    │  - MCP client: speaks JSON-RPC to local subprocesses    │          │
│    └────────┬───────────────────────────────┬────────────────┘          │
│             │                               │                           │
│   LLM turns │ HTTPS to Anthropic API        │ JSON-RPC over stdio       │
│  (Claude    │ (model reasoning,             │ tools/call: list_messages │
│   thinks)   │  tool-call decisions)         │                           │
│             │                               ▼                           │
│             │        ┌──────────────────────────────────────────┐       │
│             │        │  openkp Python subprocess                │       │
│             │        │  (spawned once at Claude Desktop launch) │       │
│             │        │                                          │       │
│             │        │    FastMCP (tool dispatcher)             │       │
│             │        │        │                                 │       │
│             │        │        ▼                                 │       │
│             │        │    mcp_server.py                         │       │
│             │        │    @mcp.tool() list_messages(...)        │       │
│             │        │        │                                 │       │
│             │        │        ▼                                 │       │
│             │        │    scrapers/messages.py                  │       │
│             │        │    fetch_messages()                      │       │
│             │        │        │                                 │       │
│             │        │        ▼                                 │       │
│             │        │    scrapers/request.py                   │       │
│             │        │    KaiserRequest  ← refreshes session    │       │
│             │        │        │           on 401 / Ping         │       │
│             │        │        ▼                                 │       │
│             │        │    scrapers/session.py                   │       │
│             │        │    SessionStore                          │       │
│             │        │        │  reads:                         │       │
│             │        │        │   • ~/.openkp/session.json      │       │
│             │        │        │     (session cookies, UA)       │       │
│             │        │        │   • macOS Keychain              │       │
│             │        │        │     (kp.org username+password)  │       │
│             │        │        ▼                                 │       │
│             │        │    httpx.AsyncClient                     │       │
│             │        │    (cookies + headers)                   │       │
│             │        └─────────────────┬────────────────────────┘       │
│             │                          │                                │
│             │                          │ HTTPS                          │
│             ▼                          ▼                                │
└─────────────┬──────────────────────────┬───────────────────────────────┘
              │                          │
              ▼                          ▼
    ┌─────────────────┐      ┌────────────────────────────────────────┐
    │ Anthropic API   │      │ healthy.kaiserpermanente.org           │
    │ (Claude model)  │      │                                        │
    └─────────────────┘      │ Endpoints OpenKP hits:                 │
                             │   /mycare/v1.0/user                    │
                             │   /mychartcn/Home/CSRFToken            │
                             │   /mychartcn/Clinical/CareTeam/Load    │
                             │   /mychartcn/app/communication-center  │
                             │   /mychartcn/api/conversations/        │
                             │     GetConversationList                │
                             │     GetConversationDetails             │
                             │   /mychartcn/keepalive.asp             │
                             └────────────────────────────────────────┘
```

## Five things the diagram shows

**1. Two separate network paths leave the Mac.** Claude Desktop talks to
Anthropic for LLM reasoning. openkp talks directly to Kaiser. They never
meet on the wire. Anthropic never sees a raw Kaiser HTTP request and
Kaiser never sees an LLM prompt.

**2. openkp runs entirely on your Mac as a subprocess.** No hosted service.
Credentials live in macOS Keychain, session cookies in `~/.openkp/`, code on
your disk. If you unplugged the Mac's network mid-reasoning, Kaiser calls
would fail but nothing on a server somewhere is holding PHI. This is
ADR-003 in code form.

**3. Kaiser does not know OpenKP exists.** From
`healthy.kaiserpermanente.org`'s perspective, it receives HTTP requests
that look indistinguishable from your browser. Same cookies, same
user-agent, same header contracts. That is intentional. It is why we
reverse-engineer the exact request shape from HAR captures instead of
guessing.

**4. The LLM does see PHI.** Tool results (messages, demographics, PCP,
insurance plans) flow back through openkp to Claude Desktop to Anthropic
so the LLM can reason about them. That is fundamental to the assistant
being useful. But the raw HTTP exchange with Kaiser stays private to your
Mac. Anthropic sees the parsed, stripped, pydantic-shaped result. Kaiser
sees the HTTP wire, nothing else.

**5. Why restart matters after code edits.** The "openkp Python subprocess"
box is spawned once at Claude Desktop launch and stays alive for the whole
session with Python modules cached in memory. Editing code on disk does not
update what the subprocess has loaded. Killing Claude Desktop (Cmd+Q) kills
that subprocess. Relaunch respawns it fresh and reads the new code.

## Layer responsibilities (inside the openkp subprocess)

The four-layer scraper architecture from `CLAUDE.md`:

| Layer | File | Responsibility |
|---|---|---|
| Auth | `scrapers/auth.py` | Playwright-driven interactive login. First run only. |
| Session | `scrapers/session.py` | Persists cookies to `~/.openkp/session.json`. Silent reuse. |
| Request | `scrapers/request.py` | One `KaiserRequest` wrapper used by every scraper. Handles 401 / Ping redirects, retries once with a fresh session. |
| Tools | `scrapers/profile.py`, `scrapers/messages.py`, ... | One module per endpoint family. Pydantic models. Parser resilience. |

The MCP surface (`mcp_server.py`) is a thin layer on top that calls into
the scrapers and hands results back to Claude as `dict`s.

## See also

- `DESIGN.md` §1-3 for the vision and principles behind the architecture.
- `docs/adr/` for the decisions that shaped it (ADR-001 through ADR-006).
- `docs/research/endpoints/` for per-endpoint request and response contracts.
- `docs/recon/` for the session-by-session story of how this got built.
