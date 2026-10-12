import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Type
from urllib.parse import urlparse, urljoin

from curl_cffi import requests as cf_requests
from curl_cffi.requests.exceptions import RequestException, Timeout
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from app.ai.tools.base import Tool
from app.ai.http.safe_client import is_safe_host, pinned_curl_get
from app.core.url_safety import UnsafeURLError
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.web_fetch import WebFetchInput, WebFetchOutput
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ee.audit.tool_audit import log_tool_audit

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
REQUEST_TIMEOUT_SECONDS = 30
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


def _is_safe_host(hostname: str) -> bool:
    """True only if every address the hostname resolves to is globally routable
    (rejects private, loopback, link-local, CGNAT, reserved and metadata
    addresses). Used to report a refused redirect with its target; the fetch
    itself is guarded and pinned by ``pinned_curl_get`` (app.core.egress)."""
    return is_safe_host(hostname)


def _content_type_matches(content_type: str, prefixes: Tuple[str, ...]) -> bool:
    ct = (content_type or "").lower()
    return any(ct.startswith(p) for p in prefixes)


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def _parse_html(html: str) -> Dict[str, Any]:
    """Extract structured signal + readable text from an HTML document.

    Returns a dict with title/description/metadata/json_ld/headings/text keys.
    Any individual extraction failure is swallowed; the body text path is
    best-effort so the model always gets *something* readable.
    """
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


class WebFetchTool(Tool):
    """Fetch the contents of a public HTTP(S) URL."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="web_fetch",
            description="""
Purpose:
Fetch a public HTTP or HTTPS URL and return readable content. For HTML
pages, the agent receives the page title, meta tags (including OpenGraph
and Twitter Cards), any JSON-LD blocks (often containing Product /
NewsArticle / Recipe data), top headings, and the visible body text with
scripts/styles stripped. For JSON / XML / plain text URLs, the raw body
is returned.

Use when:
    - The user references a webpage, article, or public document by URL
    - You need to read a small piece of public web content to answer a question

