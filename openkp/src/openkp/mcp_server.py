"""OpenKP MCP server.

Exposes Kaiser Permanente patient-portal actions as MCP tools so Claude can
read and (eventually) write to your medical record.

Current state: skeleton. Only the `ping` and `whoami` tools work. Real tools
arrive once the auth layer is live.

Run directly:
    python -m openkp.mcp_server

Or via the installed script:
    openkp

Connect from Claude Desktop by adding this to your MCP config:

    {
      "mcpServers": {
        "openkp": {
          "command": "/path/to/venv/bin/openkp"
        }
      }
    }
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from openkp import __version__
from openkp.config import load_config
from openkp.scrapers.allergies import fetch_allergies
from openkp.scrapers.labs import (
    download_lab_result_pdf as _download_lab_result_pdf,
    fetch_lab_result,
    fetch_lab_results,
)
from openkp.scrapers.medications import fetch_medications
from openkp.scrapers.messages import (
    download_message_attachment as _download_message_attachment,
    fetch_message,
    fetch_messages,
)
from openkp.scrapers.problems import fetch_problems
from openkp.scrapers.profile import fetch_profile
from openkp.scrapers.refill import (
    fetch_refill_order,
    request_refill as _request_refill,
)
from openkp.scrapers.request import KaiserRequest
from openkp.scrapers.session import SessionStore

logger = logging.getLogger("openkp")

mcp = FastMCP("OpenKP")

_session_store: SessionStore | None = None


def _get_session_store() -> SessionStore:
    """Lazy singleton. Built on first authenticated tool call, reused thereafter."""
    global _session_store
    if _session_store is None:
        cfg = load_config()
        _session_store = SessionStore(cfg.data_dir, cfg.username, cfg.password)
    return _session_store


@mcp.tool()
def ping() -> str:
    """Smoke test. Returns 'pong' if the MCP server is alive."""
    return "pong"


@mcp.tool()
def whoami() -> dict:
    """Return the configured Kaiser username and data directory. Does NOT return the password."""
    cfg = load_config()
    return {
        "username": cfg.username,
        "data_dir": str(cfg.data_dir),
        "version": __version__,
    }


@mcp.tool()
async def session_check() -> dict:
    """Verify end-to-end auth: can we reach an authenticated Kaiser endpoint?

    Triggers the full auth path on first call: silent session refresh if the
    persistent profile is still good, otherwise a visible Chromium window for
    interactive login (including any MFA). Subsequent calls are silent.

    Returns a summary of the probe, never PHI. Use `get_profile` (when it
    lands) for actual user data.
    """
    store = _get_session_store()
    client = KaiserRequest(store)

    probe_path = "/mychartcn/keepalive.asp?cnt=1"
    probe_headers = {
        "Accept": "*/*",
        "Referer": "https://healthy.kaiserpermanente.org/mychartcn/Home?lang=en-US",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        response = await client.get(probe_path, headers=probe_headers)
    except Exception as exc:
        logger.exception("session_check failed")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    session = await store.get_session()
    return {
        "status": "alive" if response.status_code == 200 else "unexpected",
        "probe_url": probe_path,
        "probe_status": response.status_code,
        "cookie_count": len(session.cookies),
    }


@mcp.tool()
async def get_profile() -> dict:
    """Return the patient's profile: demographics, contact info, insurance plans.

    Source: Kaiser's `/mycare/v1.0/user` endpoint, called with the pharmacy
    consumer identity. See ADR-006 for why.

    Returns a dict shaped like the `Profile` pydantic model in
    `openkp.scrapers.profile`, including PCP (from `CareTeam/Load`) and
    `emergency_contacts` (from `Demographics/Relationships/GetRelationshipList`,
    which also covers DPOAHC healthcare agents). Either nested fetch failing
    leaves its slot null / empty rather than failing the whole call.

    See `docs/research/endpoints/profile.md` and `emergency_contacts.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    profile = await fetch_profile(client)
    return profile.model_dump()


@mcp.tool()
async def list_messages(
    folder: str = "inbox",
    search: str | None = None,
    before_iso: str | None = None,
    limit: int = 50,
    deep_search: bool = False,
    max_pages: int = 30,
) -> list[dict]:
    """List message threads from the Kaiser message center.

    Args:
      folder: Which folder to list. One of "inbox", "archive", "bookmarked",
        "automated", "appointments". Defaults to "inbox".
      search: Optional search string. Kaiser matches against subject, body,
        and sender.
      before_iso: Pagination cursor. Pass the ISO timestamp of the oldest
        thread from a previous page to fetch older results.
      limit: Max threads to return in single-page mode. Clamped to 50
        (Kaiser's per-page max). Ignored when `deep_search=True`.
      deep_search: If True, walk pagination across the full inbox history.
        Use this when searching for older threads — Kaiser's `searchQuery`
        only matches within one loaded page (≈50 threads), so a default
        search misses anything older. Costs one round trip per page until
        Kaiser reports no more results or `max_pages` is hit.
      max_pages: Hard cap on pages walked in deep-search mode. Default 30
        (≈1500 threads worth of history, sufficient for most accounts).

    Returns a list of thread summaries, each shaped like the `MessageThread`
    pydantic model in `openkp.scrapers.messages`. The `id` field is the
    thread handle you pass to `read_message`.

    See `docs/research/endpoints/messages.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    threads = await fetch_messages(
        client,
        folder=folder,
        search=search,
        before_iso=before_iso,
        limit=limit,
        deep_search=deep_search,
        max_pages=max_pages,
    )
    return [t.model_dump() for t in threads]


