"""Standalone test: does Playwright + playwright-stealth defeat Reblaze on
super-pharm and KSP from your residential IP?

Run from your laptop (not the bagofwords backend) to validate the approach
before we add code to web_fetch.

    pip install playwright playwright-stealth
    playwright install chromium
    python test_stealth_fetch.py

For each URL it prints:
 - final URL after redirects
 - HTTP status
 - whether real product content was extracted (title, h1, body length)
 - first 400 chars of visible text
 - whether the page looks like a bot-challenge (small body, mostly scripts)

If you see real titles like 'CARELINE BOLD ...' and body text containing
the price (₪ symbol + a number), the approach works and we should land it.
If every page comes back as a challenge / blocked, stealth is insufficient
and we need to escalate (camoufox, residential proxy, scraping API).
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Dict, Optional, Tuple

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

URLS = [
    "https://shop.super-pharm.co.il/cosmetics/eye-makeup/mascara/BOLD-%D7%9E%D7%A1%D7%A7%D7%A8%D7%94-%D7%A9%D7%97%D7%95%D7%A8%D7%94/p/449344",
    "https://ksp.co.il/web/item/251453?appkey=1006",
    "https://careline.co.il/bold-mascara-2/",  # control: known to work even with plain fetch
]

NAV_TIMEOUT_MS = 30_000
SETTLE_MS = 2_500          # wait after load for sensor JS / hydration
MAX_BODY_BYTES = 1_000_000


def looks_like_challenge(html: str, visible_text: str) -> Tuple[bool, str]:
    """Heuristic: bot-protection pages are small, mostly scripts, no body text."""
    if len(html) < 5_000 and "<script" in html.lower() and len(visible_text.strip()) < 100:
        markers = []
        for needle in ("rbzns", "kramericaindustries", "_abck", "cf_chl_", "bm_sz",
                       "cf-mitigated", "cdn-cgi/challenge", "px-captcha", "datadome"):
            if needle in html.lower():
                markers.append(needle)
        return True, ", ".join(markers) or "small body + scripts only"
    return False, ""


def extract_visible_text(html: str) -> str:
    """Very crude tag-stripper for the diagnostic only — production parser
    is selectolax-based."""
    no_script = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    no_style = re.sub(r"<style\b[^>]*>.*?</style>", " ", no_script, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", no_style)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def fetch_one(url: str) -> Dict:
    result: Dict = {"url": url}
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
                navigator_languages_override=("he-IL", "he", "en-US", "en"),
                navigator_platform_override="MacIntel",
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="he-IL",
                timezone_id="Asia/Jerusalem",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            await stealth.apply_stealth_async(context)
            page = await context.new_page()

            response = None
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as e:
                result["nav_error"] = repr(e)
            # Let sensor JS / SPA hydration finish
            await page.wait_for_timeout(SETTLE_MS)

            try:
                html = await page.content()
            except Exception as e:
                html = ""
                result["content_error"] = repr(e)

            if len(html) > MAX_BODY_BYTES:
                html = html[:MAX_BODY_BYTES]

            visible = extract_visible_text(html)
            title = await page.title()

            result["status"] = response.status if response else None
            result["final_url"] = page.url
            result["title"] = title
            result["html_bytes"] = len(html)
            result["visible_chars"] = len(visible)
            result["visible_preview"] = visible[:400]

            challenge, markers = looks_like_challenge(html, visible)
            result["looks_like_challenge"] = challenge
            result["challenge_markers"] = markers

            # h1 heuristic — products usually have one
            h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
            result["h1"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h1_match.group(1))).strip()[:120] if h1_match else None

            # Price heuristic — look for ₪ or ILS near a number
            price = re.search(r"(₪\s*\d[\d,.]*|\d[\d,.]*\s*₪|\bILS\s*\d[\d,.]*)", visible)
            result["price_hit"] = price.group(0) if price else None
        finally:
            await browser.close()
    return result


def print_result(r: Dict) -> None:
    print(f"\n=== {r['url']}")
    if r.get("fatal_error"):
        print(f"  FATAL: {r['fatal_error']}")
        return
    print(f"  status           : {r.get('status')}")
    print(f"  final_url        : {r.get('final_url')}")
    print(f"  title            : {r.get('title')!r}")
    print(f"  html_bytes       : {r.get('html_bytes')}")
    print(f"  visible_chars    : {r.get('visible_chars')}")
    print(f"  h1               : {r.get('h1')!r}")
    print(f"  price_hit        : {r.get('price_hit')!r}")
    print(f"  looks_like_challenge: {r.get('looks_like_challenge')}  ({r.get('challenge_markers')})")
    if r.get("nav_error"): print(f"  nav_error        : {r['nav_error']}")
    if r.get("content_error"): print(f"  content_error    : {r['content_error']}")
    print(f"  visible_preview  : {r.get('visible_preview')!r}")


async def main():
    for url in URLS:
        try:
            r = await fetch_one(url)
        except Exception as e:
            import traceback
            r = {"url": url, "fatal_error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}"}
        print_result(r)


if __name__ == "__main__":
    asyncio.run(main())
