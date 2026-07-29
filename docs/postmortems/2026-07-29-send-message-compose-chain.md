# send_message had never worked — 2026-07-29

**Symptom:** `send_message(confirm=True)` failed instantly with

```
GetComposeId response missing composeId (response keys: str)
```

**Duration:** two months. First recorded failure 2026-05-27, fixed 2026-07-29.

**Impact:** the tool had never succeeded once. `~/.openkp/audit.log` contained
no `phase="result"` line in its entire history. Preview (`confirm=False`)
worked the whole time, which made it look like a narrow bug in the commit
path rather than a chain that had never run end to end.

**Fixed in:** #7, #6, #8. Message sent live 2026-07-29 15:10:05 UTC.

---

## What was actually wrong

Three separate defects, discovered one at a time because each one masked the
next.

### 1. GetComposeId returns a bare JSON string

The entire 130-byte response body is:

```
"WP-<128 chars>"
```

There is no object. `response.json()` returns a `str`, so looking up a
`composeId` key finds nothing. Not double-encoded either — a second
`json.loads()` raises `JSONDecodeError`.

### 2. GetViewers was never called

`VIEWERS_PATH` was defined in `messages.py` and referenced nowhere. The
browser calls it between GetComposeId and SaveDraft to get the patient's own
`wprId`. Without it, `viewers[0].wprId` went out as `""`.

The function that built it said so in its own docstring: *"falling back to
empty — Kaiser's server-side validation will reject a clearly-bad viewers
array."* It was right.

### 3. The recipient object carried 12 keys

`_build_recipient_payload` echoed the whole recipient row on the theory that
unknown Kaiser fields should survive the round trip. The browser sends
exactly five and drops the rest.

Kaiser rejected 2 and 3 together with `{"error": 2}`. Because both were fixed
in the same change, **which one it objected to is still unknown.**

---

## Root cause

All three trace to one thing: **the send chain's response shapes were never
captured.** They were inferred from HAR `content.size` values.

`messages.md` had documented this honestly the whole time:

> **Response-body capture gap.** Chrome DevTools' HAR exporter has a small
> circular buffer. The 690 KB `GetConversationList` response that fires when
> the inbox loads evicts every other body.

So the request side was verified against live Kaiser, and the response side
was a guess. `126 bytes` became `{"composeId": "WP-…"}` because that is what
126 bytes of JSON *looks like* it should be.

Then the tests mocked the guess:

```python
httpx.Response(200, json={"composeId": "CID-1"}),   # GetComposeId
```

A fully green suite certified a shape nobody had ever seen. **The tests could
never have caught this**, because they asserted the same assumption the code
made.

The clue was in the captures all along. Three HARs recorded GetComposeId
responses of 130, 130, and 126 bytes — consistent with a variable-length bare
token, not a fixed wrapper. Nobody had read `content.size` as evidence.

---

## Why it took so long to find

**The error message pointed the wrong way.**

```python
f"(response keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__})"
```

On a non-dict this prints the *type name* under a label that says *keys*. So
the message read `response keys: str`, which parses naturally as "there is a
key called str" and sends you hunting for a missing field on an object that
never existed. The payload was the token.

Now `_describe_payload` prints `type: str` and, for dicts, `type: dict, keys: [...]`.

**Four debugging cycles were spent on a stale process.** The MCP server runs
as a subprocess of Claude Desktop. Python binds modules at import, so code
changes need a full app restart, and `Cmd+Q` did not always take:

```
Claude.app  pid 13621  started Jul 24 11:22   <- never quit
  openkp    pid 13676  started Jul 24 11:22   <- serving stale code
```

Three separate "it still fails" reports were the same pre-patch process
answering each time. **Always confirm the server process is newer than the
edit** before trusting a live result:

```
ps -ax -o pid,lstart,command | grep venv/bin/openkp
```

**Test output contaminated the debug log.** A temporary probe wrote to
`~/.openkp/debug.log` from `_post_get_compose_id`, which `pytest` also
exercises. A local test run produced log lines identical in shape to live
traffic — `{"composeId":"CID-123"}` — that were briefly read as the real
Kaiser response. Any live-capture probe must skip itself under pytest:

