"""Refill scraper.

One MCP tool surfaces from this module:

- `request_refill(medication_id, confirm=False)` — preview a mail-order refill
  for one prescription, then commit it on a follow-up call with `confirm=True`.

v1 ships **mail-order only** (`deliveryMethod: "M"`). Local-pickup support is
deferred to v2 and requires its own HAR capture before the body schema can be
trusted. See `docs/research/endpoints/refill.md` for the full request/response
maps and the rationale for the mail-only scope.

Architectural notes:

- This is the first OpenKP write tool. It reuses the `apims.kaiserpermanente.org`
  BFF host and session-cookie crossover that `medications.py` proved out.
- The "confirm-before-act" pattern is implemented as the *return shape*: a
  `confirm=False` call performs read-only reconnaissance (medication lookup,
  wallet check) and returns a `RefillPreview`. A `confirm=True` call assumes
  the caller has reviewed the preview and proceeds with the 3-POST commit
  pipeline (cart/prescription -> rxdeliveryeligibility -> placeorderMail).
- All commit-path activity is recorded in `<data_dir>/audit.log` via the
  `safety` module before and after the Kaiser call. `OPENKP_DRY_RUN=1`
  short-circuits the final POST and synthesizes a fake `OrderConfirmation`.

Docs: `docs/research/endpoints/refill.md`
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from openkp.safety import audit_log_event, is_dry_run
from openkp.scrapers.medications import (
    BFF_CLIENT_ID,
    BFF_HOST,
    BFF_ORIGIN,
    BFF_REGION_SENTINEL,
    FILTER_ALL,
    _bff_headers,
    _fetch_user_guid,
)
from openkp.scrapers.profile import Profile, fetch_demographics
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

# --- BFF endpoint paths ---

CART_PATH = "/kp/mycare/pharmacy-microservices/pharmacy-center-kpweb-bff/v1/cart"
ADDRESS_PATH = "/kp/mycare/pharmacy-microservices/pharmacy-center-kpweb-bff/v1/address"
SHIPPING_DATE_PATH = "/kp/mycare/pharmacy-microservices/pharmacy-center-kpweb-bff/v1/shippingDate"
WALLET_PATH = "/kp/mycare/payment-sts/paymentsts-bff-walletbff/v1/walletV3"
CART_PRESCRIPTION_PATH = "/kp/mycare/pharmacy-microservices/pharmacy-center-kpweb-bff/v1/cart/prescription"
ELIGIBILITY_PATH = "/kp/mycare/pharmacy-microservices/pharmacy-center-kpweb-bff/v1/rxdeliveryeligibility"
PLACE_ORDER_MAIL_PATH = "/kp/mycare/pharmacy-microservices/rx-place-order-bff/v1/placeorderMail"

CART_URL = f"{BFF_HOST}{CART_PATH}"
ADDRESS_URL = f"{BFF_HOST}{ADDRESS_PATH}"
SHIPPING_DATE_URL = f"{BFF_HOST}{SHIPPING_DATE_PATH}"
WALLET_URL = f"{BFF_HOST}{WALLET_PATH}"
CART_PRESCRIPTION_URL = f"{BFF_HOST}{CART_PRESCRIPTION_PATH}"
ELIGIBILITY_URL = f"{BFF_HOST}{ELIGIBILITY_PATH}"
PLACE_ORDER_MAIL_URL = f"{BFF_HOST}{PLACE_ORDER_MAIL_PATH}"

# v1 hardcodes mail-order. Local pickup is deferred to v2 — see refill.md.
DELIVERY_METHOD_MAIL = "M"
DELIVERY_METHOD_LABEL = "Mail order"

# Insurance plans observed in Hugo's capture. KP echoes these into both
# request bodies. Sending verbatim is the safest path until we see another
# member's data shape.
DEFAULT_INSURANCE_PLANS = ["COMMERCIAL", "MFAP", "CASH"]

# Source-application tags echoed back by Kaiser's frontend. They show up in
# the placeorderMail response too, so they're load-bearing — send verbatim.
SOURCE_APPLICATION = "WPP"
SOURCE_CHANNEL = "WEB"

# `executionContext.statusCode` and per-Rx `rxOrderResponseCode` of "000"
# both mean OK. See refill.md "Response shape".
SUCCESS_STATUS_CODE = "000"


# --- models ---


class CardOnFile(BaseModel):
    """The patient's saved payment method, fetched from /walletV3.

    We do NOT echo `last_4_digit`, `expiry_date`, `card_holder_name`, or
    `wallet_payment_token` from the MCP tool surface — those go straight into
    the placeorderMail body and never appear in `RefillPreview` or
    `OrderConfirmation`. This model exists only as an internal carrier.
    """

    card_holder_first: str | None = None
    card_holder_last: str | None = None
    card_holder_middle: str | None = None
    expiry_date: str | None = None      # "MM/YY"
    last_4_digit: str | None = None
    card_type: str | None = None        # e.g. "AM" for Amex
    wallet_payment_token: str | None = None
    billing_zip: str | None = None


class RefillPreview(BaseModel):
    """What `request_refill(confirm=False)` returns. Read-only reconnaissance."""

    medication_id: str
    name: str | None = None
    brand_name: str | None = None
    instructions: str | None = None
    estimated_copay: float | None = None
    days_supply: int | None = None
    refills_remaining: int | None = None
    next_fill_eligible_date: str | None = None
    delivery_method: str = DELIVERY_METHOD_MAIL
    delivery_method_label: str = DELIVERY_METHOD_LABEL
    shipping_city_state_zip: str | None = None  # Coarse — we don't echo street1
    payment_method_on_file: bool = False
    can_confirm: bool = False
    warnings: list[str] = Field(default_factory=list)


class OrderConfirmation(BaseModel):
    """What `request_refill(confirm=True)` returns when the order is placed."""

    order_number: str | None = None      # KP's internal reference, not user-facing
    placed_at: str | None = None         # KP's "MM/DD/YYYY HH:MM:SS" string passed through
    delivery_method: str = DELIVERY_METHOD_MAIL
    delivery_method_label: str = DELIVERY_METHOD_LABEL
    estimated_delivery_window: str | None = None
    rx_number: str | None = None
    name: str | None = None
    execution_status_code: str | None = None
    execution_status_details: str | None = None
    per_rx_response_code: str | None = None
    succeeded: bool = False
    dry_run: bool = False


# --- public ---


async def request_refill(
    client: KaiserRequest,
    medication_id: str,
    *,
    confirm: bool = False,
    data_dir: Path | None = None,
) -> RefillPreview | OrderConfirmation:
    """Preview or commit a mail-order refill for one prescription.

    Args:
      client: Authenticated KaiserRequest.
      medication_id: The `rx_number` from a `list_medications` result. This
        must be a currently-fillable Rx; nonFillable Rxs return a preview
        with `can_confirm=False`.
      confirm: When False (default), perform read-only checks and return a
        `RefillPreview` so the user (or Claude) can review before committing.
        When True, run the full cart -> eligibility -> placeorderMail commit
        pipeline and return an `OrderConfirmation`.
      data_dir: OpenKP data directory for the audit log. Required when
        `confirm=True`. Ignored on preview.

    Mail-order only in v1 (`deliveryMethod: "M"`). See refill.md for v2 plans.
    """
    if not isinstance(medication_id, str) or not medication_id.strip():
        raise ValueError("medication_id must be a non-empty string")
    medication_id = medication_id.strip()

    guid = await _fetch_user_guid(client)
    rx_row, bucket = await _find_rx(client, guid, medication_id)
    profile = await fetch_demographics(client)
    wallet = await _fetch_wallet(client, guid)

    preview = _build_preview(medication_id, rx_row, bucket, profile, wallet)

    if not confirm:
        return preview

    if not preview.can_confirm:
        # Refuse to commit when the preview surfaced a blocking warning.
        raise ValueError(
            "Cannot confirm refill: preview reports it is not committable. "
            f"Reasons: {', '.join(preview.warnings) or 'unknown'}."
        )
    if data_dir is None:
        raise ValueError("data_dir is required when confirm=True")
    if rx_row is None or wallet is None:
        # Should be unreachable when can_confirm=True, but defend anyway.
        raise RuntimeError("Internal: confirmable preview but missing Rx or wallet")

    return await _commit_refill(
        client=client,
        guid=guid,
        rx_row=rx_row,
        profile=profile,
        wallet=wallet,
        medication_id=medication_id,
        data_dir=data_dir,
    )


# --- preview construction ---


def _build_preview(
    medication_id: str,
    rx_row: dict[str, Any] | None,
    bucket: str | None,
    profile: Profile,
    wallet: CardOnFile | None,
) -> RefillPreview:
    """Compose a `RefillPreview` from the prep-call results.

    Sets `can_confirm=True` only when every blocker is clear: Rx exists in the
    `fillable` bucket, has a mailable flag, KP says it's refill-eligible, and
    a payment method is on file. When the Rx is found in the `nonFillable`
    bucket (because it's currently being processed, or the patient has no
    refills remaining, etc.), surface specific Kaiser-side reasons so the
    user understands *why* it can't be refilled — not just "not found".
    """
    warnings: list[str] = []

    if rx_row is None:
        warnings.append(
            "medication not found in current prescriptions — verify the medication_id "
            "matches the rx_number from list_medications"
        )
        return RefillPreview(
            medication_id=medication_id,
            payment_method_on_file=wallet is not None and bool(wallet.wallet_payment_token),
            shipping_city_state_zip=_short_address_label(profile),
            can_confirm=False,
            warnings=warnings,
        )

    afc = rx_row.get("afcInfo") or {}
    name = _str_or_none(rx_row.get("medicineName"))
    brand_name = _str_or_none(rx_row.get("commonBrandName"))
    instructions = _str_or_none(rx_row.get("consumerInstructions")) or _str_or_none(afc.get("sigText"))
    estimated_copay = _estimated_mail_copay(rx_row)
    days_supply = _int_or_none(afc.get("dispenseddayssupply"))
    refills_remaining = _str_to_int(rx_row.get("refillsRemaining"))
    next_fill_eligible = _str_or_none(afc.get("nextFillEligibleDate"))

    # Bucket-driven: if Kaiser put the Rx in nonFillable, the surface-level
    # `refillEligible` check below is redundant — but the buckets carry no
    # reason on their own, so we still inspect the row to explain why.
    if bucket == "nonFillable":
        warnings.append("Kaiser is not currently accepting a refill for this Rx")
    if not _bool_truthy(rx_row.get("mailable")):
        warnings.append("prescription is not mailable (mail-order is the only v1 delivery method)")
    # `refillEligible` can lag reality. Verified 2026-04-25 chlorthalidone test:
    # Kaiser kept refillEligible=false even though the Rx was in fillable[],
    # next_fill_eligible_date had passed, and the kp.org pharmacy UI accepted
    # the order without complaint. Treat bucket=fillable + past-eligible-date
    # as the authoritative "yes" signal and skip the stale-flag warning.
    if not rx_row.get("refillEligible") and not _eligible_date_has_passed(next_fill_eligible, bucket):
        if next_fill_eligible:
            warnings.append(
                f"Kaiser flags this Rx as not currently refill-eligible "
                f"(next fill eligible: {next_fill_eligible})"
            )
        else:
            warnings.append("Kaiser flags this Rx as not currently refill-eligible")
    if rx_row.get("rarStatus") and _str_or_none(rx_row.get("rarCodeKey")):
        warnings.append(f"refill blocked: {rx_row['rarCodeKey']}")
    if (refills_remaining is not None and refills_remaining <= 0) and bucket == "nonFillable":
        warnings.append("no refills remaining on this prescription")
    if wallet is None or not wallet.wallet_payment_token:
        warnings.append("no payment method on file")

    if profile.guid is None:
        warnings.append("no GUID on profile")

    # De-duplicate warnings while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            deduped.append(w)

    return RefillPreview(
        medication_id=medication_id,
        name=name,
        brand_name=brand_name,
        instructions=instructions,
        estimated_copay=estimated_copay,
        days_supply=days_supply,
        refills_remaining=refills_remaining,
        next_fill_eligible_date=next_fill_eligible,
        shipping_city_state_zip=_short_address_label(profile),
        payment_method_on_file=wallet is not None and bool(wallet.wallet_payment_token),
        can_confirm=not deduped,
        warnings=deduped,
    )


# --- commit pipeline ---


async def _commit_refill(
    *,
    client: KaiserRequest,
    guid: str,
    rx_row: dict[str, Any],
    profile: Profile,
    wallet: CardOnFile,
    medication_id: str,
    data_dir: Path,
) -> OrderConfirmation:
    """Run the 3-POST sequence (or short-circuit under dry-run).

    Sequence:
      1. POST cart/prescription      — adds the Rx to KP's server-side cart
      2. POST rxdeliveryeligibility  — validates delivery to patient zip
      3. POST placeorderMail         — commits the order

    Each step audits before and after via `audit_log_event`. Under dry-run,
    none of the three POSTs run; we synthesize a success `OrderConfirmation`
    and audit it as such. Idempotent prep GETs (already done by the caller in
    `request_refill`) DO run under dry-run because they can't hurt anything.
    """
    rx_number = _str_or_none((rx_row.get("afcInfo") or {}).get("rxnbr")) or medication_id
    rx_name = _str_or_none(rx_row.get("medicineName"))
    transaction_ref = str(uuid.uuid4())

    audit_fields = {
        "medication_id": medication_id,
        "rx_number": rx_number,
        "delivery_method": DELIVERY_METHOD_MAIL,
        "transaction_control_reference": transaction_ref,
    }
    audit_log_event(data_dir, tool="request_refill", phase="intent", fields=audit_fields)

    if is_dry_run():
        confirmation = OrderConfirmation(
            order_number=f"dry-run-{transaction_ref}",
            placed_at=datetime.now(timezone.utc).isoformat(),
            delivery_method=DELIVERY_METHOD_MAIL,
            delivery_method_label=DELIVERY_METHOD_LABEL,
            rx_number=rx_number,
            name=rx_name,
            execution_status_code=SUCCESS_STATUS_CODE,
            execution_status_details="Dry run — no request sent to Kaiser",
            per_rx_response_code=SUCCESS_STATUS_CODE,
            succeeded=True,
            dry_run=True,
        )
        audit_log_event(
            data_dir,
            tool="request_refill",
            phase="result",
            fields={**audit_fields, "order_number": confirmation.order_number, "succeeded": True},
        )
        return confirmation

    try:
        await _post_cart_prescription(client, guid, rx_row)
        await _post_eligibility(client, guid, rx_row, profile)
        delivery_window = await _fetch_shipping_date(client, profile)
        place_response = await _post_place_order_mail(
            client=client,
            guid=guid,
            rx_row=rx_row,
            profile=profile,
            wallet=wallet,
            transaction_ref=transaction_ref,
            delivery_window=delivery_window,
        )
    except Exception as exc:
        audit_log_event(
            data_dir,
            tool="request_refill",
            phase="error",
            fields={**audit_fields, "error_type": type(exc).__name__, "error_msg": str(exc)[:200]},
        )
        raise

    confirmation = _parse_place_order_response(
        payload=place_response,
        rx_number=rx_number,
        rx_name=rx_name,
        delivery_window=delivery_window,
    )

    audit_log_event(
        data_dir,
        tool="request_refill",
        phase="result",
        fields={
            **audit_fields,
            "order_number": confirmation.order_number,
            "execution_status_code": confirmation.execution_status_code,
            "per_rx_response_code": confirmation.per_rx_response_code,
            "succeeded": confirmation.succeeded,
        },
    )
    return confirmation


# --- prep GETs ---


async def _find_rx(
    client: KaiserRequest,
    guid: str,
    rx_number: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Hit rxDetails?prescriptions_filter=all, find the matching raw row.

    Looks in both the `fillable[]` and `nonFillable[]` buckets. Returns
    `(row, bucket)` where bucket is `"fillable"`, `"nonFillable"`, or None
    if the rx_number isn't found in either. The bucket distinction is
    load-bearing for the preview's warning logic — a recently-refilled Rx
    shows up in nonFillable rather than disappearing entirely.

    We pull the raw payload (not the parsed `Medication`) because the request
    bodies for cart/prescription, rxdeliveryeligibility, and placeorderMail
    all need to echo Kaiser's full prescription metadata verbatim.
    """
    from openkp.scrapers.medications import RX_DETAILS_URL

    response = await client.get(
        RX_DETAILS_URL,
        params={"prescriptions_filter": FILTER_ALL},
        headers=_bff_headers(guid, FILTER_ALL),
    )
    response.raise_for_status()
    payload = response.json() if response.content else {}
    if not isinstance(payload, dict):
        return None, None

    prescriptions = payload.get("prescriptions") or {}
    for bucket_name in ("fillable", "nonFillable"):
        rows = prescriptions.get(bucket_name) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            afc = row.get("afcInfo") or {}
            if (
                _str_or_none(afc.get("rxnbr")) == rx_number
                or _str_or_none(row.get("rxNumber")) == rx_number
            ):
                return row, bucket_name
    return None, None


