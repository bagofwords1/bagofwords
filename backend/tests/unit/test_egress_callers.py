"""Every outbound HTTP path covered by the SSRF / DNS-rebinding fix goes
through the pinned egress guard, and legitimate destinations keep working.

Per path: a destination that resolves to cloud metadata (directly, via an
encoded IP, or on the *second* DNS answer — rebinding) is refused and never
contacted; a private/internal destination still works while
``block_private_outbound`` is off and is refused when it is on.

DNS is faked at the boundary (``app.core.egress._getaddrinfo``) and the
servers are real sockets on 127.0.0.1 reached through ``*.invalid`` names,
so "the server saw the request" proves the pinned address was used.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core import egress
from tests.utils.egress_fakes import FakeDNS, local_server


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


@pytest.fixture
def loopback_counts_as_public(monkeypatch):
    """Public-tier paths (web_fetch, sandbox http) refuse loopback; let the
    127.0.0.1 test server stand in for a public site."""
    real = egress.ip_violation
    monkeypatch.setattr(
        egress, "ip_violation",
        lambda ip, policy, bp=None: None if str(ip) == "127.0.0.1" else real(ip, policy, bp),
    )


def _block_private(monkeypatch):
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)


# ── 1. web_fetch ────────────────────────────────────────────────────────────


def _web_fetch(url):
    from app.ai.tools.implementations.web_fetch import WebFetchTool

    class _On:
        value = True

    ctx = {
        "settings": SimpleNamespace(get_config=lambda k: _On()),
        "report": SimpleNamespace(id="r1"),
        "organization": SimpleNamespace(id="o1"),
        "current_user": SimpleNamespace(id="u1"),
    }

    async def run():
        events = [e async for e in WebFetchTool().run_stream({"url": url}, ctx)]
        return [e for e in events if e.type == "tool.end"][-1].payload["output"]

    return asyncio.run(run())


def test_web_fetch_rebinding_second_answer_never_reached(dns, loopback_counts_as_public):
    html = b"<html><head><title>News</title></head><body>hi</body></html>"
    with local_server({"/a": (200, {"Content-Type": "text/html"}, html)}) as (port, seen):
        dns({"news.invalid": ["127.0.0.1", "169.254.169.254"]})
        first = _web_fetch(f"http://news.invalid:{port}/a")
        second = _web_fetch(f"http://news.invalid:{port}/a")
    assert first["success"] is True and first["title"] == "News"
    assert second["success"] is False
    assert len(seen) == 1


def test_web_fetch_follows_redirect_to_another_public_host(dns, loopback_counts_as_public):
    html = b"<html><head><title>Landed</title></head><body>ok</body></html>"
    with local_server({"/landing": (200, {"Content-Type": "text/html"}, html)}) as (port_b, seen_b):
        with local_server({"/go": (302, {"Location": f"http://b.invalid:{port_b}/landing"}, b"")}) as (port_a, _):
            dns({"a.invalid": "127.0.0.1", "b.invalid": "127.0.0.1"})
            out = _web_fetch(f"http://a.invalid:{port_a}/go")
    assert out["success"] is True and out["title"] == "Landed"
    assert [h for _, _, h in seen_b] == [f"b.invalid:{port_b}"]


@pytest.mark.parametrize("target", ["http://169.254.169.254/", "http://2852039166/", "http://[::ffff:a9fe:a9fe]/", "http://meta.invalid/"])
def test_web_fetch_redirect_to_metadata_refused(dns, loopback_counts_as_public, target):
    with local_server({"/r": (302, {"Location": target}, b"")}) as (port, seen):
        dns({"site.invalid": "127.0.0.1", "meta.invalid": "169.254.169.254"})
        out = _web_fetch(f"http://site.invalid:{port}/r")
    assert out["success"] is False
    assert "redirect" in out["error_message"].lower()
    assert len(seen) == 1


def test_web_fetch_refuses_private_hosts(dns):
    dns({"intranet.invalid": "10.0.0.8"})
    assert _web_fetch("http://intranet.invalid/")["success"] is False


# ── 2. custom API / MCP data-source URLs ────────────────────────────────────


def test_custom_api_internal_host_works_and_metadata_refused(dns, monkeypatch):
    from app.data_sources.clients.custom_api_client import CustomApiClient

    with local_server() as (port, seen):
        dns({"api.corp.invalid": "10.20.30.40", "erp.corp.invalid": "127.0.0.1", "meta.invalid": "169.254.169.254"})
        ok = CustomApiClient(base_url=f"http://erp.corp.invalid:{port}").test_connection()
        bad = CustomApiClient(base_url="http://meta.invalid/latest").test_connection()
        _block_private(monkeypatch)
        flagged = CustomApiClient(base_url=f"http://erp.corp.invalid:{port}").test_connection()
    assert ok["success"] is True
    assert bad["success"] is False
    assert flagged["success"] is False
    assert len(seen) == 1


def test_custom_api_tool_call_rebinding_refused(dns):
    from app.data_sources.clients.custom_api_client import CustomApiClient

    ep = {"name": "get_items", "method": "GET", "path": "/items", "parameters": []}
    with local_server({"/items": (200, {}, [{"id": 1}])}) as (port, seen):
        dns({"erp.corp.invalid": ["127.0.0.1", "169.254.169.254"]})
        client = CustomApiClient(base_url=f"http://erp.corp.invalid:{port}", endpoints=[ep])
        first = client.call_tool("get_items", {})
        second = client.call_tool("get_items", {})
    assert first["success"] is True
    assert second["success"] is False
    assert len(seen) == 1


def test_mcp_client_never_contacts_metadata(dns):
    from app.data_sources.clients.mcp_client import McpClient

    dns({"mcp.corp.invalid": "169.254.169.254"})
    for transport in ("streamable_http", "sse"):
        res = asyncio.run(McpClient(server_url="http://mcp.corp.invalid/mcp", transport=transport)._atest_connection())
        assert res["success"] is False


@pytest.mark.asyncio
async def test_connection_test_params_refuses_metadata_url(dns):
    from app.services.connection_service import ConnectionService

    dns({"meta.invalid": "169.254.169.254"})
    svc = ConnectionService()
    for ds_type, cfg in (
        ("custom_api", {"base_url": "http://meta.invalid/", "auth_type": "none"}),
        ("custom_api", {"base_url": "http://0xa9fea9fe/", "auth_type": "none"}),
        ("mcp", {"server_url": "http://169.254.169.254/mcp", "transport": "streamable_http"}),
    ):
        res = await svc.test_connection_params(ds_type, cfg, {})
        assert res["success"] is False, (ds_type, cfg)


# ── 3. MCP OAuth discovery / DCR ────────────────────────────────────────────


def _mcp_auth_server(port, *, token_endpoint=None, registration_endpoint=None):
    base = f"http://mcp.corp.invalid:{port}"
    return {
        "/.well-known/oauth-protected-resource/mcp": (200, {}, {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": ["mcp:read"]}),
        "/.well-known/oauth-authorization-server": (200, {}, {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": token_endpoint or f"{base}/token",
            "registration_endpoint": registration_endpoint or f"{base}/register",
        }),
        ("POST", "/register"): (201, {}, {"client_id": "cid-1"}),
    }


def _with_routes(routes_fn):
    """local_server whose routes need the server's own port."""
    from contextlib import contextmanager

    @contextmanager
    def cm(**kw):
        routes = {}
        with local_server(routes) as (port, seen):
            routes.update(routes_fn(port, **kw))
            yield port, seen

    return cm


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_against_internal_provider_works(dns):
    from app.services.mcp_dcr_service import discover_mcp_oauth, register_client

    with _with_routes(_mcp_auth_server)() as (port, seen):
        dns({"mcp.corp.invalid": "127.0.0.1"})
        meta = await discover_mcp_oauth(f"http://mcp.corp.invalid:{port}/mcp")
        reg = await register_client(meta["registration_endpoint"], "https://bow.example/cb")
    assert meta["token_url"].endswith("/token") and meta["scopes"] == "mcp:read"
    assert reg["client_id"] == "cid-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["token_endpoint", "registration_endpoint"])
