"""Pinned outbound HTTP (app.core.egress): the SSRF guard that also closes
DNS rebinding.

Contract under test:
- cloud metadata / link-local is refused in every tier, in every spelling
  (decimal, hex, octal, IPv4-mapped / NAT64 IPv6, unspecified, trailing dot,
  userinfo tricks);
- private / loopback is allowed on the admin tier unless
  ``block_private_outbound`` is on, and always refused on the public tier;
- the client connects to the address that was validated, never to a second
  DNS answer (simulated rebinding resolver: first answer fine, second answer
  metadata), on every request and every redirect hop;
- TLS still verifies the certificate against the *hostname*.

DNS is simulated at the boundary (``egress._getaddrinfo``); the HTTP servers
are real sockets on 127.0.0.1 so the pin is observable: the hostnames used
here (``*.invalid``) cannot resolve, so a client that did its own lookup
would fail instead of reaching the server.
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl

import httpx
import pytest

from app.ai.http import safe_client
from app.core import egress
from app.core.egress import (
    EgressBlockedError,
    Policy,
    check_google_token_uri,
    check_host,
    check_url,
    curl_pinned_request,
    mcp_client_factory,
    pinned_async_client,
    pinned_client,
)
from app.core.url_safety import UnsafeURLError
from tests.utils.egress_fakes import FakeDNS, local_server


METADATA_SPELLINGS = [
    "169.254.169.254",
    "2852039166",            # decimal
    "0xa9fea9fe",            # hex
    "0xa9.0xfe.0xa9.0xfe",   # dotted hex
    "0251.0376.0251.0376",   # octal
    "169.254.43518",         # inet_aton short form
    "[::ffff:169.254.169.254]",
    "[::ffff:a9fe:a9fe]",
    "[64:ff9b::a9fe:a9fe]",  # NAT64
    "[fe80::1]",
    "[fd00:ec2::254]",
    "100.100.100.200",
    "0.0.0.0",
    "[::]",
]


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_env_proxy(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)


@pytest.fixture
def dns(monkeypatch):
    def install(table):
        fake = FakeDNS(table)
        monkeypatch.setattr(egress, "_getaddrinfo", fake)
        return fake

    return install


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", METADATA_SPELLINGS)
@pytest.mark.parametrize("policy", [Policy.ADMIN, Policy.PUBLIC])
def test_metadata_refused_in_every_spelling_and_tier(host, policy):
    with pytest.raises(UnsafeURLError):
        check_url(f"http://{host}/latest/meta-data/", policy)


@pytest.mark.parametrize("url", [
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://METADATA.GOOGLE.INTERNAL./",
    "http://metadata/",
    "http://user:pw@169.254.169.254/",
    "http://public.example.com@169.254.169.254/",
])
def test_metadata_names_and_userinfo_tricks_refused(url):
    with pytest.raises(UnsafeURLError):
        check_url(url, Policy.ADMIN)


@pytest.mark.parametrize("url", ["http://evil.example\\@169.254.169.254/", "http://a b/", "gopher://x/", "file:///etc/passwd"])
def test_ambiguous_or_non_http_urls_refused(url):
    with pytest.raises(UnsafeURLError):
        check_url(url, Policy.ADMIN)


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.10", "172.16.5.5", "127.0.0.1", "100.64.1.1"])
def test_private_allowed_on_admin_tier_by_default(dns, ip):
    dns({"llm-gw.corp.invalid": ip})
    assert [str(a) for a in check_host("llm-gw.corp.invalid", Policy.ADMIN)] == [ip]


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.10", "127.0.0.1", "100.64.1.1"])
def test_private_refused_when_block_private_outbound(dns, monkeypatch, ip):
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    dns({"llm-gw.corp.invalid": ip})
    with pytest.raises(UnsafeURLError):
        check_host("llm-gw.corp.invalid", Policy.ADMIN)


@pytest.mark.parametrize("ip", ["10.1.2.3", "127.0.0.1"])
def test_private_refused_on_public_tier(dns, ip):
    dns({"intranet.invalid": ip})
    with pytest.raises(UnsafeURLError):
        check_host("intranet.invalid", Policy.PUBLIC)


def test_public_allowed_on_both_tiers(dns):
    dns({"api.vendor.invalid": "93.184.216.34"})
    for policy in Policy:
        assert check_host("api.vendor.invalid", policy) == [ipaddress.ip_address("93.184.216.34")]


def test_any_bad_address_in_the_answer_refuses_the_host(dns):
    dns({"mixed.invalid": [["93.184.216.34", "169.254.169.254"]]})
    with pytest.raises(UnsafeURLError):
        check_host("mixed.invalid", Policy.ADMIN)


def test_unresolvable_host_passes_admin_save_check_but_not_connect(dns):
    dns({})
    assert check_url("https://not-yet-in-dns.invalid/", Policy.ADMIN) == []
    with pytest.raises(UnsafeURLError):
        check_url("https://not-yet-in-dns.invalid/", Policy.PUBLIC)
    with pytest.raises(EgressBlockedError):
        pinned_client(Policy.ADMIN).get("http://not-yet-in-dns.invalid/")


# ── httpx pinning ───────────────────────────────────────────────────────────


def test_httpx_connects_to_the_validated_address_and_keeps_hostname(dns):
    with local_server() as (port, seen):
        fake = dns({"internal-api.invalid": "127.0.0.1"})
        with pinned_client(Policy.ADMIN) as c:
            r = c.get(f"http://internal-api.invalid:{port}/x")
        assert r.status_code == 200
        assert str(r.url) == f"http://internal-api.invalid:{port}/x"  # caller sees the hostname
        assert seen == [("GET", "/x", f"internal-api.invalid:{port}")]       # server sees the hostname
        assert fake.lookups == ["internal-api.invalid"]               # resolved once, by us


def test_httpx_rebinding_second_answer_is_never_used(dns):
    with local_server() as (port, seen):
        dns({"rebind.invalid": ["127.0.0.1", "169.254.169.254"]})
        with pinned_client(Policy.ADMIN) as c:
            assert c.get(f"http://rebind.invalid:{port}/").status_code == 200
            with pytest.raises(EgressBlockedError):
                c.get(f"http://rebind.invalid:{port}/")
        assert len(seen) == 1


def test_httpx_public_answer_is_pinned_for_public_tier(dns, monkeypatch):
    """First answer public, second metadata: the request goes to the public
    address that was checked (observed at the connection layer)."""
    dns({"news.invalid": ["93.184.216.34", "169.254.169.254"]})
    sent = []

    def fake_handle(self, request):
        sent.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    with pinned_client(Policy.PUBLIC) as c:
        assert c.get("https://news.invalid/a").status_code == 200
        with pytest.raises(EgressBlockedError):
            c.get("https://news.invalid/a")
    assert sent == [("93.184.216.34", "news.invalid", "news.invalid")]


@pytest.mark.asyncio
async def test_async_client_rebinding_and_redirect_to_metadata(dns):
    routes = {
        "/hop": (302, {"Location": "/final"}, b""),
        "/evil": (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b""),
    }
    with local_server(routes) as (port, seen):
        dns({"svc.invalid": "127.0.0.1"})
        async with pinned_async_client(Policy.ADMIN, follow_redirects=True) as c:
            r = await c.get(f"http://svc.invalid:{port}/hop")
            assert r.status_code == 200 and r.url.path == "/final"
            with pytest.raises(EgressBlockedError):
                await c.get(f"http://svc.invalid:{port}/evil")
        assert [p for _, p, _ in seen] == ["/hop", "/final", "/evil"]


def test_redirect_to_another_allowed_host_is_followed_and_rechecked(dns):
    # Two servers: a -> 302 -> b. b's name resolves fine the first time and
    # to metadata the second time.
    with local_server() as (port_b, seen_b):
        with local_server({"/go": (302, {"Location": f"http://b.invalid:{port_b}/landing"}, b"")}) as (port_a, seen_a):
            dns({"a.invalid": "127.0.0.1", "b.invalid": ["127.0.0.1", "169.254.169.254"]})
            with pinned_client(Policy.ADMIN, follow_redirects=True) as c:
                r = c.get(f"http://a.invalid:{port_a}/go")
                assert r.status_code == 200 and r.url.host == "b.invalid"
                with pytest.raises(EgressBlockedError):
                    c.get(f"http://a.invalid:{port_a}/go")
            assert [h for _, _, h in seen_b] == [f"b.invalid:{port_b}"]


def test_env_proxy_still_validates_hostname(dns, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.invalid:3128")
    dns({"metadata-alias.invalid": "169.254.169.254"})
    with pytest.raises(EgressBlockedError):
        pinned_client(Policy.ADMIN).get("http://metadata-alias.invalid/")


@pytest.mark.parametrize("make_client", [
    lambda: pinned_client(Policy.ADMIN),
    lambda: pinned_async_client(Policy.ADMIN),
])
def test_env_proxy_reaches_host_the_server_cannot_resolve(dns, monkeypatch, make_client):
    # Locked-down on-prem: no outbound DNS, everything leaves via HTTP(S)_PROXY.
    # The target resolves only at the proxy, so it must not be refused here.
    import asyncio

    with local_server() as (port, seen):
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
        dns({})  # nothing resolves locally
        client = make_client()
        if isinstance(client, httpx.AsyncClient):
            async def go():
                async with client:
                    return await client.get("http://llm.corp.invalid/v1/models")
            r = asyncio.run(go())
        else:
            with client:
                r = client.get("http://llm.corp.invalid/v1/models")
    assert r.status_code == 200
    assert seen == [("GET", "http://llm.corp.invalid/v1/models", "llm.corp.invalid")]


def test_unresolvable_host_without_proxy_still_refused(dns):
    dns({})
    with pytest.raises(EgressBlockedError):
        pinned_client(Policy.ADMIN).get("http://llm.corp.invalid/")


def test_blocked_error_is_an_httpx_connect_error():
    # Existing `except httpx.HTTPError` handlers keep working.
    assert issubclass(EgressBlockedError, httpx.ConnectError)
    assert issubclass(EgressBlockedError, UnsafeURLError)


def test_mcp_factory_keeps_sdk_defaults_and_pins(dns):
    client = mcp_client_factory(Policy.ADMIN)(headers={"X-A": "1"})
    assert isinstance(client._transport, egress.AsyncPinnedTransport)
    assert client.timeout.read == 300.0 and client.timeout.connect == 30.0
    assert client.follow_redirects is False
    assert client.headers["X-A"] == "1"


# ── TLS still verifies the hostname ─────────────────────────────────────────


def _self_signed(tmp_path, name):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def test_tls_certificate_is_verified_against_the_hostname(dns, tmp_path):
    cert, key = _self_signed(tmp_path, "gw.corp.invalid")
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    client_ctx = ssl.create_default_context(cafile=cert)
    with local_server(tls_context=server_ctx) as (port, seen):
        dns({"gw.corp.invalid": "127.0.0.1", "impostor.invalid": "127.0.0.1"})
        with pinned_client(Policy.ADMIN, verify=client_ctx) as c:
            assert c.get(f"https://gw.corp.invalid:{port}/").status_code == 200
            # Same IP, different name: the cert does not cover it -> refused.
            with pytest.raises(httpx.ConnectError) as exc:
                c.get(f"https://impostor.invalid:{port}/")
            assert "certificate" in str(exc.value).lower() or "hostname" in str(exc.value).lower()


# ── curl_cffi pinning (web_fetch / sandbox http) ────────────────────────────


def test_curl_canonical_url_and_resolve_pin(dns):
    from curl_cffi.const import CurlOpt

    dns({"xn--exmple-cua.invalid": "93.184.216.34"})
    url, opts = curl_pinned_request("https://user:pw@Exämple.invalid./p?q=1#frag", Policy.PUBLIC)
    assert url == "https://xn--exmple-cua.invalid/p?q=1"
    assert opts[CurlOpt.RESOLVE] == ["xn--exmple-cua.invalid:443:93.184.216.34"]


@pytest.mark.parametrize("host", METADATA_SPELLINGS)
def test_curl_refuses_metadata_spellings(host):
    with pytest.raises(UnsafeURLError):
        curl_pinned_request(f"http://{host}/", Policy.PUBLIC)


@pytest.mark.asyncio
async def test_curl_rebinding_second_answer_is_never_used(dns):
    with local_server() as (port, seen):
        dns({"rebind.invalid": ["127.0.0.1", "169.254.169.254"]})
        r = await safe_client.pinned_curl_get(f"http://rebind.invalid:{port}/", timeout=5, policy=Policy.ADMIN)
        assert r.status_code == 200
        with pytest.raises(UnsafeURLError):
            await safe_client.pinned_curl_get(f"http://rebind.invalid:{port}/", timeout=5, policy=Policy.ADMIN)
    assert seen == [("GET", "/", f"rebind.invalid:{port}")]


def test_sandbox_http_get_refuses_metadata_and_private(dns):
    dns({"rebind.invalid": "169.254.169.254", "intranet.invalid": "10.0.0.5"})
    client = safe_client.SafeHttpClient()
    for url in ("http://rebind.invalid/", "http://intranet.invalid/", "http://2852039166/", "http://[::ffff:a9fe:a9fe]/"):
        page = client.get(url)
        assert page.success is False and page.error


def test_sandbox_http_redirect_to_metadata_refused(dns, monkeypatch):
    with local_server({"/r": (302, {"Location": "http://169.254.169.254/"}, b"")}) as (port, seen):
        dns({"site.invalid": "127.0.0.1"})
        # The sandbox helper is public-tier; let the loopback test server
        # stand in for a public site for this redirect test.
        real = egress.ip_violation
        monkeypatch.setattr(
            egress, "ip_violation",
            lambda ip, policy, bp=None: None if str(ip) == "127.0.0.1" else real(ip, policy, bp),
        )
        page = safe_client.SafeHttpClient().get(f"http://site.invalid:{port}/r")
    assert page.success is False
    assert "redirect" in (page.error or "").lower()
    assert [p for _, p, _ in seen] == ["/r"]


# ── Google service-account token_uri ────────────────────────────────────────


@pytest.mark.parametrize("uri", ["https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"])
def test_google_token_uri_allows_google(uri):
    check_google_token_uri({"type": "service_account", "token_uri": uri})


@pytest.mark.parametrize("uri", [
    None,
    "http://169.254.169.254/token",
    "https://attacker.example/token",
    "http://oauth2.googleapis.com/token",
    "https://oauth2.googleapis.com.attacker.example/token",
])
def test_google_token_uri_rejects_everything_else(uri):
    info = {"type": "service_account"}
    if uri is not None:
        info["token_uri"] = uri
    with pytest.raises(UnsafeURLError):
        check_google_token_uri(info)