async def _fetch_wallet(client: KaiserRequest, guid: str) -> CardOnFile | None:
    """Fetch the patient's saved payment method from /walletV3.

    walletV3 response shape (verified 2026-04-25 from kp-refill-2.har):

        {
          "card": [
            {
              "accountType": "creditcard",
              "billingAddress": "",                  # empty STRING, not a sub-object
              "cardType": "American Express",        # full name, NOT "AM" code
              "ccName": "Test Patient",
              "ccNumber": "2000",                    # last-4
              "expDate": "2802",                     # YYYYMM format
              "firstName": "...",
              "lastName": "...",
              "middleInitial": "",
              "nickName": "AMEX",
              "paymentToken": "...",                 # ← what we need
              "zipCode": "90210"
            },
            null
          ],
          "check": [],
          "defaultToken": "...",
          "profileId": "...",
          "walletKey": ""
        }

    placeorderMail expects `creditCardDetails` with field names like
    `last4Digit`, `expiryDate` ("MM/YY"), `cardType` ("AM"-style code),
    `cardHolderName.{firstName,lastName,middleName}`, `billingAddress.zipCode`,
    `walletPaymentToken`. We bridge the two shapes here.

    Returns None when no token-bearing card is found; the preview surfaces
    that as "no payment method on file".
    """
    response = await client.get(WALLET_URL, headers=_wallet_headers())
    if response.status_code >= 400:
        logger.warning("walletV3 returned %s; treating as no card on file", response.status_code)
        return None
    if not response.content:
        logger.warning("walletV3 returned empty body")
        return None
    try:
        payload = response.json()
    except Exception as exc:
        logger.warning("walletV3 JSON decode failed: %s", type(exc).__name__)
        return None

    card_dict = _find_card_with_token(payload)
    if card_dict is None:
        logger.warning(
            "walletV3 returned content but no payment token found. shape=%s",
            _shape_summary(payload),
        )
        return None

    return CardOnFile(
        card_holder_first=_str_or_none(card_dict.get("firstName")),
        card_holder_last=_str_or_none(card_dict.get("lastName")),
        card_holder_middle=_str_or_none(card_dict.get("middleInitial")) or "",
        expiry_date=_format_expiry_date(card_dict.get("expDate")),
        last_4_digit=_str_or_none(card_dict.get("ccNumber")),
        card_type=_card_type_code(card_dict.get("cardType")),
        wallet_payment_token=(
            _str_or_none(card_dict.get("paymentToken"))
            or _str_or_none(card_dict.get("walletPaymentToken"))
            or _str_or_none(card_dict.get("token"))
        ),
        billing_zip=_str_or_none(card_dict.get("zipCode")),
    )


