"""Site-wide HTTP security headers (pentest 828432330).

Raw ASGI so it covers API routes, the SPA fallback and static files alike.
A header a route already set wins (the artifact shells send
``frame-ancestors 'none'``, file embeds send their own CSP), so this only
fills gaps.

The CSP is deliberately structural — it restricts framing, plugins and
``<base>`` — and leaves script/style sources alone: Nuxt's inline bootstrap,
chart libraries and LLM-built artifact HTML would need a nonce rollout first.

Public report and conversation pages (``/r/…``, ``/c/…``) stay frameable from
any origin because customers embed shared reports on their own sites. Every
other page may be framed by this origin and by Office on the web: the Excel
add-in (routes/excel.py) loads the app in an iframe inside Office's frame, and
``frame-ancestors`` checks every ancestor. No ``X-Frame-Options`` for the same
reason — it cannot express that allowlist, and ``frame-ancestors`` supersedes it.

An install can add its own origins to that list for an internal application
that hosts BOW in a frame: in the config file (``security.frame_ancestors`` /
``BOW_FRAME_ANCESTORS``) or, by an admin, in Settings → General. Each entry goes straight into a response header, so only a plain
origin is accepted; anything else is dropped with a warning rather than
stopping the server.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

FRAMEABLE_PREFIXES = ("/r/", "/c/")

BASE_CSP = "object-src 'none'; base-uri 'self'"
DEFAULT_FRAME_ANCESTORS = (
    "'self'",
    "https://*.office.com",
    "https://*.officeapps.live.com",
    "https://*.office365.com",
    "https://*.sharepoint.com",
    "https://*.microsoft365.com",
)
FRAME_ANCESTORS = "frame-ancestors " + " ".join(DEFAULT_FRAME_ANCESTORS)
HSTS = "max-age=31536000; includeSubDomains"

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN = re.compile(rf"^(https?)://(\*\.)?({_LABEL}(?:\.{_LABEL})*)(?::(\d{{1,5}}|\*))?$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# Domains where anyone can publish a site of their own: "*." over one of these
# would let any of those sites frame the sign-in page.
_SHARED_HOSTING = {
    "github.io", "gitlab.io", "vercel.app", "netlify.app", "pages.dev", "workers.dev",
    "web.app", "firebaseapp.com", "appspot.com", "herokuapp.com", "onrender.com",
    "azurewebsites.net", "azurestaticapps.net", "cloudapp.net", "windows.net",
    "cloudfront.net", "amazonaws.com", "blogspot.com", "glitch.me", "ngrok.io", "ngrok-free.app",
}
# Second-level registries under a country code (co.uk, com.au, co.il, ...).
_REGISTRY_LABELS = {"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "go"}


def frame_ancestor_source(value) -> str | None:
    """The CSP source for one configured origin, or None when it is not a
    plain origin: https (http only for localhost), an optional leading
    ``*.`` over a domain someone owns (not a shared-hosting or registry
    suffix), an optional port, no path or keyword."""
    if not isinstance(value, str):
        return None
    source = value.strip().lower()
    if source.endswith("/"):
        source = source[:-1]
    match = _ORIGIN.match(source)
    if not match:
        return None
    scheme, wildcard, host, port = match.groups()
    if scheme == "http" and (wildcard or host not in _LOCAL_HOSTS):
        return None
    if wildcard and ("." not in host or _is_shared_suffix(host)):
        return None
    if port and port != "*":
        if not 0 < int(port) <= 65535:
            return None
        port = str(int(port))
    return f"{scheme}://{wildcard or ''}{host}" + (f":{port}" if port else "")


def _is_shared_suffix(host: str) -> bool:
    labels = host.split(".")
    if any(host == d or host.endswith("." + d) for d in _SHARED_HOSTING):
        return True
    return len(labels) == 2 and labels[0] in _REGISTRY_LABELS


def frame_ancestors_directive(extra=None) -> str:
    """``frame-ancestors`` with this origin, Office on the web and the
    install's valid extra origins (deduplicated, in configured order)."""
    sources = list(DEFAULT_FRAME_ANCESTORS)
    for value in extra or ():
        source = frame_ancestor_source(value)
        if source is None:
            logger.warning("Ignoring security.frame_ancestors entry %r: expected an https origin you control, "
                           "e.g. https://erp.example.com or https://*.example.com", value)
        elif source not in sources:
            sources.append(source)
    return "frame-ancestors " + " ".join(sources)


def _configured_frame_ancestors() -> list:
    try:
        from app.settings.config import settings

        return list(settings.bow_config.security.frame_ancestors or ())
    except Exception:
        logger.exception("Could not read security.frame_ancestors; using the default frame policy")
        return []


def _is_https(scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-proto":
            return value.split(b",")[0].strip().lower() == b"https"
    return False


def security_headers_for(path: str, https: bool, frame_ancestors: str = FRAME_ANCESTORS) -> dict[str, str]:
    frameable = any(path.startswith(p) for p in FRAMEABLE_PREFIXES)
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": BASE_CSP if frameable else f"{BASE_CSP}; {frame_ancestors}",
    }
    if https:
        headers["Strict-Transport-Security"] = HSTS
    return headers


# Liveness/readiness probes must never depend on the database, not even on a
# background read they would start: they keep the startup policy.
_STATIC_PREFIXES = ("/health",)


def _saved_frame_ancestors() -> tuple:
    """Never waits: frame_ancestors_service answers from memory and refreshes
    in the background."""
    try:
        from app.services.frame_ancestors_service import cached_origins

        return cached_origins()
    except Exception:
        logger.debug("Allowed framing sites unavailable; using the config list only", exc_info=True)
        return ()


class SecurityHeadersMiddleware:
    """``frame_ancestors`` given explicitly is the whole extra list (tests);
    otherwise it is the config file's list plus the sites admins save in
    Settings → General (services/frame_ancestors_service.py)."""

    def __init__(self, app, frame_ancestors=None):
        self.app = app
        self.dynamic = frame_ancestors is None
        configured = _configured_frame_ancestors() if frame_ancestors is None else frame_ancestors
        # Checked once here, so a bad config entry warns once, not per rebuild.
        self.frame_ancestors = frame_ancestors_directive(configured)
        self._configured = [s for s in (frame_ancestor_source(v) for v in configured or ()) if s]
        self._built = ((), self.frame_ancestors)

    def _directive(self, path: str) -> str:
        if not self.dynamic or path.startswith(_STATIC_PREFIXES):
            return self.frame_ancestors
        saved = _saved_frame_ancestors()
        if saved != self._built[0]:
            self._built = (saved, frame_ancestors_directive([*self._configured, *saved]))
        return self._built[1]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        extra = security_headers_for(path, _is_https(scope), self._directive(path))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in extra.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
