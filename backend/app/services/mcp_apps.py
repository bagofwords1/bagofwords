"""MCP Apps host support (extension ``io.modelcontextprotocol/ui``).

An MCP server can attach an interactive UI to a tool: the tool's
``_meta.ui.resourceUri`` names a ``ui://`` resource holding an HTML document
(MIME ``text/html;profile=mcp-app``) that the host renders next to the tool
call, in a sandboxed iframe. BOW is that host in report chat.

This module is the server side of it:

* reading a tool's UI binding and visibility out of its ``_meta``
  (``parse_tool_ui_meta``), and the visibility rules the spec makes MUST:
  an app-only tool never reaches the model (``model_visible``) and an app
  may only call tools that list ``"app"`` (``app_callable``);
* reading a ``ui://`` resource and its ``_meta.ui`` (CSP domains, iframe
  permissions, border preference), validating every declared domain, and
  turning it into the Content-Security-Policy the sandbox is served with
  (``build_app_csp``).

Spec: https://github.com/modelcontextprotocol/ext-apps (specification/2026-01-26).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"

VISIBILITY_MODEL = "model"
VISIBILITY_APP = "app"
DEFAULT_VISIBILITY = [VISIBILITY_MODEL, VISIBILITY_APP]

# A UI document is HTML, scripts usually inlined. Generous, but bounded: it is
# held in memory, sent to the browser and parsed there.
MAX_UI_HTML_BYTES = 4 * 1024 * 1024

# Iframe permissions the spec defines (``_meta.ui.permissions``) and the
# Permissions-Policy feature each one maps to on the iframe's ``allow``.
PERMISSION_FEATURES = {
    "camera": "camera",
    "microphone": "microphone",
    "geolocation": "geolocation",
    "clipboardWrite": "clipboard-write",
}

_CSP_KEYS = ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")
_MAX_DOMAINS_PER_KEY = 32

# scheme://host[:port], host may start with "*." (the spec allows wildcard
# subdomains). Nothing else: no paths, quotes, semicolons or whitespace, so a
# declared domain can never break out of its CSP directive.
_DOMAIN_RE = re.compile(
    r"^(https|http|wss|ws)://(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$"
)


# ── Tool metadata ───────────────────────────────────────────────────────────


def parse_tool_ui_meta(meta: Any) -> Optional[Dict[str, Any]]:
    """The UI binding a tool declares in ``_meta``, or None.

    Returns ``{"resourceUri": "ui://…", "visibility": [...]}``; either key may
    be absent (an app-only helper tool often has a visibility and no UI of its
    own). Reads the deprecated flat ``_meta["ui/resourceUri"]`` as a fallback.
    """
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    out: Dict[str, Any] = {}

    uri = ui.get("resourceUri")
    if not isinstance(uri, str):
        uri = meta.get("ui/resourceUri")
    if isinstance(uri, str) and uri.startswith("ui://") and len(uri) <= 2048:
        out["resourceUri"] = uri

    vis = ui.get("visibility")
    if isinstance(vis, list):
        cleaned = [v for v in vis if v in (VISIBILITY_MODEL, VISIBILITY_APP)]
        # De-duplicate, keep order; an explicit empty list means "nobody".
        seen: List[str] = []
        for v in cleaned:
            if v not in seen:
                seen.append(v)
        out["visibility"] = seen
    return out or None


def tool_ui(metadata_json: Any) -> Optional[Dict[str, Any]]:
    """The stored UI binding of a ConnectionTool (its ``metadata_json["ui"]``)."""
    if not isinstance(metadata_json, dict):
        return None
    ui = metadata_json.get("ui")
    return ui if isinstance(ui, dict) else None


def tool_visibility(metadata_json: Any) -> List[str]:
    ui = tool_ui(metadata_json) or {}
    vis = ui.get("visibility")
    return list(vis) if isinstance(vis, list) else list(DEFAULT_VISIBILITY)


def model_visible(metadata_json: Any) -> bool:
    """May the model see / call this tool? False for app-only tools."""
    return VISIBILITY_MODEL in tool_visibility(metadata_json)


def app_callable(metadata_json: Any) -> bool:
    """May an MCP App call this tool? False for model-only tools."""
    return VISIBILITY_APP in tool_visibility(metadata_json)


def tool_resource_uri(metadata_json: Any) -> Optional[str]:
    uri = (tool_ui(metadata_json) or {}).get("resourceUri")
    return uri if isinstance(uri, str) and uri.startswith("ui://") else None


# ── Resource metadata & CSP ─────────────────────────────────────────────────


def clean_domains(values: Any) -> List[str]:
    """The declared origins that are well-formed; the rest are dropped (and logged)."""
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for v in values[: _MAX_DOMAINS_PER_KEY * 2]:
        if not isinstance(v, str):
            continue
        d = v.strip().lower().rstrip("/")
        if _DOMAIN_RE.match(d) and d not in out:
            out.append(d)
        else:
            logger.info("mcp_apps: dropping malformed CSP domain %r", v[:200])
        if len(out) >= _MAX_DOMAINS_PER_KEY:
            break
    return out


def parse_resource_ui_meta(*metas: Any) -> Dict[str, Any]:
    """Merge ``_meta.ui`` from a resource's content item and its listing entry.

    The spec says the content item wins and the listing is the fallback, so
    pass them in that order. Returns ``{csp, permissions, prefersBorder,
    domain}`` with every domain validated.
    """
    csp: Dict[str, List[str]] = {}
    permissions: List[str] = []
    prefers_border: Optional[bool] = None
    domain: Optional[str] = None
    for meta in metas:
        ui = meta.get("ui") if isinstance(meta, dict) and isinstance(meta.get("ui"), dict) else None
        if ui is None:
            continue
        raw_csp = ui.get("csp") if isinstance(ui.get("csp"), dict) else {}
        for key in _CSP_KEYS:
            if key not in csp and key in raw_csp:
                csp[key] = clean_domains(raw_csp.get(key))
        if not permissions and isinstance(ui.get("permissions"), dict):
            permissions = [k for k in ui["permissions"] if k in PERMISSION_FEATURES]
        if prefers_border is None and isinstance(ui.get("prefersBorder"), bool):
            prefers_border = ui["prefersBorder"]
        if domain is None and isinstance(ui.get("domain"), str):
            domain = ui["domain"][:253]
    return {
        "csp": {k: csp.get(k, []) for k in _CSP_KEYS},
        "permissions": permissions,
        "prefersBorder": prefers_border,
        "domain": domain,
    }


def build_app_csp(csp: Optional[Dict[str, Any]]) -> str:
    """The Content-Security-Policy an MCP App document runs under.

    Starts from the spec's restrictive default (no network, inline script and
    style only) and opens exactly the origins the resource declared:
    ``connectDomains`` → connect-src, ``resourceDomains`` → script/style/img/
    font/media, ``frameDomains`` → frame-src, ``baseUriDomains`` → base-uri.
    Domains are re-validated here so a caller can pass untrusted input.
    """
    csp = csp if isinstance(csp, dict) else {}
    connect = clean_domains(csp.get("connectDomains"))
    resources = clean_domains(csp.get("resourceDomains"))
    frames = clean_domains(csp.get("frameDomains"))
    base_uri = clean_domains(csp.get("baseUriDomains"))

    SELF, INLINE, EVAL, NONE = "'self'", "'unsafe-inline'", "'unsafe-eval'", "'none'"

    def src(*sources: str, extra: Iterable[str] = ()) -> str:
        return " ".join([*sources, *extra])

    directives = [
        f"default-src {NONE}",
        "script-src " + src(SELF, INLINE, EVAL, "blob:", extra=resources),
        "style-src " + src(SELF, INLINE, extra=resources),
        "img-src " + src(SELF, "data:", "blob:", extra=resources),
        "font-src " + src(SELF, "data:", extra=resources),
        "media-src " + src(SELF, "data:", "blob:", extra=resources),
        "connect-src " + (src(extra=connect) if connect else NONE),
        "frame-src " + (src(extra=frames) if frames else NONE),
        "base-uri " + (src(extra=base_uri) if base_uri else SELF),
        "worker-src " + src(SELF, "blob:"),
        f"object-src {NONE}",
        f"form-action {NONE}",
    ]
    return "; ".join(directives)


def permissions_allow_attr(permissions: Any) -> str:
    """The iframe ``allow`` attribute for the requested permissions."""
    if not isinstance(permissions, list):
        return ""
    return "; ".join(PERMISSION_FEATURES[p] for p in permissions if p in PERMISSION_FEATURES)


# ── Reading the UI resource ─────────────────────────────────────────────────


class McpAppResourceError(Exception):
    """The ui:// resource could not be used (missing, wrong type, too large)."""


