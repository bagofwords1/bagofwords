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
"""
from __future__ import annotations

FRAMEABLE_PREFIXES = ("/r/", "/c/")

BASE_CSP = "object-src 'none'; base-uri 'self'"
FRAME_ANCESTORS = (
    "frame-ancestors 'self' https://*.office.com https://*.officeapps.live.com "
    "https://*.office365.com https://*.sharepoint.com https://*.microsoft365.com"
)
HSTS = "max-age=31536000; includeSubDomains"


def _is_https(scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-proto":
            return value.split(b",")[0].strip().lower() == b"https"
    return False


def security_headers_for(path: str, https: bool) -> dict[str, str]:
    frameable = any(path.startswith(p) for p in FRAMEABLE_PREFIXES)
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": BASE_CSP if frameable else f"{BASE_CSP}; {FRAME_ANCESTORS}",
    }
    if https:
        headers["Strict-Transport-Security"] = HSTS
    return headers


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        extra = security_headers_for(scope.get("path", ""), _is_https(scope))

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
