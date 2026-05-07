# Document Center endpoints

Source HAR: `docs/research/captures/kp-capture-various-with-phi.har`, 2026-05-06.
**Most JSON response bodies for these calls were stripped by Chrome's HAR
export.** URLs, methods, request bodies, and response sizes are intact.
Future work needs a fresh capture with bodies preserved.

## Summary

The MyChart "Document Center" surface (`/mychartcn/app/document-center/my-documents`)
is a **separate data surface from messages and labs**. Letters, releases,
signed forms, and other scanned reports live here. None of these are surfaced
as MCP tools yet. This addresses the CLAUDE.md loose end: "spot-check whether
MyChart Documents holds reports OpenKP doesn't reach" — yes, it does.

| Feature | Endpoint | Status |
| --- | --- | --- |
| Documents requiring signature | `POST /mychartcn/api/documents/viewer/LoadDocumentsToSign` body `{}` | 🔴 Returned 22 B (empty) — Hugo had nothing to sign. |
| "Other" documents (letters, releases, scanned reports) | `POST /mychartcn/api/documents/viewer/LoadOtherDocuments` body `{"isInitialLoad":true}` | 🔴 Returned **15.8 KB**. Real data. Bodies stripped — re-capture needed. |
| Get document detail (existing infra, reused) | `POST /mychartcn/api/documents/viewer/GetDocumentDetails` | ✅ Already mapped (used by `download_lab_result_pdf` etc.). |
| Document binary download | `GET /mychartcn/Documents/ViewDocument/DownloadOrStream` | ✅ Already mapped. |
| Document Center BFF — alternate documents list | `GET /kp/prod/mycare/ddm/getdocumentsbff/v1/documents?esb-envlbl=PROD` | 🔴 Returned **22 KB**. Different host (`apims.kaiserpermanente.org`), different auth. |
| Document Center BFF — PDF binary | `GET /kp/prod/mycare/ddm/getdocumentsbff/v1/documents/pdf/<encryptedId>?esb-envlbl=PROD` | 🔴 Returned 200, binary, 0 B in HAR (binary stripped). |

Page route: `/mychartcn/app/document-center/my-documents` (legacy MyChart) and
`/secure/my-documents` / `/northern-california/secure/my-documents-myc` (the
AEM-styled landing pages).

## Two parallel "documents" surfaces

Both endpoints fired during the same page load. They appear to overlap but
aren't identical:

- **`LoadOtherDocuments`** (legacy `/mychartcn/api/`) — per the existing
  pattern (CSRF, page nonce, MyChart cookie auth). Same family as
  `GetConversationDetails`, `LoadHealthIssuesData`, etc.
- **`getdocumentsbff/v1/documents`** (new ddm BFF) — different host, different
  auth (see below). The "ddm" prefix likely stands for *Document Distribution
  Management* (Epic / KP internal naming).

Hypothesis: legacy returns Epic-native documents, BFF returns docs from the
broader Kaiser DMS (cross-system, non-Epic origins). Confirming requires
bodies. **For tool design we should plan for one unified `list_documents`
that calls both and dedupes.**

## Auth contract for the ddm BFF

The HAR shows zero `X-` headers and only analytics cookies on the GET — but
the call returned 22 KB of data, so Kaiser session cookies must be flowing
even if the HAR export elided them. The simpler explanation is HAR
sanitization (HttpOnly cookies dropped). Treat as "session cookies + Origin /
Referer only" for now and confirm with a fresh capture.

If that's actually the contract, this BFF is the lightest auth surface we've
seen — useful as a fallback if other approaches need to break.

## Record Download (Federal V/D/T surface)

Adjacent but distinct from the Document Center: the "View / Download /
Transmit" record-download surface required by Meaningful Use / 21st Century
Cures.

| Endpoint | HTTP | Body | Notes |
| --- | --- | --- | --- |
| `POST /mychartcn/api/record-download/getvdtsettings` | POST | `{}` | 463 B. Returns the user's V/D/T preferences and capabilities. |
| `POST /mychartcn/api/record-download/LoadSingleVisits` | POST | `{"count":5,"endDat":"","fromInst":""}` | 8.4 KB. Lists visits available for record export. |

Page route: `/mychartcn/app/record-download`. Likely paginated via
`count` / `endDat` / `fromInst`.

Tool candidate: `download_visit_ccda(csn)` would emit a federally-mandated
C-CDA XML or PDF for one visit. Useful for users who want to share their
record with a non-KP provider. Not v1.

## Tool candidates (when bodies are mapped)

- `list_documents()` — unified across legacy + ddm BFF.
- `download_document(document_id)` — file save to `~/.openkp/downloads/`,
  same pattern as `download_lab_result_pdf`.
- `list_documents_to_sign()` — surface pending consent/release docs.
- `download_visit_ccda(csn)` — C-CDA / PDF export for record portability.

## Open questions

1. Body shape of `LoadOtherDocuments` — is it `{DataList: [...]}` like
   `LoadHealthIssuesData`, or a different envelope?
2. Does the ddm BFF list overlap with `LoadOtherDocuments`, or are they
   complementary?
3. Auth contract on the ddm BFF — confirm with a fresh capture whether the
   apparent "cookies only" pattern is real or a HAR-export artifact.
4. Does `LoadDocumentsToSign` include consent forms patients sometimes need
   for procedures (HIPAA releases, surgical consents)?
