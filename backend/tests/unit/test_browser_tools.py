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
    http_credentials_from_secrets,
    redact_secrets,
    resolve_secret_placeholders,
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


def _report(config, credentials=None, auth_policy="system_only"):
    conn = SimpleNamespace(
        id="conn-1", type="browser", config=config, auth_policy=auth_policy,
        decrypt_credentials=lambda: dict(credentials or {}),
    )
    ds = SimpleNamespace(id="ds-1", connections=[conn])
    return SimpleNamespace(id="rep-1", data_sources=[ds])


class TestConnectionResolution:
    @pytest.mark.asyncio
    async def test_dict_config(self):
        rep = _report({"url_patterns": ["https://x.com/**"], "allow_downloads": False})
        ctx = await get_browser_connection({"report": rep})
        assert ctx.patterns == ["https://x.com/**"]
        assert ctx.allow_downloads is False

    @pytest.mark.asyncio
    async def test_json_string_config(self):
        rep = _report('{"url_patterns": ["https://x.com/**"]}')
        ctx = await get_browser_connection({"report": rep})
        assert ctx.patterns == ["https://x.com/**"]
        assert ctx.allow_downloads is True  # default

    @pytest.mark.asyncio
    async def test_double_encoded_config(self):
        # The /data_sources create path stores a JSON string of a JSON string.
        rep = _report('"{\\"url_patterns\\": [\\"https://x.com/**\\"]}"')
        ctx = await get_browser_connection({"report": rep})
        assert ctx.patterns == ["https://x.com/**"]

    @pytest.mark.asyncio
    async def test_no_browser_connection(self):
        rep = SimpleNamespace(data_sources=[])
        assert await get_browser_connection({"report": rep}) is None

    @pytest.mark.asyncio
    async def test_system_secrets_and_session_key(self):
        rep = _report(
            {"url_patterns": ["https://x.com/**"]},
            credentials={"secrets": {"API_TOKEN": "tok-123", "EMPTY": ""}},
        )
        user = SimpleNamespace(id="user-9")
        ctx = await get_browser_connection({"report": rep, "user": user})
        assert ctx.secrets == {"API_TOKEN": "tok-123"}  # empty values dropped
        assert ctx.session_key == "rep-1:conn-1:user-9"

    @pytest.mark.asyncio
    async def test_session_key_isolated_per_user(self):
        rep = _report({"url_patterns": ["https://x.com/**"]})
        a = await get_browser_connection({"report": rep, "user": SimpleNamespace(id="a")})
        b = await get_browser_connection({"report": rep, "user": SimpleNamespace(id="b")})
        assert a.session_key != b.session_key

    @pytest.mark.asyncio
    async def test_proxy_from_config_and_credentials(self):
        rep = _report(
            {"url_patterns": ["https://x.com/**"], "proxy_server": "http://proxy:3128",
             "proxy_bypass": "localhost,.internal"},
            credentials={"proxy_username": "u", "proxy_password": "p"},
        )
        ctx = await get_browser_connection({"report": rep})
        assert ctx.proxy == {"server": "http://proxy:3128", "bypass": "localhost,.internal",
                             "username": "u", "password": "p"}

    @pytest.mark.asyncio
    async def test_no_proxy_when_unconfigured(self):
        rep = _report({"url_patterns": ["https://x.com/**"]})
        ctx = await get_browser_connection({"report": rep})
        assert ctx.proxy is None

    @pytest.mark.asyncio
    async def test_missing_user_secrets_lists_declared_keys(self):
        rep = _report({"url_patterns": ["https://x.com/**"],
                       "user_secret_keys": ["API_TOKEN", "FIRST_NAME"]},
                      credentials={"secrets": {"API_TOKEN": "shared"}})
        ctx = await get_browser_connection({"report": rep})
        assert ctx.missing_user_secrets == ["FIRST_NAME"]


