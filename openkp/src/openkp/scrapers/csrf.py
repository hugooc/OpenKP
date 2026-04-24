"""CSRF anti-forgery token fetcher for Kaiser endpoints.

Many MyChart endpoints behind `/mychartcn/` require a
`__RequestVerificationToken` header or the ASP.NET anti-forgery middleware
bounces the request to `/mychartcn/Home/FiveHundred`. The token is served
by a dedicated endpoint that returns an HTML fragment with the value in a
hidden input element. OpenKP fetches a fresh token per call — cheap, and
sidesteps the question of how long any one token is valid.

Observed to be required for at least:
- `POST /mychartcn/Clinical/CareTeam/Load`
- `POST /mychartcn/api/conversations/GetConversationList`
- `POST /mychartcn/api/conversations/GetConversationDetails`
- `POST /mychartcn/api/conversations/GetFoldersList`

See `docs/research/endpoints/profile.md` (CareTeam section) and
`docs/research/endpoints/messages.md` for full per-endpoint contracts.
"""

from __future__ import annotations

import random
import re

from openkp.scrapers.request import KaiserRequest

CSRF_PATH = "/mychartcn/Home/CSRFToken"

# Response HTML looks like: <input name="__RequestVerificationToken" value="..."/>
_CSRF_INPUT_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
    re.IGNORECASE,
)


async def fetch_csrf_token(client: KaiserRequest, referer: str) -> str:
    """Fetch a one-shot CSRF anti-forgery token.

    Args:
      client: authenticated Kaiser HTTP client.
      referer: the page URL the calling endpoint will use as its `Referer`
        header. Pass the same value both here and in the subsequent POST.

    Returns the raw token string. Raises `ValueError` if the expected
    input element isn't present in the response (i.e. Kaiser changed the
    page shape).
    """
    params = {"noCache": f"{random.random()}"}
    headers = {
        "Accept": "*/*",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = await client.get(CSRF_PATH, params=params, headers=headers)
    response.raise_for_status()
    match = _CSRF_INPUT_RE.search(response.text)
    if not match:
        raise ValueError("CSRF token input not found in response")
    return match.group(1)
