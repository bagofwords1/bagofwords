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
    _is_link_local_literal,
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


class TestLinkLocal:
    def test_metadata_ip_is_link_local(self):
        assert _is_link_local_literal("169.254.169.254")

    def test_public_ip_not_link_local(self):
        assert not _is_link_local_literal("93.184.216.34")

    def test_hostname_not_link_local(self):
        assert not _is_link_local_literal("example.com")


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
