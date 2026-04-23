# ADR-003: Local-first only, never a hosted service

**Date:** 2026-04-22
**Status:** Active
**Authors:** Test Patient

## Context

Open Record offers both a self-hosted path and a hosted multi-tenant service at openrecord.fanpierlabs.com. A hosted service lowers the bar for non-technical users. It also creates serious privacy, legal, and trust liabilities because the service operator holds every user's Kaiser credentials and all their PHI.

The question: should OpenKP ever be offered as a hosted service?

## Decision

No. OpenKP will only ever be a local-first tool. Users run it on their own machines. The project will never operate or endorse a hosted variant.

## Alternatives considered

**Offer both self-hosted and hosted, like Open Record.** Rejected. The two modes have different ethical postures and it's hard to maintain credibility on privacy principles while also running a hosted service.

**Offer a hosted service with strong crypto (client-side encryption, zero-knowledge storage).** Technically interesting. Still rejected. The moment OpenKP holds credentials or PHI in any form on any server we don't control at the user level, we've created a target, a legal exposure, and a trust hierarchy that defeats the CAIHL framing.

**Offer a managed Playwright service that users point their local MCP server at.** Rejected for the same reasons. Browser automation on a server we run is effectively a hosted service in a wig.

## Consequences

**We commit to:**
- Never operating a service that holds another user's Kaiser credentials
- Never operating a service that holds another user's PHI
- Building distribution paths (`.dxt`, browser extension) that preserve the local-first property
- Saying no to anyone who asks us to run a version of OpenKP for them

**We give up:**
- The easiest path to non-technical user adoption
- Any revenue model based on hosting
- A shortcut around the hard work of making local-first installable by ordinary users

**We gain:**
- Credibility on privacy principles
- Zero HIPAA exposure (we never touch PHI as an entity)
- A simpler legal posture (we don't have business associate agreements to maintain)
- Alignment between our stated CAIHL framing and our operational reality

## Status

Active. This is a hard architectural commitment. It should be superseded only by a very thoughtful ADR that accounts for all the consequences above, and only after extensive community discussion.