async def _fetch_shipping_date(client: KaiserRequest, profile: Profile) -> str | None:
    """Fetch the estimated delivery window and format the deliveryStatus string.

    /shippingDate response (verified 2026-04-25 from kp-refill-2.har):

        {"region": "CN", "state": "CA", "zipCode": "90210",
         "from": "04/29/2026", "to": "05/01/2026",
         "fromDays": 3, "toDays": 5}

    KP's frontend builds the deliveryStatus string from from/to:
    "Estimated to arrive between Wednesday, April 29 and Friday, May 1. "
    (note the trailing space and no year). We do the same here.

    This field is cosmetic — placeorderMail still places the order if it's
    empty. On any error or parse failure, return None.
    """
    address = _primary_address(profile)
    zip_code = address.postal_code if address else None
    state = address.state if address else None

    headers = _bff_base_headers()
    if zip_code:
        headers["X-ZipCode"] = zip_code
    if state:
        headers["X-state"] = state
    headers["x-OrderPlacedDate"] = datetime.now().strftime("%m/%d/%Y")
    headers["x-benefitsIndicator-feature"] = "true"
    headers["x-cost-feature"] = "true"

    try:
        response = await client.get(SHIPPING_DATE_URL, headers=headers)
    except Exception as exc:
        logger.warning("shippingDate fetch failed (%s); skipping", type(exc).__name__)
        return None
    if response.status_code >= 400 or not response.content:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    return _compose_delivery_window(payload)


