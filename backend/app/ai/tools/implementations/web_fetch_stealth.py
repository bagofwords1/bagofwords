"""Playwright-driven tier-2 fallback for web_fetch.

Tier-1 uses curl_cffi with a Chrome TLS fingerprint, which beats most JA3
checks but cannot execute the JS challenges (Reblaze rbzns, Akamai _abck,
Cloudflare cf_chl_, PerimeterX, DataDome) that some sites use to gate
content. This module spins up a real headless Chromium with
playwright-stealth patches applied to the navigator, plugins, WebGL and
sec-ch-ua surfaces, so those challenges actually run before we read the
DOM.

Kept in its own module so the playwright/playwright_stealth imports stay
lazy (the function is called only when tier-1 looks blocked) and so the
tests can patch a single name without touching the curl_cffi path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

STEALTH_NAV_TIMEOUT_MS = 30_000
STEALTH_SETTLE_MS = 2_500
STEALTH_VIEWPORT = {"width": 1440, "height": 900}
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
IL_TLDS = (".co.il", ".org.il", ".net.il", ".ac.il", ".gov.il")


@dataclass
class StealthFetchResult:
    status: Optional[int]
    final_url: Optional[str]
    html: Optional[str]
    error: Optional[str] = None


def _locale_for(host: str) -> tuple[tuple[str, str], str, str]:
    """Bias locale, timezone and language tuple by TLD.

    IL retail (super-pharm, careline, ksp) ships Hebrew pages and some
    look at Accept-Language. For everything else we keep the en-US/UTC
    defaults so we don't lie about the visitor in a way the site might
    score against us.
    """
    h = (host or "").lower()
    if any(h.endswith(tld) for tld in IL_TLDS):
        return (("he-IL", "he", "en-US", "en"), "he-IL", "Asia/Jerusalem")
    return (("en-US", "en"), "en-US", "UTC")


async def fetch_via_stealth(
    url: str,
    *,
    nav_timeout_ms: int = STEALTH_NAV_TIMEOUT_MS,
    settle_ms: int = STEALTH_SETTLE_MS,
    max_html_bytes: int = 1_000_000,
) -> StealthFetchResult:
    """Render `url` in a stealth-patched Chromium and return the final HTML.

    Errors during launch / navigation / content-read are captured on the
    result rather than raised — the caller treats this as a best-effort
    second tier.
    """
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        return StealthFetchResult(None, None, None, error=f"stealth deps unavailable: {exc}")

    host = urlparse(url).hostname or ""
    languages, locale, timezone = _locale_for(host)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                stealth = Stealth(
                    navigator_languages_override=languages,
                    navigator_platform_override="MacIntel",
                )
                context = await browser.new_context(
                    viewport=STEALTH_VIEWPORT,
                    locale=locale,
                    timezone_id=timezone,
                    user_agent=STEALTH_USER_AGENT,
                )
                await stealth.apply_stealth_async(context)
                page = await context.new_page()

                response = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                await page.wait_for_timeout(settle_ms)

                html = await page.content()
                if len(html) > max_html_bytes:
                    html = html[:max_html_bytes]

                return StealthFetchResult(
                    status=response.status if response else None,
                    final_url=page.url,
                    html=html,
                )
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning(f"web_fetch_stealth: navigation failed for {url}: {exc}")
        return StealthFetchResult(None, None, None, error=f"stealth fetch failed: {exc}")
