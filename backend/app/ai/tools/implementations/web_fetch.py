import ipaddress
import logging
import socket
from typing import AsyncIterator, Dict, Any, Type
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.ai.tools.base import Tool
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

MAX_RESPONSE_BYTES = 1_000_000
MAX_TEXT_CHARS = 200_000
REQUEST_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5
USER_AGENT = "bagofwords-agent/1.0"
ALLOWED_CONTENT_TYPE_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/ld+json",
)


def _is_safe_host(hostname: str) -> bool:
    """Resolve hostname and reject if any resolved address is non-public.

    Blocks loopback, link-local, private networks, multicast, reserved ranges,
    and unspecified addresses (covers SSRF against cloud metadata services,
    internal infrastructure, and the host's own network).
    """
    if not hostname:
        return False
    lowered = hostname.lower()
    if lowered in ("localhost",) or lowered.endswith(".localhost"):
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


class WebFetchTool(Tool):
    """Fetch the contents of a public HTTP(S) URL."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="web_fetch",
            description="""
Purpose:
Fetch the textual contents of a public HTTP or HTTPS URL and return them
to the agent for analysis. Large bodies are truncated and only text-like
responses (HTML, JSON, XML, plain text) are returned.

Use when:
    - The user references a webpage, article, or public document by URL
    - You need to read a small piece of public web content to answer a question

Do not use when:
    - You need data from a SQL database (use create_data)
    - You need to read an uploaded file (use inspect_data)
    - You need to call an authenticated API (use execute_mcp)
            """,
            category="research",
            version="1.0.0",
            input_schema=WebFetchInput.model_json_schema(),
            output_schema=WebFetchOutput.model_json_schema(),
            tags=["web", "fetch", "http", "research"],
            timeout_seconds=REQUEST_TIMEOUT_SECONDS + 5,
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

        parsed = urlparse(data.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
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

        if not _is_safe_host(parsed.hostname):
            await log_tool_audit(
                runtime_ctx,
                action="tool.web_fetch_blocked_unsafe_host",
                resource_type="report",
                resource_id=report_id,
                details={"tool": "web_fetch", "hostname": parsed.hostname},
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        error_message="Refusing to fetch a non-public address.",
                    ).model_dump(),
                    "observation": {
                        "summary": "Blocked non-public host",
                        "success": False,
                    },
                },
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "fetching"})

        current_url = data.url
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    async with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                break
                            redirect_url = str(response.url.join(location))
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

                        content_type = response.headers.get("content-type", "").lower()
                        if not any(content_type.startswith(p) for p in ALLOWED_CONTENT_TYPE_PREFIXES):
                            yield ToolEndEvent(
                                type="tool.end",
                                payload={
                                    "output": WebFetchOutput(
                                        success=True,
                                        url=data.url,
                                        final_url=str(response.url),
                                        status_code=response.status_code,
                                        content_type=content_type or None,
                                        content=None,
                                        truncated=False,
                                        error_message=f"Skipped non-text content-type: {content_type or 'unknown'}.",
                                    ).model_dump(),
                                    "observation": {
                                        "summary": f"Fetched {response.status_code} ({content_type or 'unknown'}); body skipped",
                                        "success": True,
                                    },
                                },
                            )
                            return

                        chunks: list[bytes] = []
                        total = 0
                        truncated = False
                        async for chunk in response.aiter_bytes():
                            if total + len(chunk) > MAX_RESPONSE_BYTES:
                                chunks.append(chunk[: MAX_RESPONSE_BYTES - total])
                                truncated = True
                                break
                            chunks.append(chunk)
                            total += len(chunk)

                        encoding = response.encoding or "utf-8"
                        try:
                            text = b"".join(chunks).decode(encoding, errors="replace")
                        except LookupError:
                            text = b"".join(chunks).decode("utf-8", errors="replace")

                        if len(text) > MAX_TEXT_CHARS:
                            text = text[:MAX_TEXT_CHARS]
                            truncated = True

                        await log_tool_audit(
                            runtime_ctx,
                            action="tool.web_fetch_executed",
                            resource_type="report",
                            resource_id=report_id,
                            details={
                                "tool": "web_fetch",
                                "url": data.url,
                                "final_url": str(response.url),
                                "status_code": response.status_code,
                                "content_type": content_type,
                                "bytes": total,
                                "truncated": truncated,
                            },
                        )

                        yield ToolEndEvent(
                            type="tool.end",
                            payload={
                                "output": WebFetchOutput(
                                    success=True,
                                    url=data.url,
                                    final_url=str(response.url),
                                    status_code=response.status_code,
                                    content_type=content_type or None,
                                    content=text,
                                    truncated=truncated,
                                ).model_dump(),
                                "observation": {
                                    "summary": f"Fetched {response.status_code} ({content_type or 'unknown'}); {len(text)} chars{' (truncated)' if truncated else ''}",
                                    "success": True,
                                    "preview": text[:2000],
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
        except httpx.TimeoutException:
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
        except httpx.HTTPError as e:
            logger.warning(f"web_fetch: HTTP error for {data.url}: {e}")
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WebFetchOutput(
                        success=False,
                        url=data.url,
                        error_message=f"HTTP error: {e}",
                    ).model_dump(),
                    "observation": {"summary": f"HTTP error: {e}", "success": False},
                },
            )