```python
if "PYTEST_CURRENT_TEST" in os.environ:
    return
```

---

## Verified response shapes

Captured live 2026-07-29. Full detail in `docs/research/endpoints/messages.md`.

| Endpoint | Response |
| --- | --- |
| `GetComposeId` | Bare string `"WP-<~125>"`. 130 bytes = 128 token chars + 2 quotes |
| `GetViewers` | `{"viewers":[{"wprId","name","isSelf",…}], …}`. 323 bytes. Name field is `name`, not `displayName` |
| `SaveDraft` | `{"conversationId":"WP-…","error":0}`. 115 bytes. An object, **not** a bare string |
| Recipient key set | Exactly `displayName`, `userId`, `poolId`, `providerId`, `departmentId`. `poolId` and `departmentId` are empty in the browser too |

A prediction worth recording as wrong: SaveDraft was expected to be a bare
string by analogy with GetComposeId, on the byte arithmetic. It is not. The
parser survived because `_extract_token` accepts both forms, not because the
reasoning was right.

---

## Kaiser returns errors as HTTP 200

`{"error": 2}` arrived with a 200 status, so `raise_for_status()` sailed
past it. `error: 0` accompanies success, so the field is always present on
these endpoints.

`_raise_for_kaiser_error` now checks it on SaveDraft and Send. **Assume any
Epic/MyChart endpoint can fail with a 200.**

| code | meaning |
| --- | --- |
| `0` | success |
| `2` | rejected. Seen with an empty `wprId` and a 12-key recipient. Which one it objected to is not established |

---

## Collateral findings

Two problems surfaced while getting CI green that had nothing to do with
messaging.

**The install was broken for new users.** `mcp[cli]>=1.2.0` had no upper
bound and mcp 2.0.0 had shipped, dropping `mcp.server.fastmcp` — which
`mcp_server.py` imports at module level. Anyone following the README got a
package that could not import. Local venvs held 1.27.0 from before the
release, so nothing looked wrong on the author's machine. Only a clean CI
build exposed it. Now capped at `<2.0.0`; supporting 2.x means a port.

**Unbounded linters broke every PR.** `ruff>=0.5.0` resolved to 0.16.0, whose
new rules flagged 87 findings in untouched files. `main` had been green a
month earlier with no code change in between. Both linters are now pinned to
a major line.

The shared lesson: **an unbounded dependency is a build that changes when you
are not looking.** `pydantic`, `httpx`, and the rest are still unbounded.

---

## PHI found in the public repo

A sweep during this session found two real values that had been published:

- `WISHLIST.md` named a real treating provider in a use-case sentence.
  Combined with the author's name in LICENSE and the ADRs, that discloses a
  care relationship.
- `messages.md` pasted a real ~85-char document ID out of a HAR, with the
  line above naming the uploaded file.

Both were removed from the working tree. **Both remain in git history**
(`0def0c8`, `6fc9d1b`) — removing them there means a history rewrite, which
breaks every existing fork and clone.

`scripts/phi-scan.py` now checks for this class of mistake:

```
python3 scripts/phi-scan.py            # working tree
python3 scripts/phi-scan.py --history  # every commit
```

Both leaks read as harmless illustrative detail when written. That is the
failure mode — not carelessness, but a real value doing duty as an example.

---

## Checklist for the next endpoint

1. **Never document a response shape you have not seen.** Mark it
   `(inferred)` and treat it as a known gap, not a fact.
2. **Read `content.size` when the body is missing.** Three captures at
   130/130/126 bytes said "variable-length bare token" two months early.
3. **Never mock an inferred shape without a comment saying so.** A green
   suite over a guessed fixture is worse than no test — it manufactures
   confidence.
4. **Include the payload type in parse errors, never just field names.**
5. **Verify the server process is newer than your edit** before believing a
   live result.
6. **Gate debug probes on `PYTEST_CURRENT_TEST`** so test runs cannot
   masquerade as live capture.
7. **Check for `{"error": N}` on 200 responses.**
8. **Run `scripts/phi-scan.py` before publishing anything.**