Do not use when:
    - You need data from a SQL database (use create_data)
    - You need to read an uploaded file (use inspect_data)
    - You need to call an authenticated API (use execute_mcp)
            """,
            category="research",
            version="2.0.0",
            input_schema=WebFetchInput.model_json_schema(),
            output_schema=WebFetchOutput.model_json_schema(),
            tags=["web", "fetch", "http", "research"],
            timeout_seconds=REQUEST_TIMEOUT_SECONDS + 35,
            idempotent=True,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WebFetchInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WebFetchOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = WebFetchInput(**tool_input)
        report = runtime_ctx.get("report")
        report_id = str(report.id) if report else None

        organization_settings = runtime_ctx.get("settings")
        if organization_settings:
            enable_web_fetch = organization_settings.get_config("enable_web_fetch")
            if not enable_web_fetch or not enable_web_fetch.value:
                await log_tool_audit(
                    runtime_ctx,
                    action="tool.access_blocked_by_policy",
                    resource_type="report",
                    resource_id=report_id,
                    details={"tool": "web_fetch", "policy": "enable_web_fetch"},
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": WebFetchOutput(
                            success=False,
                            url=data.url,
                            error_message="Web fetch is disabled for this organization.",
                        ).model_dump(),
                        "observation": {
                            "summary": "web_fetch blocked: enable_web_fetch is disabled",
                            "success": False,
                        },
                    },
                )
                return
        else:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        error_message="Web fetch is unavailable (missing organization settings).",
                    ).model_dump(),
                    "observation": {"summary": "Missing settings context", "success": False},
                },
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"title": f"Fetching {data.url}", "url": data.url},
        )

        parsed_url = urlparse(data.url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        error_message="URL must use http or https and include a hostname.",
                    ).model_dump(),
                    "observation": {"summary": "Invalid URL", "success": False},
                },
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "fetching"})

        current_url = data.url
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                try:
                    # Resolve once, validate every address, connect to the
                    # validated one: a rebinding DNS answer is never used.
                    response = await pinned_curl_get(current_url, timeout=REQUEST_TIMEOUT_SECONDS)
                except UnsafeURLError:
                    await log_tool_audit(
                        runtime_ctx,
                        action="tool.web_fetch_blocked_unsafe_host",
                        resource_type="report",
                        resource_id=report_id,
                        details={
                            "tool": "web_fetch",
                            "hostname": urlparse(current_url).hostname,
                            "via_redirect": _hop > 0,
                        },
                    )
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": WebFetchOutput(
                                success=False,
                                url=data.url,
                                final_url=current_url if _hop > 0 else None,
                                error_message=(
                                    "Refusing to follow redirect to a non-public address."
                                    if _hop > 0 else "Refusing to fetch a non-public address."
                                ),
                            ).model_dump(),
                            "observation": {"summary": "Blocked non-public host", "success": False},
                        },
                    )
                    return
                status = response.status_code

                if 300 <= status < 400 and status != 304:
                    location = response.headers.get("location")
                    if not location:
                        break
                    redirect_url = urljoin(current_url, location)
                    redirect_host = urlparse(redirect_url).hostname
                    if not redirect_host or not _is_safe_host(redirect_host):
                        await log_tool_audit(
                            runtime_ctx,
                            action="tool.web_fetch_blocked_unsafe_host",
                            resource_type="report",
                            resource_id=report_id,
                            details={
                                "tool": "web_fetch",
                                "hostname": redirect_host,
                                "via_redirect": True,
                            },
                        )
                        yield ToolEndEvent(
                            type="tool.end",
                            payload={
                                "output": WebFetchOutput(
                                    success=False,
                                    url=data.url,
                                    final_url=redirect_url,
                                    error_message="Refusing to follow redirect to a non-public address.",
                                ).model_dump(),
                                "observation": {
                                    "summary": "Blocked redirect to non-public host",
                                    "success": False,
                                },
                            },
                        )
                        return
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

                if not is_text:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": WebFetchOutput(
                                success=True,
                                url=data.url,
                                final_url=final_url,
                                status_code=status,
                                content_type=content_type or None,
                                content=None,
                                truncated=False,
                                error_message=f"Skipped non-text content-type: {content_type or 'unknown'}.",
                            ).model_dump(),
                            "observation": {
                                "summary": f"Fetched {status} ({content_type or 'unknown'}); body skipped",
                                "success": True,
                            },
                        },
                    )
                    return

                encoding = response.encoding or "utf-8"
                try:
                    body_text = body_bytes.decode(encoding, errors="replace")
                except LookupError:
                    body_text = body_bytes.decode("utf-8", errors="replace")

                output = WebFetchOutput(
                    success=True,
                    url=data.url,
                    final_url=final_url,
                    status_code=status,
                    content_type=content_type or None,
                    content=None,
                    truncated=truncated,
                )
                summary_extras: List[str] = []

                if is_html:
                    try:
                        parsed = _parse_html(body_text)
                        output.title = parsed["title"]
                        output.description = parsed["description"]
                        output.metadata = parsed["metadata"] or None
                        output.json_ld = parsed["json_ld"] or None
                        output.headings = parsed["headings"] or None
                        text_body = parsed["text"] or ""
                        if len(text_body) > MAX_TEXT_CHARS:
                            text_body = text_body[:MAX_TEXT_CHARS]
                            output.truncated = True
                        output.content = text_body
                        if output.title:
                            summary_extras.append(f'title="{output.title[:60]}"')
                        if output.json_ld:
                            summary_extras.append(f"{len(output.json_ld)} json-ld block(s)")
                    except Exception as exc:
                        logger.warning(f"web_fetch: HTML parse failed for {final_url}: {exc}")
                        fallback = body_text
                        if len(fallback) > MAX_TEXT_CHARS:
                            fallback = fallback[:MAX_TEXT_CHARS]
                            output.truncated = True
                        output.content = fallback
                        summary_extras.append("parse-failed; raw HTML")
                else:
                    if len(body_text) > MAX_TEXT_CHARS:
                        body_text = body_text[:MAX_TEXT_CHARS]
                        output.truncated = True
                    output.content = body_text

                await log_tool_audit(
                    runtime_ctx,
                    action="tool.web_fetch_executed",
                    resource_type="report",
                    resource_id=report_id,
                    details={
                        "tool": "web_fetch",
                        "url": data.url,
                        "final_url": final_url,
                        "status_code": status,
                        "content_type": content_type,
                        "bytes": len(body_bytes),
                        "truncated": output.truncated,
                        "is_html": is_html,
                    },
                )

                summary = f"Fetched {status} ({content_type or 'unknown'}); {len(output.content or '')} chars"
                if output.truncated:
                    summary += " (truncated)"
                if summary_extras:
                    summary += " · " + " · ".join(summary_extras)

                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {
                            "summary": summary,
                            "success": True,
                            "url": data.url,
                            "final_url": final_url,
                            "status_code": status,
                            "title": output.title,
                            "description": output.description,
                            "content": output.content,
                            "metadata": output.metadata,
                            "json_ld": output.json_ld,
                            "headings": output.headings,
                            "truncated": output.truncated,
                        },
                    },
                )
                return

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        final_url=current_url,
                        error_message=f"Too many redirects (limit {MAX_REDIRECTS}).",
                    ).model_dump(),
                    "observation": {"summary": "Too many redirects", "success": False},
                },
            )
        except Timeout:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        error_message=f"Request timed out after {REQUEST_TIMEOUT_SECONDS}s.",
                    ).model_dump(),
                    "observation": {"summary": "Timeout", "success": False},
                },
            )
        except RequestException as e:
            logger.warning(f"web_fetch: request error for {data.url}: {e}")
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        error_message=f"Request failed: {e}",
                    ).model_dump(),
                    "observation": {"summary": f"Request failed: {e}", "success": False},
                },
            )