@mcp.tool()
async def read_message(thread_id: str) -> dict | None:
    """Read a single message thread in full, including every message's body.

    Args:
      thread_id: The `id` field from a `list_messages` result.

    Returns a dict shaped like the `MessageThreadDetail` pydantic model. The
    `messages` array is ordered most-recent-first per Kaiser's convention.
    Message bodies are HTML-stripped to plain text. Each message's
    `attachments[]` carries `dcs_id`, `name`, and `file_extension` — pass
    `dcs_id` to `download_message_attachment` to fetch the binary. Returns
    `None` if the thread cannot be found.

    See `docs/research/endpoints/messages.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    thread = await fetch_message(client, thread_id)
    return thread.model_dump() if thread else None


@mcp.tool()
async def download_message_attachment(
    dcs_id: str,
    file_extension: str = "PDF",
    display_name: str | None = None,
    organization_id: str = "",
) -> dict:
    """Download a message attachment to disk and return its local path.

    Args:
      dcs_id: The `dcs_id` field from a `read_message` attachment.
      file_extension: Kaiser's extension marker (e.g. "PDF", "JPG"). Pass
        through from the attachment metadata.
      display_name: Optional override for the saved filename. If omitted,
        Kaiser's display name is used.
      organization_id: Cross-region attachment marker. Empty string is the
        common same-region case.

    Saved under `~/.openkp/downloads/` (same directory as lab PDFs). Returns
    a dict shaped like `MessageAttachmentDownload`, with `status` being:
      - "downloaded" — saved, `path` holds the local filesystem path.
      - "error"      — `reason` holds a short explanation.

    The bytes are NOT returned through MCP — too big for Claude's context.
    Hand the path to a separate PDF / image reading tool, or open locally.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    outcome = await _download_message_attachment(
        client,
        dcs_id=dcs_id,
        file_extension=file_extension,
        display_name=display_name,
        organization_id=organization_id,
    )
    return outcome.model_dump()


@mcp.tool()
async def list_lab_results(
    search: str = "",
    limit: int = 50,
    include_all_types: bool = False,
) -> list[dict]:
    """List recent lab result orders (newest first, LAB type by default).

    Args:
      search: Optional search string. Kaiser matches against order names and
        (empirically) other fields. Empty = no filter.
      limit: Max orders to return. Clamped to 200 (Kaiser's ceiling).
      include_all_types: If False (default), only LAB-type results. If True,
        also include IMAGING (radiology, ECG, cardiac device checks) and
        OTHER (pathology, transcriptions).

    Returns a list of summaries shaped like the `LabResult` pydantic model in
    `openkp.scrapers.labs`. The `order_key` is the handle you pass to
    `read_lab_result` or `download_lab_result_pdf` for a specific result.

    See `docs/research/endpoints/labs.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    results = await fetch_lab_results(
        client,
        search=search,
        limit=limit,
        include_all_types=include_all_types,
    )
    return [r.model_dump() for r in results]


@mcp.tool()
async def read_lab_result(order_key: str) -> dict | None:
    """Read one result in full: components, values, reference ranges, narrative.

    Args:
      order_key: The `order_key` field from a `list_lab_results` result.

    For LAB-type results, the `components` array carries each individual
    measurement with `value`, `numeric_value`, `units`, `reference_range`, and
    `is_abnormal`. For IMAGING / OTHER results, the `narrative`, `impression`,
    and `result_note` fields carry the prose text (HTML-stripped).

    `has_pdf` tells you whether `download_lab_result_pdf` will return a file.
    Returns `None` if the order cannot be found.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    result = await fetch_lab_result(client, order_key)
    return result.model_dump() if result else None


