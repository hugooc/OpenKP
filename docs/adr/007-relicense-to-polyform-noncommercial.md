# ADR-007: Relicense from MIT to PolyForm Noncommercial 1.0.0

**Date:** 2026-05-27
**Status:** Active
**Authors:** Hugo Campos

## Context

OpenKP launched publicly on 2026-05-11 under the MIT license. The decision to use MIT traces back to ADR-001, where the rationale was:

> "Open Record's license is source-available and restricts commercial use and redistribution. We want OpenKP to be fully MIT so CAIHL researchers, other patient-advocacy projects, and future maintainers have no friction."

That reasoning still holds for the audiences we care about, individual patients, researchers, advocates, nonprofits, and government. What's changed is the maintainer's clarified intent about the audiences we don't care about. OpenKP exists because Kaiser's portal isn't built to make a patient's own record legible to them, and the maintainer wants the project to serve patients. The maintainer does not want OpenKP to become an ingredient in a paid product, a paid SaaS, or paid consulting work. The phrase the maintainer used when this surfaced: "this is my gift to the world. I don't want anyone to use what we built to make a buck."

MIT explicitly allows all of those commercial paths. That's the gap.

## Decision

**Relicense OpenKP from MIT to PolyForm Noncommercial 1.0.0**, effective 2026-05-27.

The PolyForm Noncommercial license is a modern, plain-English, software-native source-available license. Its key terms in plain language:

- **Free for any noncommercial purpose.** Personal use, research, experimentation, hobby projects, religious observance.
- **Free for noncommercial organizations.** Charitable organizations, educational institutions, public research bodies, public safety / health organizations, environmental protection orgs, and government institutions.
- **Source remains available.** Anyone can fork, inspect, modify, and redistribute under the same terms.
- **Commercial use requires a separate license** from the maintainer. "Commercial" here means "use intended for or directed toward commercial advantage or monetary compensation."

We chose PolyForm Noncommercial 1.0.0 over the alternatives because:

- **It's software-native.** Creative Commons licenses (CC BY-NC, CC BY-NC-SA) are widely recognized but Creative Commons themselves recommend against using CC for code — CC was designed for creative works and doesn't address patent grants, sublicensing, or other software-specific concerns. PolyForm was drafted by a panel of software lawyers specifically for code.
- **The "commercial" definition is clear.** PolyForm spells out what counts. CC's NC clause has been litigated in unpredictable ways across jurisdictions.
- **Patent grant is included.** PolyForm gives licensees a patent license alongside the copyright license. MIT had implied-license ambiguity here; CC has nothing.
- **GitHub recognizes it.** The license shows up correctly on the repo page and in API metadata.

## Consequences

**What changes for OpenKP users:**

- New cloners (after 2026-05-27) get PolyForm NC, not MIT.
- The legal text in `openkp/LICENSE` is replaced. The `Required Notice: Copyright (c) 2026 Hugo Campos` line is preserved at the top per PolyForm's notice requirement.
- Doc references to "MIT licensed" are updated everywhere (`README.md`, `CLAUDE.md`, `DESIGN.md`, `openkp/README.md`, `site/index.html`, `pyproject.toml`).

**What does NOT change:**

- The architecture, the tool surface, the local-first guarantees.
- Existing MIT clones taken between 2026-05-11 and 2026-05-26 stay under MIT for whoever has them. We can't claw back what's already been licensed under MIT. In practice, the public repo has no visible forks and no commercial deployments at the time of this decision, so the practical surface area of pre-relicense MIT snapshots is near zero. The real moat for any would-be commercial fork is keeping up with Kaiser's portal changes, which the relicense doesn't help with on either side.
- The maintainer can still grant separate commercial licenses to specific parties later. The "dual license" door remains open.

**What this supersedes:**

- ADR-001's mentions of MIT licensing (context item 3, consequences bullet). The build-fresh-vs-fork decision itself is unchanged.

**What this does not address:**

- A contributor agreement. Today there are no third-party code contributions, so this isn't urgent. If contributions arrive, a lightweight CLA or DCO inbound-equals-outbound model will be needed so contributors agree their contributions are licensed under PolyForm NC.
- A commercial-license offering. Not built. If a real commercial inquiry arrives, the maintainer will decide terms at that point. Until then, the noncommercial license is the only public license OpenKP offers.

## Operational record

The relicense was executed in a single commit on 2026-05-27 covering:

- `openkp/LICENSE` (replaced with PolyForm NC 1.0.0 text + `Required Notice` line)
- `README.md`, `CLAUDE.md`, `DESIGN.md`, `openkp/README.md` (MIT references replaced)
- `openkp/pyproject.toml` (`license` field updated)
- `site/index.html` (final-CTA copy updated)
- `docs/release-checklist.md` (post-launch item 6 updated to mark the LICENSE attribution work as complete via this relicense)
- `docs/adr/001-build-fresh-vs-fork-open-record.md` (status note added pointing here)
- `docs/adr/007-relicense-to-polyform-noncommercial.md` (this file)

Reference: <https://polyformproject.org/licenses/noncommercial/1.0.0/>