# --- commit POSTs ---


async def _post_cart_prescription(
    client: KaiserRequest,
    guid: str,
    rx_row: dict[str, Any],
) -> dict[str, Any]:
    """POST cart/prescription. Adds the Rx to Kaiser's server-side cart."""
    body = {
        "identifier": {
            "id": "",
            "type": "",
            "removeRDOptions": False,
            "isM3PMem": False,
            "InsurancePlans": DEFAULT_INSURANCE_PLANS,
            "applyFeature": True,
            "isTeenUser": False,
            "isOrderByRx": False,
        },
        "cartItems": {guid: []},
        "rxNumber": json.dumps({**rx_row, "isAddedToCart": True}),
    }
    response = await client.post(
        CART_PRESCRIPTION_URL,
        headers=_bff_pharmacy_headers(),
        json=body,
    )
    response.raise_for_status()
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


async def _post_eligibility(
    client: KaiserRequest,
    guid: str,
    rx_row: dict[str, Any],
    profile: Profile,
) -> dict[str, Any]:
    """POST rxdeliveryeligibility. Validates delivery to the patient's zip.

    The body is essentially the same Rx blob as cart/prescription but as an
    array entry under `cartItems[GUID]`, with first/last name flattened in,
    plus the patient's postalCode at the top level.
    """
    address = _primary_address(profile)
    zip_code = (address.postal_code if address else None) or ""

    afc = rx_row.get("afcInfo") or {}
    item: dict[str, Any] = {
        "amount": str(afc.get("copay") or ""),
        "applyFeature": True,
        "autoRefillEnrolled": bool(rx_row.get("autoRefillEnrolled", False)),
        "autoRefillEligible": bool(rx_row.get("autoRefillEligible", False)),
        "benefitIndicators": "N/A",
        "costEstimate": "",
        "costSavings": "N/A",
        "costSavingsAmount": "N/A",
        "daysSupplyThreshold": _str_or_none(rx_row.get("daysSupplyThreshold")) or "200",
        "deacode": _str_or_none(afc.get("deacode")) or "",
        "dispenseddayssupply": afc.get("dispenseddayssupply"),
        "dispenseLocationCode": _str_or_none(afc.get("dispenseLocationCode")) or "N/A",
        "fillable": bool(afc.get("fillable", True)),
        "fillOptions": rx_row.get("fillOptions") or [],
        "firstFill": "N/A",
        "firstName": profile.first_name or "",
        "instructions": _str_or_none(rx_row.get("consumerInstructions")) or "",
        "InsurancePlans": DEFAULT_INSURANCE_PLANS,
        "isAddedToCart": True,
        "isLastCopayDisplayed": False,
        "isM3PMem": False,
        "isMember": True,
        "isNewPrescription": "N/A",
        "isQuantityUpdated": False,
        "isTeenUser": False,
        "lastDispensedNDC": _str_or_none(afc.get("lastdispensedndc")) or "",
        "compound": "N/A",
        "lastName": profile.last_name or "",
        "lastRefillDate": _str_or_none(rx_row.get("lastRefillDate")) or "",
        "lastsoldDate": _str_or_none(rx_row.get("lastSoldDate")) or "",
        "legacyTrailClaims": False,
        "mailable": _str_or_none(rx_row.get("mailable")) or "true",
        "maxDaysSupply": _max_days_supply_from_fill_options(rx_row),
        "nextFillDate": _str_or_none(rx_row.get("nextFillDate")) or "",
        "nhinId": _str_or_none(rx_row.get("nhinId")) or "",
        "isOrderByRx": False,
        "prescribedBy": _str_or_none(rx_row.get("consumerName")) or "",
        "prescribedOn": _str_or_none(rx_row.get("prescribedOn")) or "",
        "mrn": _str_or_none(afc.get("mrn")) or "",
        "mrnrgn": _str_or_none(afc.get("mrnrgn")) or "",
        "prescriptionName": (_str_or_none(rx_row.get("medicineName")) or "").lower(),
        "prescriptionNumber": _str_or_none(afc.get("rxnbr")) or "",
        "quantity": "N/A",
        "rarStatus": bool(rx_row.get("rarStatus", False)),
        "rarCodeKey": _str_or_none(rx_row.get("rarCodeKey")) or "",
        "rarCodeValue": _str_or_none(rx_row.get("rarCodeValue")) or "",
        "refillable": "N/A",
        "refillEligible": bool(rx_row.get("refillEligible", False)),
        "refillReminder": "N/A",
        "refillsRemaining": _str_or_none(rx_row.get("refillsRemaining")) or "0",
        "removeRDOptions": False,
        "rxestimatedCopay": "N/A",
        "RxCustomIndicators": rx_row.get("RxCustomIndicators") or [],
        "selections": rx_row.get("selections") or {},
        "selectedDaysSupply": "N/A",
        "selectedEstimatedCopay": "N/A",
        "selectedQuantity": "N/A",
        "source": "EPIC",
        "useLastCopay": "N/A",
        "usrId": guid,
        "userIdType": "self",
    }
    body = {
        "cartItems": {guid: [item]},
        "postalCode": zip_code,
    }
    response = await client.post(
        ELIGIBILITY_URL,
        headers=_bff_pharmacy_headers(extra={"x-benefitsIndicator-feature": "true"}),
        json=body,
    )
    response.raise_for_status()
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