@mcp.tool()
async def download_lab_result_pdf(order_key: str) -> dict:
    """Download the kp.org-generated PDF report for one result, save to disk.

    Args:
      order_key: The `order_key` field from a `list_lab_results` result.

    The PDF is saved under `~/.openkp/downloads/`. Returns a dict shaped like
    the `LabPdfDownload` pydantic model, with `status` being one of:
      - "downloaded" — PDF saved, `path` holds the local filesystem path.
      - "generation_in_progress" — Kaiser is building the PDF on demand. Wait
        ~30 seconds and call this tool again with the same order_key.
      - "no_pdf_available" — Kaiser does not have a PDF for this order (typical
        for simple LAB results). Don't retry, the doc will not appear.
      - "error" — Something went wrong, `reason` holds a short explanation.

    For large PDFs (cardiac device interrogation reports, imaging studies),
    the path is what you'd hand to a separate PDF-reading tool or open locally.
    The bytes are NOT returned through MCP — too big for Claude's context.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    outcome = await _download_lab_result_pdf(client, order_key)
    return outcome.model_dump()


@mcp.tool()
async def list_medications(filter: str = "all") -> dict:
    """List active and recent prescriptions (medication list).

    Args:
      filter: "all" (default) returns the full medication list — both
        currently-orderable and not. "fillable" narrows to Rx that Kaiser
        flags as currently refillable per its own timing / inventory /
        regulatory rules.

    Returns a dict shaped like the `MedicationsResponse` pydantic model in
    `openkp.scrapers.medications`, with a `medications` array plus summary
    counts (`total_count`, `refillable_count`, `recent_refillable_count`)
    and the upstream `status_code` / `status_details`.

    Each medication carries: name + brand_name, sig (`instructions`),
    `prescriber`, `rx_number`, `days_supply`, `refills_remaining`, `copay`,
    `last_refill_date`, `next_fill_date`, `next_fill_eligible_date`, NDC,
    plus boolean state for mailable / first fill / new prescription /
    auto-refill on / eligible / PRN / compound. `is_currently_orderable`
    indicates whether a refill can be placed right now;
    `refill_blocked_reason` carries Kaiser's reason code when blocked.

    Source: Kaiser's pharmacy BFF microservices on
    `apims.kaiserpermanente.org`. This is the first OpenKP tool to use the
    BFF — see `docs/research/endpoints/medications.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    response = await fetch_medications(client, filter=filter)
    return response.model_dump()


@mcp.tool()
async def list_problems() -> dict:
    """List active health issues from the patient's problem list.

    The "problem list" is what KP shows on the Health Summary page and the
    dedicated Health Issues page — active diagnoses and ongoing health
    conditions, name + date noted. Useful as the anchor for "what's going
    on with my X" questions and for pairing with the medication list.

    Returns a dict shaped like the `ProblemsResponse` pydantic model in
    `openkp.scrapers.problems`, with a `problems` array plus `total_count`.

    Each problem carries: `id`, `name`, `date_noted` (display string,
    typically `"M/D/YYYY"`), `action_code` (raw int — `0` is the only value
    observed and indicates active), `is_read_only`, and `comments` (clinician
    free text, often null).

    No ICD codes, severity, or resolved-date in this surface — KP does not
    expose those to the patient view. See `docs/research/endpoints/problems.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    response = await fetch_problems(client)
    return response.model_dump()


