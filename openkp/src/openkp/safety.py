"""Safety scaffolding for write tools.

Two pieces:

1. **Audit log.** Every write tool records an entry to `<data_dir>/audit.log`
   before hitting Kaiser ("intent") and after the response comes back
   ("result" or "error"). JSONL format, one event per line. The patient (or a
   support-channel session) can replay the log to see what was written.

2. **Dry run.** `OPENKP_DRY_RUN=1` short-circuits any committing call without
   actually sending it to Kaiser. Idempotent prep reads (GET cart, GET wallet,
   etc.) still run so we catch shape mismatches before spending a real refill.

The "confirm-before-act" pattern is implemented by individual write tools
exposing a `confirm: bool = False` parameter, not as a helper here. Default
False returns a preview; True commits.

Defense in depth: `redact_for_audit` recursively scrubs known-sensitive keys
(payment tokens, card last-4, full street addresses, emails, phones) from any
dict/list before writing. Callers should still pass narrow payloads — the
redaction is a backstop, not an excuse to dump full request bodies.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DRY_RUN_ENV = "OPENKP_DRY_RUN"
AUDIT_LOG_FILENAME = "audit.log"

# Keys whose values are dropped at write time. Match is case-insensitive on the
# key name. Values are recursively dropped — if a key holds a nested dict, the
# whole subtree is replaced with the redaction sentinel.
_REDACT_KEYS = frozenset({
    "walletpaymenttoken",
    "creditcarddetails",
    "carddetails",
    "last4digit",
    "last_4_digit",
    "expirydate",
    "cardholdername",
    "cardtype",
    "emailid",
    "emailaddress",
    "email",
    "mobilenumber",
    "mobile_number",
    "phonenumber",
    "phone_number",
    "street1",
    "street2",
    "addressline1",
    "addressline2",
    "shippingaddress",
    "billingaddress",
    "placerdetails",
    "ssn",
})

_REDACTED_SENTINEL = "[redacted]"


def is_dry_run() -> bool:
    """True when OPENKP_DRY_RUN is set to a truthy value.

    Truthy: "1", "true", "yes", "on" (case-insensitive). Anything else, including
    unset, is False.
    """
    raw = os.getenv(DRY_RUN_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def redact_for_audit(value: Any) -> Any:
    """Recursively scrub known-sensitive keys.

    Returns a new structure. The input is not mutated. Non-dict, non-list
    inputs pass through unchanged. List items and dict values are walked.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                out[k] = _REDACTED_SENTINEL
            else:
                out[k] = redact_for_audit(v)
        return out
    if isinstance(value, list):
        return [redact_for_audit(item) for item in value]
    return value


def audit_log_event(
    data_dir: Path,
    *,
    tool: str,
    phase: str,
    fields: dict[str, Any] | None = None,
) -> None:
    """Append one JSONL event to <data_dir>/audit.log.

    Args:
      data_dir: The OpenKP data directory (typically ~/.openkp). Must exist.
      tool: Name of the calling MCP tool, e.g. "request_refill".
      phase: One of "intent" (about to call Kaiser), "result" (Kaiser
        responded successfully), or "error" (call failed). Free-form — these
        are conventions, not validated.
      fields: Additional event-specific data. Keys matching the redaction list
        will have their values replaced before the line is written.

    Failures are logged but never raised. Audit logging must not break the
    tool call it is recording.
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "phase": phase,
        "dry_run": is_dry_run(),
    }
    if fields:
        event.update(redact_for_audit(fields))

    path = data_dir / AUDIT_LOG_FILENAME
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except Exception as exc:
        logger.warning("audit log write failed (%s); event lost", type(exc).__name__)