async def _post_place_order_mail(
    *,
    client: KaiserRequest,
    guid: str,
    rx_row: dict[str, Any],
    profile: Profile,
    wallet: CardOnFile,
    transaction_ref: str,
    delivery_window: str | None,
) -> dict[str, Any]:
    """POST placeorderMail. The committing step.

    Composes the full request body from rx_row + profile + wallet, with the
    same field shape as Kaiser's React frontend. Kaiser's BFF is strict about
    field presence — when in doubt, send what the browser sent (verbatim).
    """
    afc = rx_row.get("afcInfo") or {}
    member = _build_cosmos_member(guid, profile)
    address = _primary_address(profile)

    body: dict[str, Any] = {
        "placeOrderReq": {
            "deliveryMethod": DELIVERY_METHOD_MAIL,
            "transactionControlReference": transaction_ref,
            "optionalPayment": True,
            "placerDetails": {
                "placerID": {"placerIdType": "GUID", "placerIDValue": guid},
                "placerName": {
                    "firstName": profile.first_name or "",
                    "lastName": profile.last_name or "",
                    "middleName": profile.middle_name or "",
                },
                "preferredLanguage": "EN",
                "region": BFF_REGION_SENTINEL,
                "emailID": profile.email or "",
                "mobileNumber": _digits_only(_primary_mobile_number(profile)) or "",
            },
            "rxDetails": {
                "rxInfo": [{
                    "id": guid,
                    "idType": "self",
                    "memberName": {
                        "firstName": profile.first_name or "",
                        "lastName": profile.last_name or "",
                        "middleName": profile.middle_name or "",
                    },
                    "rxNumber": _str_or_none(afc.get("rxnbr")) or "",
                    "rxName": (_str_or_none(rx_row.get("medicineName")) or "").lower(),
                    "nhinId": _str_or_none(rx_row.get("nhinId")) or "",
                    "mrn": _str_or_none(afc.get("mrn")) or "",
                    "mrnRegion": _str_or_none(afc.get("mrnrgn")) or "",
                    "daysSupplyRequested": afc.get("dispenseddayssupply"),
                    "daysSupplyMax": afc.get("dispenseddayssupply"),
                    "responseCode": "00",
                    "estimatedCopay": _format_money(_estimated_mail_copay(rx_row)),
                    "estimatedCopaySource": "CE",
                }]
            },
            "cosmosData": {
                "member": member,
                "prescriptions": [{**rx_row, "isAddedToCart": True, "userId": "", "userType": ""}],
                "rxcomid": "",
            },
            "sourceApplication": SOURCE_APPLICATION,
            "sourceChannel": SOURCE_CHANNEL,
            "shippingAddress": {
                "street1": (address.street1 if address else "") or "",
                "city": (address.city if address else "") or "",
                "state": (address.state if address else "") or "",
                "zip": (address.postal_code if address else "") or "",
                "singleUse": False,
            },
            "creditCardDetails": {
                "oneTimeUse": False,
                "cardHolderName": {
                    "firstName": wallet.card_holder_first or profile.first_name or "",
                    "lastName": wallet.card_holder_last or profile.last_name or "",
                    "middleName": wallet.card_holder_middle or "",
                },
                "billingAddress": {"zipCode": wallet.billing_zip or ((address.postal_code if address else "") or "")},
                "expiryDate": wallet.expiry_date or "",
                "last4Digit": wallet.last_4_digit or "",
                "cardType": wallet.card_type or "",
                "walletPaymentToken": wallet.wallet_payment_token or "",
            },
            "deliveryStatus": delivery_window or "",
            "regionalPhone": "1-888-218-6245",
        }
    }

    response = await client.post(
        PLACE_ORDER_MAIL_URL,
        headers=_place_order_headers(guid),
        json=body,
    )
    response.raise_for_status()
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