@mcp.tool()
async def list_allergies() -> dict:
    """List recorded drug, food, and environmental allergies.

    The most common state is "no known allergies" — empty `allergies` list
    plus `status: "no_known_allergies"`. That is a real, valid medical state,
    not an error.

    Returns a dict shaped like the `AllergiesResponse` pydantic model in
    `openkp.scrapers.allergies`, with `allergies`, `total_count`, `status`
    (one of `"no_known_allergies"`, `"recorded"`, or null when the status
    code is unrecognized), and `status_code` (the raw `AllergiesStatus` int
    from Kaiser).

    Each allergy carries: `id`, `name`, `date_noted`, `action_code`,
    `is_read_only`, `comments`, `reactions` (list of strings), `severity`.

    Per-item field names are inferred from the structurally-identical
    problems endpoint and Epic conventions — no populated allergy has been
    observed live yet. See `docs/research/endpoints/allergies.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    response = await fetch_allergies(client)
    return response.model_dump()


@mcp.tool()
async def request_refill(medication_id: str, confirm: bool = False) -> dict:
    """Request a mail-order refill for one prescription. Two-call confirm pattern.

    **v1 supports mail-order only.** Local pickup is deferred to v2 — see
    `docs/research/endpoints/refill.md` for scope and rationale.

    Args:
      medication_id: The `rx_number` from a `list_medications` result. Must be
        an Rx Kaiser flags as currently fillable; non-fillable Rxs return a
        preview with `can_confirm=False` and a warnings list explaining why.
      confirm: When False (default), perform read-only checks and return a
        `RefillPreview` dict so the user (or Claude on the user's behalf) can
        review medication name, estimated copay, delivery destination, and
        payment-on-file status BEFORE committing. When True, run the full
        cart -> eligibility -> placeorderMail commit pipeline and return an
        `OrderConfirmation` dict.

    Confirm-before-act pattern:
      Call once with `confirm=False` to preview. Read the preview. If the
      `can_confirm` field is True and you want to proceed, call again with
      `confirm=True`. The preview will refuse to commit (raise) if any blocker
      is present (`can_confirm=False`).

    Safety:
      - Every commit-path call writes to `~/.openkp/audit.log` (JSONL) before
        and after the Kaiser request.
      - `OPENKP_DRY_RUN=1` in the environment short-circuits the final POST and
        returns a synthetic success `OrderConfirmation` with `dry_run=True`.
        Use this to smoke-test before spending a real refill.
      - Card details (last-4, expiry, wallet token) are NEVER returned through
        the MCP surface and are redacted from the audit log. KP's saved
        payment method is used per KP's own "if a copay is required, this
        payment method will be charged" policy.

    Returns a dict shaped like either `RefillPreview` (when `confirm=False`)
    or `OrderConfirmation` (when `confirm=True`).

    Source: Kaiser's pharmacy BFF microservices on `apims.kaiserpermanente.org`.
    See `docs/research/endpoints/refill.md` for the endpoint map.
    """
    store = _get_session_store()
    cfg = load_config()
    client = KaiserRequest(store)
    result = await _request_refill(
        client,
        medication_id,
        confirm=confirm,
        data_dir=cfg.data_dir,
    )
    return result.model_dump()


@mcp.tool()
async def track_refill_order(order_number: str) -> dict:
    """Look up the status of a refill order placed via `request_refill`.

    Args:
      order_number: The `order_number` field from a `request_refill(confirm=True)`
        OrderConfirmation. Internal Kaiser reference, not the user-facing
        digits a patient might see in printed paperwork.

    Returns a dict shaped like the `RefillOrder` pydantic model in
    `openkp.scrapers.refill`:

    - `order_number`, `order_type` (e.g. "MAIL"), `placer_name`
    - `order_status` (API code: "INPROGRESS", "SHIPPED", "DELIVERED") and
      `status_label` ("In Progress", etc. — the UI-friendly mirror)
    - `placed_at`, `committed_at` (ISO timestamps)
    - `rx_list[]` — each Rx on the order with `rx_status`, `tracking_id`,
      `quantity`, `drug_name`, `pharmacy_phone`, `image_url`, etc.
    - `shipping_address` — full mailing address as Kaiser stored it for the order
    - `payment[]` — card last-4 / type / expiry only (no token, no holder name)
    - `tracking_ids[]` — derived convenience list pulled from `rx_list`,
      empty until at least one Rx ships

    Read-only. No audit log entries.

    Source: `GET /rx-order-management-bff/v1/orderDetails`.
    See `docs/research/endpoints/refill.md`.
    """
    store = _get_session_store()
    client = KaiserRequest(store)
    result = await fetch_refill_order(client, order_number)
    return result.model_dump()


# --- TODO: remaining Phase 2 read tools ----------------------------------------
# - list_immunizations()
# - list_visits(limit: int = 10)
#
# Phase 3 writes (request_refill ✅ shipped 2026-04-25 — mail-only, see refill.md):
# Phase 3 reads (track_refill_order ✅ shipped 2026-04-27, see refill.md):
# - send_message(to, subject, body)
# - reply_to_message(message_id, body)
# - book_appointment(provider_id, slot_id)
# ---------------------------------------------------------------------------------


def main() -> None:
    """CLI entry point. Called by the `openkp` script defined in pyproject.toml."""
    cfg = load_config()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("OpenKP %s starting MCP server (stdio)", __version__)
    mcp.run()


if __name__ == "__main__":
    main()
