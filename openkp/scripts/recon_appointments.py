"""One-shot recon: dump appointment endpoint responses for inspection.

Why this exists: Chrome strips response bodies from HAR exports as the
network panel evicts older entries, so the JSON shapes for the visit
endpoints aren't available in the HAR. This script reuses the persisted
Kaiser session and calls the four candidate endpoints on the Visits
landing page, writing each raw response to `docs/research/captures/
recon-<name>.json` (or `.html` for visitdetails).

Run from the repo root:

    .venv/bin/python openkp/scripts/recon_appointments.py

Outputs are PHI. Don't commit them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from pathlib import Path

from openkp.config import load_config
from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest
from openkp.scrapers.session import SessionStore

OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "research" / "captures"

VISITS_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Visits"


def _xhr_headers(csrf_token: str, *, content_type: str = "application/json") -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": content_type,
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": VISITS_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


async def _save(name: str, response, *, ext: str = "json") -> None:
    out = OUT_DIR / f"recon-{name}.{ext}"
    out.write_bytes(response.content)
    body = response.content
    print(
        f"{name}: HTTP {response.status_code}  "
        f"ct={response.headers.get('content-type','?'):<40}  "
        f"{len(body):>7} bytes  -> {out.name}"
    )


async def main() -> None:
    cfg = load_config()
    store = SessionStore(cfg.data_dir, cfg.username, cfg.password)
    client = KaiserRequest(store)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    csrf = await fetch_csrf_token(client, referer=VISITS_REFERER)

    # 1. VisitsHeaderOptions — small config blob, probably tab counts
    r = await client.post(
        "/mychartcn/Visits/VisitsList/VisitsHeaderOptions",
        params={"ComponentNumber": "4", "noCache": str(random.random())},
        headers=_xhr_headers(csrf),
    )
    await _save("appointments-header", r)

    # 2. LoadAppointmentRequest — pending appointment requests
    r = await client.post(
        "/mychartcn/Visits/VisitsList/LoadAppointmentRequest",
        params={"noCache": str(random.random())},
        headers={**_xhr_headers(csrf), "Accept": "*/*"},
    )
    await _save("appointments-pending", r)

    # 3. LoadUpcoming — the primary endpoint ("when's my next appointment")
    r = await client.post(
        "/mychartcn/Visits/VisitsList/LoadUpcoming",
        params={
            "timeZone": "America/Los_Angeles",
            "ComponentNumber": "5",
            "noCache": str(random.random()),
        },
        headers=_xhr_headers(csrf),
    )
    await _save("appointments-upcoming", r)

    # 4. LoadPast — first page of past visits.
    # Body is form-encoded. `serializedIndex` empty on first page; subsequent
    # pages echo the cursor from the previous response.
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.datetime.now(dt.timezone.utc).microsecond // 1000:03d}Z"
    )
    r = await client.post(
        "/mychartcn/Visits/VisitsList/LoadPast",
        params={
            "loadpast": "1",
            "searchString": "",
            "oldestRenderedDate": now_iso,
            "ComponentNumber": "7",
            "noCache": str(random.random()),
        },
        headers=_xhr_headers(csrf, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        content="serializedIndex=",
    )
    await _save("appointments-past", r)


if __name__ == "__main__":
    asyncio.run(main())
