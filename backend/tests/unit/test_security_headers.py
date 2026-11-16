"""Site-wide security headers (pentest 828432330).

Contract: every response carries nosniff, a referrer policy and a structural
CSP; framing is limited to this origin and Office on the web, except the
public /r/ and /c/ pages customers embed; HSTS only over HTTPS; a header a
route already set is never overwritten. An install may add its own origins to
the framing list; only plain origins are accepted.
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


def test_configured_origins_are_added_to_the_default_list():
    sources = _ancestors(_client(["https://erp.example.com", "https://*.example.org:8443"]).get("/authorize"))
    assert sources[:2] == ["'self'", "https://*.office.com"]
    assert "https://erp.example.com" in sources and "https://*.example.org:8443" in sources


def test_configured_origins_are_normalized_and_deduplicated():
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


def _configured_app(monkeypatch, config_list, saved):
    from app.core import security_headers
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.security, "frame_ancestors", config_list)

    monkeypatch.setattr(security_headers, "_saved_frame_ancestors", lambda: tuple(saved))
    app = FastAPI()
    app.get("/{path:path}")(lambda path: PlainTextResponse("x"))
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_the_middleware_reads_the_install_setting(monkeypatch):
    client = _configured_app(monkeypatch, ["https://erp.example.com"], [])
    assert "https://erp.example.com" in _ancestors(client.get("/authorize"))


def test_sites_saved_in_settings_are_added_after_the_config_list(monkeypatch):
    client = _configured_app(monkeypatch, ["https://erp.example.com"], ["https://portal.example.org"])
    sources = _ancestors(client.get("/authorize"))
    assert sources[-2:] == ["https://erp.example.com", "https://portal.example.org"]


def test_saved_sites_never_reach_public_pages(monkeypatch):
    client = _configured_app(monkeypatch, [], ["https://portal.example.org"])
    assert "frame-ancestors" not in client.get("/r/abc").headers["content-security-policy"]


def test_the_env_var_fills_the_setting(monkeypatch):
    from app.settings.bow_config import SecurityConfig

    monkeypatch.setenv("BOW_FRAME_ANCESTORS", " https://erp.example.com , ,https://*.example.org ")
    assert SecurityConfig().frame_ancestors == ["https://erp.example.com", "https://*.example.org"]
    monkeypatch.delenv("BOW_FRAME_ANCESTORS")
    assert SecurityConfig().frame_ancestors == []


def test_yaml_values_win_over_the_env_var(monkeypatch):
    from app.settings.bow_config import SecurityConfig

    monkeypatch.setenv("BOW_FRAME_ANCESTORS", "https://env.example.com")
    assert SecurityConfig(frame_ancestors=["https://yaml.example.com"]).frame_ancestors == ["https://yaml.example.com"]


def test_ports_are_written_without_leading_zeros():
    assert frame_ancestor_source("https://erp.example.com:00443") == "https://erp.example.com:443"


@pytest.mark.parametrize("raw, expected", [
    (None, []),
    ("https://erp.example.com", ["https://erp.example.com"]),
    ("https://a.example.com, https://b.example.com", ["https://a.example.com", "https://b.example.com"]),
    ([42, None, "https://erp.example.com"], ["42", "None", "https://erp.example.com"]),
])
def test_a_config_typo_does_not_stop_the_server(raw, expected):
    from app.settings.bow_config import SecurityConfig

    assert SecurityConfig(frame_ancestors=raw).frame_ancestors == expected


def test_non_string_config_items_end_up_rejected_not_applied():
    from app.settings.bow_config import SecurityConfig

    configured = SecurityConfig(frame_ancestors=[42, "https://erp.example.com"]).frame_ancestors
    sources = _ancestors(_client(configured).get("/"))
    assert "https://erp.example.com" in sources and "42" not in sources


def test_health_probes_never_consult_the_saved_list(monkeypatch):
    from app.core import security_headers

    def _fail():
        raise AssertionError("/health must not read the saved list")

    client = _configured_app(monkeypatch, [], ["https://portal.example.org"])
    monkeypatch.setattr(security_headers, "_saved_frame_ancestors", _fail)
    for path in ("/health", "/health/ready"):
        assert "https://portal.example.org" not in _ancestors(client.get(path))