# --- response parsing ---


def _parse_place_order_response(
    *,
    payload: dict[str, Any],
    rx_number: str,
    rx_name: str | None,
    delivery_window: str | None,
) -> OrderConfirmation:
    """Walk the placeorderMail response into an `OrderConfirmation`.

    Tolerant of malformed responses: missing fields become None, unexpected
    `executionContext.statusCode` values surface in `succeeded=False` rather
    than raising. Callers can choose to escalate based on the status code.
    """
    if not isinstance(payload, dict):
        return OrderConfirmation(
            rx_number=rx_number,
            name=rx_name,
            estimated_delivery_window=delivery_window,
        )

    order = payload.get("Order") or {}
    exec_ctx = payload.get("executionContext") or {}
    rx_refill = ((payload.get("rxRefillArray") or {}).get("rxRefill") or [])
    first_refill = rx_refill[0] if rx_refill and isinstance(rx_refill[0], dict) else {}

    exec_status = _str_or_none(exec_ctx.get("statusCode"))
    per_rx_status = _str_or_none(first_refill.get("rxOrderResponseCode"))
    succeeded = exec_status == SUCCESS_STATUS_CODE and per_rx_status == SUCCESS_STATUS_CODE

    return OrderConfirmation(
        order_number=_str_or_none(order.get("orderNumber")),
        placed_at=_str_or_none(order.get("orderPlacedDate")),
        delivery_method=_str_or_none(payload.get("deliveryMethod")) or DELIVERY_METHOD_MAIL,
        delivery_method_label=DELIVERY_METHOD_LABEL,
        estimated_delivery_window=delivery_window,
        rx_number=_str_or_none(first_refill.get("rxNumber")) or rx_number,
        name=rx_name,
        execution_status_code=exec_status,
        execution_status_details=_str_or_none(exec_ctx.get("statusDetails")),
        per_rx_response_code=per_rx_status,
        succeeded=succeeded,
        dry_run=False,
    )


