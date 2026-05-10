"""Kaiser Permanente login via Playwright.

kp.org sits behind PingFederate OAuth2 / OIDC. See
`docs/research/endpoints/auth.md` for the full redirect map derived from the
kp-login-2.har capture. Key moves:

    1. GET https://healthy.kaiserpermanente.org/sign-on
    2. Redirects through /secure/inner-door to
       https://identityauth.kaiserpermanente.org/as/authorization.oauth2
       with client_id=KPORGOauthClientPAWebSessionV1
    3. User (or us) submits pf.username + pf.pass to the Ping form
    4. Optional MFA challenge (SMS, email, push, WebAuthn). User completes
       by hand in the visible browser window.
    5. Ping issues an auth code, redirects back to /pa/oidc/cb.
    6. Epic MyChart session handoff lands on /mychartcn/Home?lang=en-US.

Because the flow includes device fingerprinting, adaptive risk scoring, and
optional WebAuthn, we drive a real Chromium via Playwright rather than raw
HTTP.

Strategy (see ADR-005):
- First run: launch Chromium headed with a persistent user data dir.
  OpenKP autofills username + password from keyring. User handles any MFA
  challenge in the visible window.
- Subsequent runs: reopen the same profile headless and probe
  /mycare/v1.0/user. If the session is alive, extract cookies. If not,
  caller falls back to interactive login.

OpenKP never handles any MFA factor in code. No TOTP, no SMS scraping, no
email scraping, no push automation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

KP_SIGN_ON_URL = "https://healthy.kaiserpermanente.org/sign-on"

PING_HOST = "identityauth.kaiserpermanente.org"
KP_HOST = "healthy.kaiserpermanente.org"

# How long to wait for the user to finish MFA in the visible browser.
INTERACTIVE_LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes


@dataclass
class KaiserSession:
    """A successfully authenticated session. Pass to the HTTP client."""

    cookies: list[dict]  # Playwright cookie shape: name, value, domain, path, ...
    user_agent: str

    def save_to(self, path: Path) -> None:
        """Write cookies + user_agent to `path` as JSON, chmod 0600.

        File contents are equivalent in sensitivity to the cookies in the
        Playwright profile at `~/.openkp/chromium-profile/`, which are also
        stored unencrypted on disk — so this does not widen the blast radius.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: temp file + rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self)))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @classmethod
    def load_from(cls, path: Path) -> KaiserSession | None:
        """Return the persisted session, or None if missing / corrupt."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return cls(cookies=data["cookies"], user_agent=data["user_agent"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Persisted session at %s is unreadable (%s), ignoring", path, exc)
            return None


async def login_interactive(data_dir: Path, username: str, password: str) -> KaiserSession:
    """Launch Chromium, complete the login + MFA flow, return cookies.

    Headed. The browser window is visible because the user may need to
    complete MFA. If the persistent profile already holds a live Kaiser
    session, the sign-on URL skips Ping entirely and we just extract cookies.

    Raises:
        playwright.async_api.TimeoutError: user did not complete login within
            INTERACTIVE_LOGIN_TIMEOUT_MS.
    """
    profile_dir = _profile_dir(data_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            logger.info("Navigating to %s", KP_SIGN_ON_URL)
            await page.goto(KP_SIGN_ON_URL, wait_until="load")

            if PING_HOST in page.url:
                logger.info("On Ping login form, autofilling credentials")
                await page.wait_for_selector(
                    'input[name="pf.username"]', state="visible", timeout=30_000
                )
                await page.fill('input[name="pf.username"]', username)
                await page.fill('input[name="pf.pass"]', password)
                # Submit by pressing Enter in the password field. This fires
                # whatever onsubmit handlers Ping attached, which matters
                # because those feed the risk-scoring fingerprint.
                await page.press('input[name="pf.pass"]', "Enter")

                logger.info(
                    "Credentials submitted, waiting up to %d seconds for login to complete "
                    "(including any MFA challenge)",
                    INTERACTIVE_LOGIN_TIMEOUT_MS // 1000,
                )
                await page.wait_for_url(_is_logged_in_url, timeout=INTERACTIVE_LOGIN_TIMEOUT_MS)
            else:
                # Either already landed on /mychartcn/Home via the persistent
                # profile, or still mid-redirect. Give it a short window.
                if not _is_logged_in_url(page.url):
                    logger.info(
                        "Sign-on did not land on Ping, waiting for /mychartcn/Home"
                    )
                    await page.wait_for_url(_is_logged_in_url, timeout=60_000)
                else:
                    logger.info("Persistent profile already holds a live session")

            logger.info("Login complete at %s", page.url)
            # Playwright returns list[Cookie] (TypedDict); convert to plain
            # list[dict] so the rest of OpenKP doesn't take a Playwright type
            # dependency.
            cookies: list[dict] = [dict(c) for c in await context.cookies()]
            user_agent = await page.evaluate("() => navigator.userAgent")
            _log_cookie_names(cookies)

            return KaiserSession(cookies=cookies, user_agent=user_agent)
        finally:
            await context.close()


def _profile_dir(data_dir: Path) -> Path:
    return data_dir / "chromium-profile"


def _is_logged_in_url(url: str) -> bool:
    """True when the URL is the logged-in MyChart home page.

    Excludes the intermediate `/mychartcn/Authentication/Login` hop in the
    Epic session-handoff chain (step 7 in auth.md).
    """
    return (
        KP_HOST in url
        and "/mychartcn/Home" in url
        and "/Authentication/Login" not in url
    )


def _log_cookie_names(cookies: list[dict]) -> None:
    """Log the names (never values) of the cookies Playwright captured.

    auth.md section 10 flags cookie-name enumeration as an open question the
    first real login should close. Names only, never values.
    """
    names = sorted({c["name"] for c in cookies})
    logger.info("Session cookies captured (%d): %s", len(names), ", ".join(names))
