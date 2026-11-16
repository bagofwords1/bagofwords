"""Site-wide security headers (pentest 828432330).

Contract: every response carries nosniff, a referrer policy and a structural
CSP; framing is limited to this origin and Office on the web, except the
public /r/ and /c/ pages customers embed; HSTS only over HTTPS; a header a
route already set is never overwritten. Admins may add their own sites to the
framing list (Settings → General); only plain origins are accepted.
"""
import re

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.core.security_headers import FRAME_ANCESTORS, SecurityHeadersMiddleware, frame_ancestor_source


def _client(frame_ancestors=None):
    app = FastAPI()

    @app.get("/{path:path}")
    def page(path: str):
        if path == "mcp-artifact-app.html":
            return PlainTextResponse("x", headers={"Content-Security-Policy": "frame-ancestors 'none'"})
        return PlainTextResponse("x")

    app.add_middleware(SecurityHeadersMiddleware, frame_ancestors=frame_ancestors)
    return TestClient(app)


def _ancestors(response) -> list[str]:
    m = re.search(r"frame-ancestors ([^;]+)", response.headers["content-security-policy"])
    return m.group(1).split() if m else []


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


def test_without_extra_origins_the_policy_is_unchanged():
    csp = _client(frame_ancestors=[]).get("/reports/123").headers["content-security-policy"]
    assert csp == f"object-src 'none'; base-uri 'self'; {FRAME_ANCESTORS}"


def test_extra_origins_are_added_to_the_default_list():
    sources = _ancestors(_client(["https://erp.example.com", "https://*.example.org:8443"]).get("/authorize"))
    assert sources[:2] == ["'self'", "https://*.office.com"]
    assert "https://erp.example.com" in sources and "https://*.example.org:8443" in sources


def test_extra_origins_are_normalized_and_deduplicated():
    sources = _ancestors(_client([" HTTPS://ERP.Example.com/ ", "https://erp.example.com"]).get("/"))
    assert sources.count("https://erp.example.com") == 1


def test_public_pages_stay_embeddable_with_extra_origins():
    csp = _client(["https://erp.example.com"]).get("/r/abc").headers["content-security-policy"]
    assert "frame-ancestors" not in csp


def test_route_set_headers_still_win_with_extra_origins():
    r = _client(["https://erp.example.com"]).get("/mcp-artifact-app.html")
    assert r.headers.get_list("content-security-policy") == ["frame-ancestors 'none'"]


@pytest.mark.parametrize("value", [
    "*", "https://*", "https://*.com", "*.example.com", "example.com",
    "http://erp.example.com", "http://*.localhost",
    "https://erp.example.com/path", "https://erp.example.com?x=1", "https://erp.example.com#x",
    "https://erp.example.com; script-src *", "https://a.com https://b.com",
    "'unsafe-inline'", "'self'", "'none'", "data:", "javascript:alert(1)",
    "https://erp.example.com:0", "https://erp.example.com:70000", "https://user@erp.example.com",
    "https://erp_example.com", "", "   ", None, 42,
    "https://*.github.io", "https://*.myorg.github.io", "https://*.azurewebsites.net",
    "https://*.vercel.app", "https://*.co.uk", "https://*.co.il", "https://*.com.au",
])
def test_anything_but_a_plain_origin_is_rejected(value):
    assert frame_ancestor_source(value) is None


@pytest.mark.parametrize("value", [
    "http://localhost:3000", "http://127.0.0.1:8080", "https://intranet", "https://erp.example.com:*",
    "https://*.example.com", "https://*.example.co.uk", "https://myorg.github.io",
])
def test_local_http_intranet_hosts_and_any_port_are_accepted(value):
    assert frame_ancestor_source(value) == value


def test_rejected_entries_are_dropped_and_the_rest_still_apply(monkeypatch):
    from app.core import security_headers

    warned = []
    monkeypatch.setattr(security_headers.logger, "warning", lambda msg, *args: warned.append(msg % args))
    sources = _ancestors(_client(["https://erp.example.com; script-src *", "https://ok.example.com"]).get("/"))
    assert "https://ok.example.com" in sources
    assert not any("script-src" in s or s == "*" for s in sources)
    assert len(warned) == 1 and "script-src" in warned[0]


def _app_with_saved(monkeypatch, saved):
    from app.core import security_headers

    monkeypatch.setattr(security_headers, "_saved_frame_ancestors", lambda: tuple(saved))
    app = FastAPI()
    app.get("/{path:path}")(lambda path: PlainTextResponse("x"))
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_sites_saved_in_settings_are_added_to_the_default_list(monkeypatch):
    sources = _ancestors(_app_with_saved(monkeypatch, ["https://portal.example.org"]).get("/authorize"))
    assert sources[:2] == ["'self'", "https://*.office.com"]
    assert sources[-1] == "https://portal.example.org"


def test_with_nothing_saved_the_policy_is_unchanged(monkeypatch):
    csp = _app_with_saved(monkeypatch, []).get("/authorize").headers["content-security-policy"]
    assert csp == f"object-src 'none'; base-uri 'self'; {FRAME_ANCESTORS}"


def test_saved_sites_never_reach_public_pages(monkeypatch):
    client = _app_with_saved(monkeypatch, ["https://portal.example.org"])
    assert "frame-ancestors" not in client.get("/r/abc").headers["content-security-policy"]


def test_ports_are_written_without_leading_zeros():
    assert frame_ancestor_source("https://erp.example.com:00443") == "https://erp.example.com:443"


def test_health_probes_never_consult_the_saved_list(monkeypatch):
    from app.core import security_headers

    def _fail():
        raise AssertionError("/health must not read the saved list")

    client = _app_with_saved(monkeypatch, ["https://portal.example.org"])
    monkeypatch.setattr(security_headers, "_saved_frame_ancestors", _fail)
    for path in ("/health", "/health/ready"):
        assert "https://portal.example.org" not in _ancestors(client.get(path))