async def test_mcp_oauth_discovery_refuses_metadata_endpoints(dns, field):
    from app.services.mcp_dcr_service import discover_mcp_oauth

    with _with_routes(_mcp_auth_server)(**{field: "http://169.254.169.254/latest/meta-data/"}) as (port, seen):
        dns({"mcp.corp.invalid": "127.0.0.1"})
        with pytest.raises(ValueError):
            await discover_mcp_oauth(f"http://mcp.corp.invalid:{port}/mcp")


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_rebinding_between_requests_refused(dns):
    from app.services.mcp_dcr_service import discover_mcp_oauth

    with _with_routes(_mcp_auth_server)() as (port, seen):
        # Checked once fine (save-time check_url), then the probe, then the
        # name flips to metadata for the well-known fetches.
        dns({"mcp.corp.invalid": ["127.0.0.1", "127.0.0.1", "169.254.169.254"]})
        with pytest.raises(ValueError):
            await discover_mcp_oauth(f"http://mcp.corp.invalid:{port}/mcp")
    assert not any(p.startswith("/.well-known") for _, p, _ in seen)


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_refuses_metadata_server_url(dns):
    from app.services.mcp_dcr_service import discover_mcp_oauth

    for url in ("http://169.254.169.254/mcp", "http://[fd00:ec2::254]/mcp", "http://metadata.google.internal/mcp"):
        with pytest.raises(ValueError):
            await discover_mcp_oauth(url)