# (connection id, credentials digest, uri) → (expires_at, payload). Short TTL:
# servers ship new UI versions, and a stale one would be visible for minutes.
_CACHE: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_S = 60.0
_CACHE_MAX = 128


def clear_resource_cache() -> None:
    _CACHE.clear()


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    _CACHE.pop(key, None)
    return None


def _cache_put(key, payload) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: len(_CACHE) - _CACHE_MAX + 1]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_S, payload)


def html_from_contents(contents: List[Dict[str, Any]], uri: str) -> Tuple[str, Dict[str, Any]]:
    """Pick the app document out of a resources/read result.

    Returns ``(html, content_item_meta)``. Raises McpAppResourceError when
    no item is an MCP App document (wrong MIME type), or it is too large.
    """
    import base64

    for item in contents or []:
        mime = (item.get("mime_type") or "").replace(" ", "").lower()
        if not mime.startswith("text/html") or "profile=mcp-app" not in mime:
            continue
        if item.get("type") == "binary":
            try:
                raw = base64.b64decode(item.get("blob_b64") or "", validate=False)
            except Exception:
                raise McpAppResourceError("The app resource is not valid base64.")
            if len(raw) > MAX_UI_HTML_BYTES:
                raise McpAppResourceError("The app resource is too large.")
            html = raw.decode("utf-8", errors="replace")
        else:
            html = item.get("text") or ""
            if len(html.encode("utf-8")) > MAX_UI_HTML_BYTES:
                raise McpAppResourceError("The app resource is too large.")
        if not html.strip():
            raise McpAppResourceError("The app resource is empty.")
        return html, item.get("meta") or {}
    raise McpAppResourceError(
        f"{uri} is not an MCP App document (expected {MCP_APP_MIME_TYPE})."
    )


