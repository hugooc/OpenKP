"""One-shot recon: dump problems/allergies endpoint responses for inspection.

Why this exists: Chrome stripped response bodies from the HAR export, so we
can't read the JSON shapes directly. This script reuses the persisted Kaiser
session and calls the four candidate endpoints (summary widget + drill-in
list, for both problems and allergies), writing each raw response to
`docs/research/captures/recon-<name>.json`.

Run from the repo root:

    .venv/bin/python openkp/scripts/recon_problems_allergies.py

Outputs are PHI. Don't commit them.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

from openkp.config import load_config
from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest
from openkp.scrapers.session import SessionStore

OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "research" / "captures"

HEALTH_SUMMARY_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/app/health-summary"
PROBLEMS_DRILLIN_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Clinical/HealthIssues"
ALLERGIES_DRILLIN_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Clinical/Allergies"


def _api_headers(csrf_token: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


async def fetch_and_save(
    client: KaiserRequest,
    *,
    name: str,
    path: str,
    body: dict,
    referer: str,
) -> None:
    csrf = await fetch_csrf_token(client, referer=referer)
    response = await client.post(path, headers=_api_headers(csrf, referer), json=body)
    out = OUT_DIR / f"recon-{name}.json"
    out.write_bytes(response.content)
    try:
        parsed = response.json()
        top_keys = sorted(parsed.keys()) if isinstance(parsed, dict) else f"<{type(parsed).__name__}>"
    except Exception:
        top_keys = "<unparseable>"
    print(f"{name}: HTTP {response.status_code}  {len(response.content):>7} bytes  top_keys={top_keys}")


async def main() -> None:
    cfg = load_config()
    store = SessionStore(cfg.data_dir, cfg.username, cfg.password)
    client = KaiserRequest(store)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Summary widgets fired from the Health Summary page.
    await fetch_and_save(
        client,
        name="problems-summary",
        path="/mychartcn/api/HealthIssues/LoadHealthIssuesData",
        body={"isHealthSummary": True},
        referer=HEALTH_SUMMARY_REFERER,
    )
    await fetch_and_save(
        client,
        name="allergies-summary",
        path="/mychartcn/api/allergies/LoadAllergies",
        body={"isHealthSummary": True},
        referer=HEALTH_SUMMARY_REFERER,
    )

    # Drill-in endpoints fired from the dedicated Clinical/* pages.
    # Query params are observed in earlier HAR; csn=undefined is literal.
    drillin_qs = f"?csn=undefined&ComponentNumber=4&noCache={random.random()}"
    await fetch_and_save(
        client,
        name="problems-drillin",
        path="/mychartcn/Clinical/HealthIssues/LoadListData" + drillin_qs,
        body={},
        referer=PROBLEMS_DRILLIN_REFERER,
    )
    await fetch_and_save(
        client,
        name="allergies-drillin",
        path="/mychartcn/Clinical/Allergies/LoadListData" + drillin_qs,
        body={},
        referer=ALLERGIES_DRILLIN_REFERER,
    )


if __name__ == "__main__":
    asyncio.run(main())