# ── 4. Google Chat service-account token_uri ────────────────────────────────


def _sa_info(token_uri):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return {
        "type": "service_account", "project_id": "p", "private_key_id": "k1", "private_key": pem,
        "client_email": "bot@p.iam.gserviceaccount.com", "client_id": "1", "token_uri": token_uri,
    }


def test_google_chat_save_rejects_foreign_token_uri_and_accepts_genuine():
    from fastapi import HTTPException

    from app.services.external_platform_service import ExternalPlatformService

    genuine = _sa_info("https://oauth2.googleapis.com/token")
    assert ExternalPlatformService._parse_google_chat_sa(json.dumps(genuine))["client_email"] == genuine["client_email"]
    with pytest.raises(HTTPException) as exc:
        ExternalPlatformService._parse_google_chat_sa(_sa_info("http://169.254.169.254/token"))
    assert exc.value.status_code == 400


def test_google_chat_token_mint_never_posts_to_foreign_token_uri():
    """A stored key whose token_uri was pointed elsewhere (saved before this
    check existed) must not POST its signed assertion there at use time."""
    from app.services.google_chat_listener_service import GoogleChatPubSubListener
    from app.services.platform_adapters.google_chat_adapter import GoogleChatAdapter

    with local_server({("POST", "/token"): (200, {}, {"access_token": "t", "expires_in": 3600})}) as (port, seen):
        info = _sa_info(f"http://127.0.0.1:{port}/token")
        listener = GoogleChatPubSubListener("projects/p/subscriptions/s", info, handler=lambda e: None)
        assert asyncio.run(listener._get_token()) is None
        adapter = GoogleChatAdapter.__new__(GoogleChatAdapter)
        adapter.service_account_info = info
        assert asyncio.run(adapter._get_access_token()) is None
    assert seen == []


# ── 5. LLM base_url (OpenAI-compatible gateway) ─────────────────────────────


_MODELS = {"object": "list", "data": [{"id": "m1", "object": "model", "created": 0, "owned_by": "gw"}]}