async def read_app_resource(client: Any, connection_id: str, uri: str) -> Dict[str, Any]:
    """Read a ``ui://`` resource through an MCP client and shape it for the host.

    Returns ``{uri, html, csp, permissions, prefersBorder, domain}``. The
    listing entry's ``_meta.ui`` is consulted only when the content item has
    none (the spec's fallback order), and only if the server lists it.
    """
    if not isinstance(uri, str) or not uri.startswith("ui://"):
        raise McpAppResourceError("MCP App resources must use the ui:// scheme.")
    try:
        digest = client._build_headers()  # same credentials → same cached copy
        digest = json.dumps(sorted(digest.items()))
    except Exception:
        digest = ""
    import hashlib
    key = (str(connection_id), hashlib.sha256(digest.encode()).hexdigest(), uri)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = await client.aread_resource(uri)
    if not result.get("success"):
        raise McpAppResourceError(result.get("error") or "Could not read the app resource.")
    html, item_meta = html_from_contents(result.get("contents") or [], uri)

    list_meta: Dict[str, Any] = {}
    if not (isinstance(item_meta, dict) and isinstance(item_meta.get("ui"), dict)):
        try:
            for r in await client.alist_resources():
                if r.get("uri") == uri and isinstance(r.get("meta"), dict):
                    list_meta = r["meta"]
                    break
        except Exception:
            pass  # resources/list is optional for UI-only resources

    ui = parse_resource_ui_meta(item_meta, list_meta)
    payload = {"uri": uri, "html": html, **ui}
    _cache_put(key, payload)
    return payload
