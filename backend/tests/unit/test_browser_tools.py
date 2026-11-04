"""Unit tests for the browser tools' security-critical pure functions.

The full browse/snapshot/screenshot flow is exercised e2e against a real
headless Chromium. Here we cover, without a browser or DB:
 - the URL-pattern grammar (validate_url_pattern) — what an admin may list
 - allowlist matching (url_matches_patterns) — incl. the userinfo trick,
   apex-vs-subdomain, and ports
 - link-local literal detection (cloud metadata is always refused)
 - connection resolution from a report (incl. double-JSON-encoded config)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.tools.implementations._browser_common import (
    BrowserSessionManager,
    _is_link_local_literal,
    _is_top_level_document,
    get_browser_connection,
    url_matches_patterns,
    validate_url_pattern,
)


class TestValidatePattern:
    @pytest.mark.parametrize("pat", [
        "https://portal.vendor.com/**",
        "https://*.vendor.com/**",
        "http://127.0.0.1:8777/**",
        "https://10.0.1.5/**",            # a single literal IP is fine
        "https://wiki.internal.corp/**",  # internal hostname is first-class
    ])
    def test_accepts(self, pat):
        assert validate_url_pattern(pat) is None

    @pytest.mark.parametrize("pat", [
        "",
        "ftp://vendor.com/**",            # wrong scheme
        "https:///path",                  # no host
        "https://*/**",                   # bare wildcard host
        "http://10.*.*.*/**",             # network-spanning host glob = scan
        "https://a.*.vendor.com/**",      # interior host wildcard
    ])
    def test_rejects(self, pat):
        assert validate_url_pattern(pat) is not None


class TestMatching:
    def test_basic(self):
        assert url_matches_patterns("https://example.com/x", ["https://example.com/**"])

    def test_off_allowlist(self):
        assert not url_matches_patterns("https://evil.com/", ["https://example.com/**"])

    def test_userinfo_trick_blocked(self):
        # host is evil.com, not portal.vendor.com
        assert not url_matches_patterns(
            "https://portal.vendor.com@evil.com/", ["https://portal.vendor.com/**"]
        )

    def test_subdomain_and_apex(self):
        pats = ["https://*.vendor.com/**"]
        assert url_matches_patterns("https://a.vendor.com/x", pats)
        assert url_matches_patterns("https://vendor.com/x", pats)     # apex too

    def test_port_preserved(self):
        assert url_matches_patterns("http://127.0.0.1:8777/", ["http://127.0.0.1:8777/**"])
        assert not url_matches_patterns("http://127.0.0.1:9999/", ["http://127.0.0.1:8777/**"])

    def test_query_string_url_self_matches(self):
        # A real URL with a query string, pasted as its own pattern, must match
        # itself — the '?' is literal, not a single-char wildcard.
        u = "https://shop.super-pharm.co.il/CARELINE/c/b_425?q=:popularity:brand:b_425"
        assert url_matches_patterns(u, [u])

    def test_question_mark_is_literal(self):
        # '?' in a pattern matches a literal '?', not "any one character".
        assert url_matches_patterns("https://x.com/a?b", ["https://x.com/a?b"])
        assert not url_matches_patterns("https://x.com/aXb", ["https://x.com/a?b"])

    def test_path_is_case_sensitive(self):
        # Host is case-insensitive, but the path is not.
        assert not url_matches_patterns("https://x.com/careline", ["https://x.com/CARELINE"])
        assert url_matches_patterns("https://X.com/CARELINE", ["https://x.com/CARELINE"])

    def test_broadened_pattern_matches_deep_link(self):
        # The practical fix an admin makes: widen to /** and the deep link matches.
        u = "https://shop.super-pharm.co.il/CARELINE/c/b_425?q=:popularity:brand:b_425"
        assert url_matches_patterns(u, ["https://shop.super-pharm.co.il/**"])


class TestLinkLocal:
    def test_metadata_ip_is_link_local(self):
        assert _is_link_local_literal("169.254.169.254")

    def test_public_ip_not_link_local(self):
        assert not _is_link_local_literal("93.184.216.34")

    def test_hostname_not_link_local(self):
        assert not _is_link_local_literal("example.com")


class TestTopLevelDocument:
    """Off-allowlist iframes (reCAPTCHA, embedded maps) may load from public
    hosts; only top-level page navigations are confined to the allowlist."""

    @staticmethod
    def _req(resource_type, parent):
        return SimpleNamespace(resource_type=resource_type, frame=SimpleNamespace(parent_frame=parent))

    def test_main_frame_navigation_is_top_level(self):
        assert _is_top_level_document(self._req("document", None))

    def test_iframe_document_is_not_top_level(self):
        assert not _is_top_level_document(self._req("document", object()))

    @pytest.mark.parametrize("rtype", ["script", "xhr", "fetch", "image", "stylesheet"])
    def test_subresources_are_not_top_level(self, rtype):
        assert not _is_top_level_document(self._req(rtype, None))

    def test_unreadable_frame_fails_closed(self):
        class Req:
            resource_type = "document"

            @property
            def frame(self):
                raise RuntimeError("frame detached")

        assert _is_top_level_document(Req())


class TestConnectionResolution:
    def _report(self, config):
        conn = SimpleNamespace(type="browser", config=config)
        ds = SimpleNamespace(connections=[conn])
        return SimpleNamespace(data_sources=[ds])

    def test_dict_config(self):
        rep = self._report({"url_patterns": ["https://x.com/**"], "allow_downloads": False})
        patterns, dl = get_browser_connection({"report": rep})
        assert patterns == ["https://x.com/**"]
        assert dl is False

    def test_json_string_config(self):
        rep = self._report('{"url_patterns": ["https://x.com/**"]}')
        patterns, dl = get_browser_connection({"report": rep})
        assert patterns == ["https://x.com/**"]
        assert dl is True  # default

    def test_double_encoded_config(self):
        # The /data_sources create path stores a JSON string of a JSON string.
        rep = self._report('"{\\"url_patterns\\": [\\"https://x.com/**\\"]}"')
        patterns, _ = get_browser_connection({"report": rep})
        assert patterns == ["https://x.com/**"]

    def test_no_browser_connection(self):
        rep = SimpleNamespace(data_sources=[])
        assert get_browser_connection({"report": rep}) is None


class _FakeContext:
    def __init__(self, kwargs, rec):
        self.kwargs = kwargs
        self._rec = rec

    async def route(self, *a, **k):
        pass

    async def route_web_socket(self, *a, **k):
        pass

    async def new_page(self):
        return SimpleNamespace()

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, rec):
        self._rec = rec

    async def new_context(self, **kwargs):
        self._rec["context_kwargs"] = kwargs
        return _FakeContext(kwargs, self._rec)

    async def close(self):
        pass


class _FakeChromium:
    def __init__(self, rec):
        self._rec = rec

    async def launch(self, **kwargs):
        self._rec["launch_kwargs"] = kwargs
        return _FakeBrowser(self._rec)


class _FakePlaywright:
    def __init__(self, rec):
        self.chromium = _FakeChromium(rec)

    async def stop(self):
        pass


def _install_fake_playwright(monkeypatch):
    """Record the kwargs open() hands Chromium, without a real browser."""
    rec: dict = {}
    import playwright.async_api as pwapi

    class _Factory:
        async def start(self):
            return _FakePlaywright(rec)

    monkeypatch.setattr(pwapi, "async_playwright", lambda: _Factory())
    return rec


async def _preview_stub():
    return SimpleNamespace(route=lambda *a, **k: None, close=_preview_stub)


class TestPreviewCertTrust:
    """What a user notices: whether a preview browser is launched trusting a
    cert. These drive open(..., preview=...) and assert on the kwargs Chromium
    actually receives -- not on a private helper (see backend/tests/AGENTS.md)."""

    @pytest.mark.asyncio
    async def test_preview_pins_the_configured_spki_and_never_blanket_disables(self, monkeypatch):
        rec = _install_fake_playwright(monkeypatch)
        # Spacing/blank entries prove the parse+join the operator's value goes through.
        monkeypatch.setenv("BOW_ARTIFACT_PREVIEW_CERT_SPKI", " AAAA , , BBBB ")
        monkeypatch.delenv("BOW_BROWSER_IGNORE_HTTPS_ERRORS", raising=False)
        preview = SimpleNamespace(route=lambda *a, **k: None, close=_preview_stub)
        await BrowserSessionManager().open("rep-1", [], False, preview=preview)
        assert "--ignore-certificate-errors-spki-list=AAAA,BBBB" in rec["launch_kwargs"].get("args", [])
        # Pinning replaces the old blanket bypass: verification is NOT turned off.
        assert "ignore_https_errors" not in rec["context_kwargs"]

    @pytest.mark.asyncio
    async def test_preview_keeps_strict_verification_when_unset(self, monkeypatch):
        rec = _install_fake_playwright(monkeypatch)
        monkeypatch.delenv("BOW_ARTIFACT_PREVIEW_CERT_SPKI", raising=False)
        monkeypatch.delenv("BOW_BROWSER_IGNORE_HTTPS_ERRORS", raising=False)
        preview = SimpleNamespace(route=lambda *a, **k: None, close=_preview_stub)
        await BrowserSessionManager().open("rep-2", [], False, preview=preview)
        args = rec["launch_kwargs"].get("args", [])
        assert not any(a.startswith("--ignore-certificate-errors") for a in args)
        assert "ignore_https_errors" not in rec["context_kwargs"]

    @pytest.mark.asyncio
    async def test_preview_spki_does_not_leak_into_general_browsing(self, monkeypatch):
        rec = _install_fake_playwright(monkeypatch)
        # An external egress proxy is present, so open() skips the local egress proxy.
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
        monkeypatch.setenv("BOW_ARTIFACT_PREVIEW_CERT_SPKI", "AAAA")
        monkeypatch.setenv("BOW_BROWSER_IGNORE_HTTPS_ERRORS", "true")
        await BrowserSessionManager().open("rep-3", [], False, preview=None)
        args = rec["launch_kwargs"].get("args", [])
        # The preview pin never applies to arbitrary external browsing...
        assert not any(a.startswith("--ignore-certificate-errors") for a in args)
        # ...and the dev flag still governs general browsing, unchanged.
        assert rec["context_kwargs"].get("ignore_https_errors") is True
