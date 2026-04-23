# research/

Scratch pad for ongoing research. Not organized, not promised to be up to date.

## Conventions

- `captures/` — HAR files, curl dumps, raw responses. **Never commit these.** They contain PHI and session cookies.
- `endpoints/` — reverse-engineered endpoint docs per portal feature (e.g., `labs.md`, `messages.md`, `refills.md`)
- `experiments/` — throwaway scripts, Jupyter notebooks, one-off Python files to try something out
- `reading/` — notes from articles, papers, or codebases we're reading (e.g., notes from Open Record's CLAUDE.md)

## Rules

1. **No PHI in committed files.** If you capture a real response, either redact it by hand before committing, or put it in `captures/` which is gitignored.
2. **Every endpoint doc has a "captured on" date.** Portals drift. Knowing when we observed a behavior helps us debug when it changes.
3. **Every experiment has a one-line outcome.** At the top of the file, write what you tried and what happened. Future-you will thank you.

## Starter endpoint doc template

Use this when you reverse-engineer a new Kaiser endpoint:

```markdown
# Endpoint: list_medications

**Captured on:** 2026-04-23
**By:** Test Patient
**Kaiser region:** NorCal

## Request

- Method: GET
- URL: https://healthy.kaiserpermanente.org/...
- Headers: ...
- Auth: session cookies from Ping OAuth

## Response

- Status: 200
- Content-Type: application/json
- Shape: ...

## Notes

- Anything weird, surprising, or worth remembering
- Observed behavior differences across account states
- Known quirks
```
