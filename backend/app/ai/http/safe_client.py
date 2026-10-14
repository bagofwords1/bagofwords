"""Shared HTTP client used by `web_fetch` and by code-exec sandbox injection.

The model-facing surface (`SafeHttpClient`) is sync so it can be called from
`generate_df` without the model touching asyncio. Internally `batch_get` uses
`asyncio.gather` with a semaphore for concurrency. Spinning a fresh event
loop via `asyncio.run` is safe because the code-exec worker thread has no
running loop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cf_requests
from curl_cffi.requests.exceptions import RequestException, Timeout
from selectolax.parser import HTMLParser

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 5_000_000
MAX_TEXT_CHARS = 200_000
MAX_TITLE_CHARS = 500
MAX_DESCRIPTION_CHARS = 1_000
MAX_META_ENTRIES = 50
MAX_META_VALUE_CHARS = 500
MAX_JSON_LD_BYTES = 30_000
MAX_HEADINGS = 30
MAX_HEADING_CHARS = 200
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_BATCH_TIMEOUT_SECONDS = 15
DEFAULT_BATCH_CONCURRENCY = 20
MAX_BATCH_CONCURRENCY = 50
MAX_BATCH_URLS = 500
MAX_REDIRECTS = 5
IMPERSONATE_PROFILE = "chrome131"
HTML_CONTENT_PREFIXES = ("text/html", "application/xhtml+xml")
TEXT_CONTENT_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/ld+json",
)
STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "aside", "svg", "template")
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_NEWLINE_RE = re.compile(r"\n{3,}")


def is_safe_host(hostname: Optional[str]) -> bool:
    """Reject non-public hosts (loopback, RFC1918, link-local, metadata, etc.)."""
    if not hostname:
        return False
    lowered = hostname.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _content_type_matches(content_type: str, prefixes: Tuple[str, ...]) -> bool:
    ct = (content_type or "").lower()
    return any(ct.startswith(p) for p in prefixes)


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def parse_html(html: str) -> Dict[str, Any]:
    """Extract title/description/meta/json_ld/headings/text from an HTML doc."""
    parsed: Dict[str, Any] = {
        "title": None,
        "description": None,
        "metadata": {},
        "json_ld": [],
        "headings": [],
        "text": "",
    }
    tree = HTMLParser(html)

    title_node = tree.css_first("title")
    if title_node is not None:
        parsed["title"] = _truncate(title_node.text(strip=True), MAX_TITLE_CHARS)

    metadata: Dict[str, str] = {}
    for node in tree.css("meta"):
        if len(metadata) >= MAX_META_ENTRIES:
            break
        attrs = node.attributes
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        value = attrs.get("content")
        if not key or value is None:
            continue
        key = key.strip()[:120]
        if not key or key in metadata:
            continue
        metadata[key] = _truncate(value.strip(), MAX_META_VALUE_CHARS) or ""
    parsed["metadata"] = metadata
    parsed["description"] = _truncate(
        metadata.get("description") or metadata.get("og:description") or metadata.get("twitter:description"),
        MAX_DESCRIPTION_CHARS,
    )

    json_ld_blocks: List[Dict[str, Any]] = []
    json_ld_bytes = 0
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            serialized = json.dumps(data, default=str)
        except (TypeError, ValueError):
            continue
        if json_ld_bytes + len(serialized) > MAX_JSON_LD_BYTES:
            break
        json_ld_bytes += len(serialized)
        if isinstance(data, list):
            json_ld_blocks.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            json_ld_blocks.append(data)
    parsed["json_ld"] = json_ld_blocks

    headings: List[str] = []
    for node in tree.css("h1, h2"):
        text = node.text(strip=True)
        if not text:
            continue
        headings.append(_truncate(text, MAX_HEADING_CHARS))
        if len(headings) >= MAX_HEADINGS:
            break
    parsed["headings"] = headings

    body = tree.body or tree.root
    if body is not None:
        for tag in STRIP_TAGS:
            for node in body.css(tag):
                node.decompose()
        try:
            raw_text = body.text(separator="\n", strip=False)
        except TypeError:
            raw_text = body.text()
        if raw_text:
            lines = [_WHITESPACE_RE.sub(" ", ln).strip() for ln in raw_text.split("\n")]
            collapsed = "\n".join(ln for ln in lines if ln)
            parsed["text"] = _NEWLINE_RE.sub("\n\n", collapsed)

    return parsed


@dataclass
class FetchedPage:
    """Result of a single `SafeHttpClient.get` call.

    Same shape regardless of single vs batch fetch, so model code written
    against `http.get(url)` works unchanged for `http.batch_get([...])`.
    """

    url: str
    success: bool = False
    status: Optional[int] = None
    final_url: Optional[str] = None
    content_type: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    meta: Dict[str, str] = field(default_factory=dict)
    json_ld: List[Dict[str, Any]] = field(default_factory=list)
    headings: List[str] = field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None

    def __repr__(self) -> str:
        if self.error:
            return f"FetchedPage(url={self.url!r}, error={self.error!r})"
        return (
            f"FetchedPage(url={self.url!r}, status={self.status}, "
            f"title={self.title!r}, json_ld={len(self.json_ld)} block(s), "
            f"text={len(self.text or '')} chars)"
        )


async def _fetch_one_async(
    session: "cf_requests.AsyncSession",
    url: str,
    timeout: int,
) -> FetchedPage:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        return FetchedPage(url=url, error="URL must be http(s) with a hostname.")
    if not is_safe_host(parsed_url.hostname):
        return FetchedPage(url=url, error="Refusing to fetch a non-public address.")

    current_url = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            response = await session.get(
                current_url, allow_redirects=False, stream=False, timeout=timeout
            )
            status = response.status_code

            if 300 <= status < 400 and status != 304:
                location = response.headers.get("location")
                if not location:
                    break
                redirect_url = urljoin(current_url, location)
                if not is_safe_host(urlparse(redirect_url).hostname):
                    return FetchedPage(
                        url=url,
                        final_url=redirect_url,
                        status=status,
                        error="Refusing to follow redirect to a non-public address.",
                    )
                current_url = redirect_url
                continue

            content_type = (response.headers.get("content-type") or "").lower()
            final_url = str(response.url)
            body_bytes = response.content or b""
            truncated = False
            if len(body_bytes) > MAX_RESPONSE_BYTES:
                body_bytes = body_bytes[:MAX_RESPONSE_BYTES]
                truncated = True

            is_html = _content_type_matches(content_type, HTML_CONTENT_PREFIXES)
            is_text = _content_type_matches(content_type, TEXT_CONTENT_PREFIXES) or is_html

            page = FetchedPage(
                url=url,
                success=True,
                status=status,
                final_url=final_url,
                content_type=content_type or None,
                truncated=truncated,
            )

            if not is_text:
                page.error = f"Skipped non-text content-type: {content_type or 'unknown'}."
                return page

            encoding = response.encoding or "utf-8"
            try:
                body_text = body_bytes.decode(encoding, errors="replace")
            except LookupError:
                body_text = body_bytes.decode("utf-8", errors="replace")

            if is_html:
                try:
                    parsed = parse_html(body_text)
                    page.title = parsed["title"]
                    page.description = parsed["description"]
                    page.meta = parsed["metadata"] or {}
                    page.json_ld = parsed["json_ld"] or []
                    page.headings = parsed["headings"] or []
                    text_body = parsed["text"] or ""
                    if len(text_body) > MAX_TEXT_CHARS:
                        text_body = text_body[:MAX_TEXT_CHARS]
                        page.truncated = True
                    page.text = text_body
                except Exception as exc:
                    logger.warning("safe_http: HTML parse failed for %s: %s", final_url, exc)
                    fallback = body_text
                    if len(fallback) > MAX_TEXT_CHARS:
                        fallback = fallback[:MAX_TEXT_CHARS]
                        page.truncated = True
                    page.text = fallback
            else:
                if len(body_text) > MAX_TEXT_CHARS:
                    body_text = body_text[:MAX_TEXT_CHARS]
                    page.truncated = True
                page.text = body_text

            return page

        return FetchedPage(
            url=url,
            final_url=current_url,
            error=f"Too many redirects (limit {MAX_REDIRECTS}).",
        )
    except Timeout:
        return FetchedPage(url=url, error=f"Request timed out after {timeout}s.")
    except RequestException as e:
        return FetchedPage(url=url, error=f"Request failed: {e}")


async def _batch_get_async(
    urls: List[str], concurrency: int, timeout: int
) -> List[FetchedPage]:
    semaphore = asyncio.Semaphore(concurrency)

    async with cf_requests.AsyncSession(
        impersonate=IMPERSONATE_PROFILE,
        timeout=timeout,
    ) as session:
        async def one(u: str) -> FetchedPage:
            async with semaphore:
                return await _fetch_one_async(session, u, timeout)

        return await asyncio.gather(*(one(u) for u in urls))


class SafeHttpClient:
    """Sync, model-facing HTTP client injected into the code-exec sandbox.

    Concurrency lives inside `batch_get` so the sandbox's AST validator
    (which forbids `asyncio` / `threading` / `socket`) never sees the model
    reach for those modules.
    """

    def __init__(self, audit_callback=None):
        self._audit_callback = audit_callback

    def get(self, url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> FetchedPage:
        """Fetch a single URL. Returns a FetchedPage with `.error` set on failure."""
        if not isinstance(url, str):
            return FetchedPage(url=str(url), error="url must be a string")
        timeout = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT_SECONDS
        page = asyncio.run(self._get_one(url, timeout))
        self._audit("http.get", urls=[url], errors=(0 if page.success and not page.error else 1))
        return page

    def batch_get(
        self,
        urls: List[str],
        *,
        concurrency: int = DEFAULT_BATCH_CONCURRENCY,
        timeout: int = DEFAULT_BATCH_TIMEOUT_SECONDS,
    ) -> List[FetchedPage]:
        """Fetch many URLs in parallel. Returns one FetchedPage per input URL,
        in the same order. Failures appear as pages with `.error` set; they
        never raise so the model can keep going.
        """
        if not isinstance(urls, (list, tuple)):
            raise TypeError("urls must be a list")
        urls = [str(u) for u in urls]
        if len(urls) > MAX_BATCH_URLS:
            raise ValueError(f"batch_get supports at most {MAX_BATCH_URLS} URLs per call")
        concurrency = max(1, min(int(concurrency or DEFAULT_BATCH_CONCURRENCY), MAX_BATCH_CONCURRENCY))
        timeout = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_BATCH_TIMEOUT_SECONDS
        if not urls:
            return []
        pages = asyncio.run(_batch_get_async(urls, concurrency, timeout))
        errors = sum(1 for p in pages if not p.success or p.error)
        self._audit("http.batch_get", urls=urls, errors=errors)
        return pages

    async def _get_one(self, url: str, timeout: int) -> FetchedPage:
        async with cf_requests.AsyncSession(
            impersonate=IMPERSONATE_PROFILE, timeout=timeout
        ) as session:
            return await _fetch_one_async(session, url, timeout)

    def _audit(self, action: str, *, urls: List[str], errors: int) -> None:
        if self._audit_callback is None:
            return
        try:
            self._audit_callback(action, urls=urls, errors=errors)
        except Exception:
            logger.debug("safe_http: audit callback failed", exc_info=True)