def test_llm_gateway_on_internal_host_works_and_rebinding_refused(dns, monkeypatch):
    from app.ai.llm.clients.openai_client import OpenAi

    with local_server({"/v1/models": (200, {}, _MODELS)}) as (port, seen):
        dns({"llm-gw.corp.invalid": ["127.0.0.1", "169.254.169.254"]})
        client = OpenAi(api_key="k", base_url=f"http://llm-gw.corp.invalid:{port}/v1", pin_egress=True)
        client.client.max_retries = 0
        assert [m.id for m in client.client.models.list().data] == ["m1"]
        with pytest.raises(Exception):
            client.client.models.list()
    assert len(seen) == 1


def test_llm_gateway_refused_when_block_private_outbound(dns, monkeypatch):
    from app.ai.llm.clients.openai_client import OpenAi

    _block_private(monkeypatch)
    with local_server({"/v1/models": (200, {}, _MODELS)}) as (port, seen):
        dns({"llm-gw.corp.invalid": "127.0.0.1"})
        client = OpenAi(api_key="k", base_url=f"http://llm-gw.corp.invalid:{port}/v1", pin_egress=True)
        client.client.max_retries = 0
        with pytest.raises(Exception):
            client.client.models.list()
    assert seen == []


@pytest.mark.parametrize("provider_type,cfg", [
    ("custom", {"base_url": "http://llm-gw.corp.invalid/v1"}),
    ("custom", {"base_url": "http://llm-gw.corp.invalid/v1", "use_responses_api": True, "verify_ssl": False}),
    ("openai", {"base_url": "http://llm-gw.corp.invalid/v1"}),
])
def test_llm_admin_base_url_uses_pinned_transport(provider_type, cfg):
    from app.ai.llm.llm import LLM
    from tests.unit.test_llm_provider_headers import make_model

    llm = LLM(make_model(provider_type, cfg))
    for sdk in (llm.client.client, llm.client.async_client):
        assert isinstance(sdk._client._transport, (egress.PinnedTransport, egress.AsyncPinnedTransport))


# ── 6. Audit streams ────────────────────────────────────────────────────────


def test_audit_http_stream_internal_endpoint_works_metadata_refused_at_send(dns, monkeypatch):
    from app.ee.audit.streams.destinations.base import INVALID
    from app.ee.audit.streams.destinations.http import HttpsDestination

    events = [{"id": "e1", "action": "x"}]
    with local_server() as (port, seen):
        # The stream was saved while siem.corp.invalid pointed at the SIEM;
        # it later answers with the metadata address.
        dns({"siem.corp.invalid": ["127.0.0.1", "169.254.169.254"]})
        dest = HttpsDestination({"url": f"http://siem.corp.invalid:{port}/ingest"}, {})
        first = asyncio.run(dest.send(events))
        second = asyncio.run(dest.send(events))
    assert first.ok
    assert second.kind == INVALID
    assert [p for _, p, _ in seen] == ["/ingest"]


def test_audit_http_stream_refused_when_block_private_outbound(dns, monkeypatch):
    from app.ee.audit.streams.destinations.http import HttpsDestination

    _block_private(monkeypatch)
    with local_server() as (port, seen):
        dns({"siem.corp.invalid": "127.0.0.1"})
        res = asyncio.run(HttpsDestination({"url": f"http://siem.corp.invalid:{port}/ingest"}, {}).send([{"id": 1}]))
    assert not res.ok and seen == []


# ── 7. Sandbox `http` helper (inspect_data / entity preview code) ───────────


def test_sandbox_http_public_api_works_and_rebinding_refused(dns, loopback_counts_as_public):
    from app.ai.http.safe_client import SafeHttpClient

    with local_server({"/api": (200, {}, {"ok": True})}) as (port, seen):
        dns({"api.public.invalid": ["127.0.0.1", "169.254.169.254"]})
        client = SafeHttpClient()
        first = client.get(f"http://api.public.invalid:{port}/api")
        second = client.get(f"http://api.public.invalid:{port}/api")
        batch = client.batch_get([f"http://api.public.invalid:{port}/api", "http://169.254.169.254/"])
    assert first.success and json.loads(first.text) == {"ok": True}
    assert not second.success and second.error
    assert not batch[1].success
    assert len(seen) == 1