# --- header builders ---


def _bff_base_headers() -> dict[str, str]:
    """Headers sent to every BFF call regardless of endpoint."""
    return {
        "Accept": "*/*",
        "Origin": BFF_ORIGIN,
        "Referer": f"{BFF_ORIGIN}/",
        "X-IBM-client-Id": BFF_CLIENT_ID,
    }


def _bff_pharmacy_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers for cart/prescription, rxdeliveryeligibility (POST). JSON content-type."""
    headers = _bff_base_headers()
    headers["Content-Type"] = "application/json"
    headers["x-cost-feature"] = "true"
    headers["x-benefitsIndicator-feature"] = "false"
    if extra:
        headers.update(extra)
    return headers


def _place_order_headers(guid: str) -> dict[str, str]:
    """placeorderMail uses a slightly different header set, including X-id and X-region."""
    headers = _bff_base_headers()
    headers["Content-Type"] = "application/json"
    headers["X-id"] = guid
    headers["X-region"] = BFF_REGION_SENTINEL
    headers["x-idType"] = "GUID"  # capitalized, vs lowercase elsewhere — verbatim
    headers["x-cost-feature"] = "true"
    headers["x-benefitsIndicator-feature"] = "false"
    return headers


def _wallet_headers() -> dict[str, str]:
    """walletV3 uses the pharmacy X-apiKey contract, not BFF style. See refill.md.

    Header casing matches the captured HAR exactly. Kaiser's API gateway has
    been observed to be case-sensitive on some headers (notably `x-apiKey`
    with lowercase x — that's how the browser sends it). Don't normalize.
    """
    headers = _bff_base_headers()
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["X-appName"] = "rwd"
    headers["X-env"] = "undefined"
    headers["X-osversion"] = "Mac OS 10.15.7"
    headers["X-region"] = BFF_REGION_SENTINEL
    headers["X-sessionToken"] = "false"
    headers["X-useragentcategory"] = "B"
    headers["X-useragenttype"] = "Macintosh"
    headers["x-apiKey"] = "kprwdpharmctr68973257122561335296"
    return headers


# --- helpers ---


def _build_cosmos_member(guid: str, profile: Profile) -> dict[str, Any]:
    """Build the cosmosData.member sub-object.

    Names appear UPPERCASE here (matches Kaiser's frontend). MRN is the raw
    value with leading zeros stripped — Kaiser's frontend normalizes it on
    output even though afcInfo.mrn keeps the zero-padded form.
    """
    address = _primary_address(profile)
    raw_mrn = profile.mrn or ""
    mrn_no_pad = raw_mrn.lstrip("0") if raw_mrn else ""
    return {
        "guid": guid,
        "mrn": mrn_no_pad,
        "firstName": (profile.first_name or "").upper(),
        "middleName": (profile.middle_name or "").upper(),
        "lastName": (profile.last_name or "").upper(),
        "shipToState": address.state if address else "",
        "removeRDOptions": False,
        "InsurancePlans": DEFAULT_INSURANCE_PLANS,
        "isM3PMem": False,
        "isTeenUser": False,
        "isEligibleForPaymentPlan": False,
    }


def _primary_address(profile: Profile):
    """Pick the address to ship to. Prefer the one flagged primary, else first."""
    if not profile.addresses:
        return None
    for a in profile.addresses:
        if a.is_primary:
            return a
    return profile.addresses[0]


def _primary_mobile_number(profile: Profile) -> str | None:
    """Pick the mobile phone to text on. Prefer 'MOBILE' type, else first phone."""
    if not profile.phones:
        return None
    for phone in profile.phones:
        if phone.type and phone.type.upper() in {"MOBILE", "CELL"}:
            return phone.number
    return profile.phones[0].number


def _digits_only(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def _short_address_label(profile: Profile) -> str | None:
    """Build a coarse 'City, State ZIP' label without echoing street1.

    Used in `RefillPreview` so the user can sanity-check delivery destination
    without us repeating their full home address back through the MCP surface.
    Kaiser's profile endpoint returns city in ALL CAPS; title-case it for the
    user-facing surface (state stays uppercase since two-letter codes look
    correct that way).
    """
    address = _primary_address(profile)
    if not address:
        return None
    parts: list[str] = []
    if address.city:
        parts.append(address.city.title())
    if address.state and address.postal_code:
        parts.append(f"{address.state} {address.postal_code}")
    elif address.state:
        parts.append(address.state)
    elif address.postal_code:
        parts.append(address.postal_code)
    return ", ".join(parts) if parts else None


def _estimated_mail_copay(rx_row: dict[str, Any]) -> float | None:
    """Pull the mail-delivery estimatedCopay from fillOptions[].

    Falls back to afcInfo.copay if no mail-delivery option is present, which
    can happen if the Rx is local-only (and therefore not refillable via this
    tool — see preview warnings).
    """
    fill_options = rx_row.get("fillOptions") or []
    if isinstance(fill_options, list):
        for option in fill_options:
            if isinstance(option, dict) and option.get("deliveryMethod") == DELIVERY_METHOD_MAIL:
                copay = option.get("estimatedCopay")
                if copay is not None:
                    try:
                        return float(copay)
                    except (TypeError, ValueError):
                        pass
    afc = rx_row.get("afcInfo") or {}
    raw = afc.get("copay")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _max_days_supply_from_fill_options(rx_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Distill fillOptions[] into the maxDaysSupply shape KP echoes."""
    fill_options = rx_row.get("fillOptions") or []
    out: list[dict[str, Any]] = []
    if isinstance(fill_options, list):
        for option in fill_options:
            if not isinstance(option, dict):
                continue
            out.append({
                "deliveryMethod": option.get("deliveryMethod"),
                "daysSupply": option.get("daysSupply"),
                "copaysttsdtl": option.get("copaysttsdtl"),
            })
    return out


_TOKEN_KEY_CANDIDATES = ("paymentToken", "walletPaymentToken", "token")

# Lookup from walletV3's full card-type names to the 2-letter codes
# placeorderMail expects. Verified from kp-refill-2.har for Amex.
# Other entries are inferred from common Kaiser/Epic conventions and
# need verification next time a non-Amex card is captured.
_CARD_TYPE_CODES = {
    "american express": "AM",
    "amex": "AM",
    "visa": "VI",
    "mastercard": "MC",
    "master card": "MC",
    "discover": "DI",
}


def _card_type_code(raw: Any) -> str | None:
    """Map walletV3's full card-type name ('American Express') to the 2-letter code.

    placeorderMail wants 'AM' / 'VI' / 'MC' / 'DI'. If we don't recognize the
    input, return the raw value uppercased — KP may accept it, and at worst
    we get a clean per-Rx response code we can surface to the user.
    """
    s = _str_or_none(raw)
    if not s:
        return None
    code = _CARD_TYPE_CODES.get(s.lower())
    if code:
        return code
    logger.warning("walletV3 returned unrecognized cardType %r; sending verbatim uppercase", s)
    return s.upper()


def _format_expiry_date(raw: Any) -> str | None:
    """Convert walletV3's expDate ('YYMM') to placeorderMail's expiryDate ('MM/YY').

    Verified from kp-refill-2.har: '2802' -> '02/28'. Returns None for any
    input that isn't exactly 4 digits — better to send empty than garbage.
    """
    s = _str_or_none(raw)
    if not s or len(s) != 4 or not s.isdigit():
        return None
    yy = s[0:2]   # year (2 digits)
    mm = s[2:4]   # month
    return f"{mm}/{yy}"


def _find_card_with_token(value: Any) -> dict[str, Any] | None:
    """Recursive walk to find the first dict carrying a non-empty token.

    The /walletV3 response shape was elided from the HAR. Be tolerant: try
    `walletPaymentToken` (what placeorderMail expects), `paymentToken`, and
    plain `token` as the trigger key. The card details (last4Digit, etc.) are
    extracted from siblings of whichever key matches.
    """
    if isinstance(value, dict):
        for key in _TOKEN_KEY_CANDIDATES:
            if _str_or_none(value.get(key)):
                return value
        for v in value.values():
            found = _find_card_with_token(v)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_card_with_token(item)
            if found is not None:
                return found
    return None


def _shape_summary(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """Produce a key-only structural summary of a JSON value for diagnostic logs.

    Returns `dict[keys]` for dicts, `[len=N]` for lists, type names for
    primitives. NEVER returns values — this is meant to be safe to log when
    we don't know what's inside the response.
    """
    if depth >= max_depth:
        return type(value).__name__
    if isinstance(value, dict):
        return {k: _shape_summary(v, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, list):
        return f"[len={len(value)}]" if not value else [_shape_summary(value[0], depth + 1, max_depth)]
    return type(value).__name__


def _compose_delivery_window(payload: Any) -> str | None:
    """Build KP's "Estimated to arrive between X and Y." string from from/to dates.

    Mimics the frontend's output verbatim, including the trailing space and
    no-year format ("Wednesday, April 29 and Friday, May 1"). Robust to
    missing or malformed dates: returns None rather than partial string.
    """
    if not isinstance(payload, dict):
        return None
    raw_from = _str_or_none(payload.get("from"))
    raw_to = _str_or_none(payload.get("to"))
    if not raw_from or not raw_to:
        return None
    try:
        from_dt = datetime.strptime(raw_from, "%m/%d/%Y")
        to_dt = datetime.strptime(raw_to, "%m/%d/%Y")
    except ValueError:
        return None
    fmt = "%A, %B %-d"  # "Wednesday, April 29" — %-d strips the leading zero
    return f"Estimated to arrive between {from_dt.strftime(fmt)} and {to_dt.strftime(fmt)}. "


def _eligible_date_has_passed(raw_date: str | None, bucket: str | None) -> bool:
    """True when bucket=fillable AND `next_fill_eligible_date` is today or earlier.

    Used to override a stale `refillEligible: false` flag when Kaiser's own
    bucket assignment + date math both say "yes". We require BOTH bucket
    membership and a parsable past date — neither alone is sufficient.

    The date format Kaiser ships in afcInfo.nextFillEligibleDate is MM/DD/YYYY.
    Returns False on any parse failure (conservative — we'd rather show the
    warning than skip it on garbage data).
    """
    if bucket != "fillable" or not raw_date:
        return False
    try:
        eligible = datetime.strptime(raw_date.strip(), "%m/%d/%Y").date()
    except ValueError:
        return False
    return eligible <= datetime.now().date()


def _format_money(value: float | None) -> str:
    """Format a copay as Kaiser's frontend does: two decimals, default '0.00'."""
    if value is None:
        return "0.00"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool_truthy(value: Any) -> bool:
    """Accept Kaiser's stringy booleans ('true'/'false') in addition to real bools."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)
