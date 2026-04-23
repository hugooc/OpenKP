# docs/

Non-code artifacts that inform the project.

## Structure

- `recon/` — research done before building. Session recons, feasibility analyses, competitive teardowns. These are point-in-time documents, not living specs.
- `adr/` — Architecture Decision Records. One file per significant decision. Numbered. Never deleted, only superseded.
- `research/` — ongoing research. Endpoint captures, HAR files, API notes, experiments. The scratch pad.

## Writing conventions

- Markdown, no em dashes, no semicolons
- Top of each file: title, date, author, status (draft / active / superseded)
- Date in ISO format (2026-04-22)
- If a doc supersedes another, link it in the header of the old one

## What belongs here vs in the code

- **Here:** human reasoning, decisions, research, historical context
- **In the code:** machine-executable behavior

When in doubt, if it's explaining *why*, it's probably a doc. If it's explaining *what happens when you run it*, it's probably code comments or the README.
