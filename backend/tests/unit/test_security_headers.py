"""Site-wide security headers (pentest 828432330).

Contract: every response carries nosniff, a referrer policy and a structural
CSP; framing is limited to this origin and Office on the web, except the
public /r/ and /c/ pages customers embed; HSTS only over HTTPS; a header a
route already set is never overwritten.
"""
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.core.security_headers import SecurityHeadersMiddleware


def _client():
    app = FastAPI()

    @app.get("/{path:path}")
    def page(path: str):
        if path == "mcp-artifact-app.html":
            return PlainTextResponse("x", headers={"Content-Security-Policy": "frame-ancestors 'none'"})
        return PlainTextResponse("x")

    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_app_pages_get_the_baseline_headers():
    r = _client().get("/reports/123")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = r.headers["content-security-policy"]
    assert "object-src 'none'" in csp and "base-uri 'self'" in csp
    assert "frame-ancestors 'self'" in csp and "https://*.officeapps.live.com" in csp
    assert "strict-transport-security" not in r.headers


def test_public_report_and_conversation_pages_stay_embeddable():
    for path in ("/r/abc", "/c/token"):
        csp = _client().get(path).headers["content-security-policy"]
        assert "frame-ancestors" not in csp


def test_hsts_only_over_https():
    r = _client().get("/", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")


def test_route_set_headers_win():
    r = _client().get("/mcp-artifact-app.html")
    assert r.headers.get_list("content-security-policy") == ["frame-ancestors 'none'"]