class TestSecretPlaceholders:
    SECRETS = {"API_TOKEN": "tok-abc-123", "FIRST_NAME": "Jane"}

    def test_resolve_in_url(self):
        out, used, missing = resolve_secret_placeholders(
            "https://x.com/api?token={{secret:API_TOKEN}}", self.SECRETS)
        assert out == "https://x.com/api?token=tok-abc-123"
        assert used == ["API_TOKEN"]
        assert missing == []

    def test_resolve_with_spaces(self):
        out, used, _ = resolve_secret_placeholders("{{ secret:FIRST_NAME }}", self.SECRETS)
        assert out == "Jane"
        assert used == ["FIRST_NAME"]

    def test_missing_key_reported_not_substituted(self):
        out, used, missing = resolve_secret_placeholders("x={{secret:NOPE}}", self.SECRETS)
        assert out == "x={{secret:NOPE}}"
        assert used == []
        assert missing == ["NOPE"]

    def test_plain_text_untouched(self):
        out, used, missing = resolve_secret_placeholders("hello world", self.SECRETS)
        assert out == "hello world"
        assert used == [] and missing == []

    def test_redacts_value_from_output(self):
        text = "Welcome! Your token is tok-abc-123."
        assert redact_secrets(text, self.SECRETS) == "Welcome! Your token is {{secret:API_TOKEN}}."

    def test_redacts_urlencoded_value(self):
        secrets = {"TOKEN": "a b/c"}
        text = "https://x.com/?q=a%20b%2Fc"
        assert redact_secrets(text, secrets) == "https://x.com/?q={{secret:TOKEN}}"

    def test_short_values_not_redacted(self):
        # Redacting a 1-char value would shred the page text without hiding anything.
        secrets = {"X": "a"}
        assert redact_secrets("a banana", secrets) == "a banana"

    def test_none_text_passthrough(self):
        assert redact_secrets(None, self.SECRETS) is None


class TestHttpCredentials:
    def test_reserved_keys_become_http_credentials(self):
        creds = http_credentials_from_secrets({"HTTP_USERNAME": "jane.doe", "HTTP_PASSWORD": "pw", "OTHER": "x"})
        assert creds == {"username": "jane.doe", "password": "pw"}

    def test_username_only(self):
        assert http_credentials_from_secrets({"HTTP_USERNAME": "jane.doe"}) == {"username": "jane.doe", "password": ""}

    def test_no_reserved_keys(self):
        assert http_credentials_from_secrets({"API_TOKEN": "x"}) is None
        assert http_credentials_from_secrets({}) is None


class TestSecretMergePatch:
    def _svc(self):
        from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
        return UserDataSourceCredentialsService()

    def _row(self, secrets, auth_mode="secrets"):
        return SimpleNamespace(auth_mode=auth_mode,
                               decrypt_credentials=lambda: {"secrets": dict(secrets)})

    def test_adds_and_keeps_existing_keys(self):
        svc = self._svc()
        row = self._row({"A": "1", "B": "2"})
        merged = svc._merge_secret_map(row, "secrets", {"secrets": {"C": "3"}})
        assert merged["secrets"] == {"A": "1", "B": "2", "C": "3"}

    def test_null_deletes_key(self):
        svc = self._svc()
        row = self._row({"A": "1", "B": "2"})
        merged = svc._merge_secret_map(row, "secrets", {"secrets": {"A": None}})
        assert merged["secrets"] == {"B": "2"}

    def test_overwrite_key(self):
        svc = self._svc()
        row = self._row({"A": "1"})
        merged = svc._merge_secret_map(row, "secrets", {"secrets": {"A": "9"}})
        assert merged["secrets"] == {"A": "9"}

    def test_first_save_no_row(self):
        svc = self._svc()
        merged = svc._merge_secret_map(None, "secrets", {"secrets": {"A": "1", "B": None}})
        assert merged["secrets"] == {"A": "1"}

    def test_auth_mode_change_discards_old_map(self):
        svc = self._svc()
        row = self._row({"A": "1"}, auth_mode="other")
        merged = svc._merge_secret_map(row, "secrets", {"secrets": {"B": "2"}})
        assert merged["secrets"] == {"B": "2"}

    def test_non_secret_payload_untouched(self):
        svc = self._svc()
        merged = svc._merge_secret_map(None, "userpass", {"user": "u", "password": "p"})
        assert merged == {"user": "u", "password": "p"}

    def test_stamp_secret_keys(self):
        svc = self._svc()
        meta = svc._stamp_secret_keys({"note": "x"}, {"secrets": {"B": "2", "A": "1"}})
        assert meta == {"note": "x", "secret_keys": ["A", "B"]}
        assert svc._stamp_secret_keys(None, {"user": "u"}) is None
