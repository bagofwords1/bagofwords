"""XOAUTH2 token layer: SASL formatting, token minting (mocked), dispatch."""
import base64

import httpx
import pytest

from app.services.email import oauth
from app.services.email.oauth import OAuthSettings, build_xoauth2, get_access_token, get_xoauth2_string
from app.services.email.sender import SmtpConfig
from app.services.email.mailbox_reader import ImapConfig


# ---------- SASL string ----------


def test_build_xoauth2_format():
    s = build_xoauth2("alice@acme.com", "TOK123")
    decoded = base64.b64decode(s).decode()
    assert decoded == "user=alice@acme.com\x01auth=Bearer TOK123\x01\x01"


# ---------- OAuthSettings.from_credentials ----------


def test_oauth_settings_microsoft():
    o = OAuthSettings.from_credentials(
        {"auth_type": "microsoft", "from_address": "analyst@acme.com",
         "ms_tenant_id": "t", "ms_client_id": "c", "ms_client_secret": "s"},
    )
    assert o.provider == "microsoft"
    assert o.mailbox == "analyst@acme.com"
    assert (o.tenant_id, o.client_id, o.client_secret) == ("t", "c", "s")


def test_oauth_settings_google():
    o = OAuthSettings.from_credentials(
        {"auth_type": "google", "from_address": "analyst@acme.com",
         "google_service_account_info": {"client_email": "sa@p.iam"}},
    )
    assert o.provider == "google"
    assert o.service_account_info["client_email"] == "sa@p.iam"


def test_oauth_settings_password_is_none():
    assert OAuthSettings.from_credentials({"auth_type": "password"}) is None


# ---------- configs carry oauth ----------


def test_smtp_config_carries_oauth():
    cfg = SmtpConfig.from_credentials(
        {"auth_type": "microsoft", "from_address": "a@acme.com", "smtp_host": "smtp.office365.com",
         "ms_tenant_id": "t", "ms_client_id": "c", "ms_client_secret": "s"}
    )
    assert cfg.auth_type == "microsoft"
    assert cfg.oauth is not None and cfg.oauth.provider == "microsoft"


def test_imap_config_carries_oauth():
    cfg = ImapConfig.from_credentials(
        {"auth_type": "google", "from_address": "a@acme.com", "imap_host": "imap.gmail.com",
         "google_service_account_info": {"client_email": "sa@p"}}
    )
    assert cfg.auth_type == "google"
    assert cfg.oauth is not None and cfg.oauth.provider == "google"


# ---------- Microsoft token (mocked HTTP) ----------


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)


async def test_ms_app_token_mocked(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "MSTOKEN", "expires_in": 3599})

    _patch_httpx(monkeypatch, handler)
    o = OAuthSettings(provider="microsoft", mailbox="a@acme.com",
                      tenant_id="tenant-123", client_id="cid", client_secret="secret")
    token = await get_access_token(o)
    assert token == "MSTOKEN"
    assert "tenant-123/oauth2/v2.0/token" in captured["url"]
    assert "grant_type=client_credentials" in captured["body"]
    assert "outlook.office365.com" in captured["body"]  # scope


async def test_get_xoauth2_string_microsoft(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"access_token": "MSTOKEN"})

    _patch_httpx(monkeypatch, handler)
    o = OAuthSettings(provider="microsoft", mailbox="analyst@acme.com",
                      tenant_id="t", client_id="c", client_secret="s")
    sasl = await get_xoauth2_string(o)
    decoded = base64.b64decode(sasl).decode()
    assert "user=analyst@acme.com" in decoded
    assert "auth=Bearer MSTOKEN" in decoded


# ---------- Google token (mock the signing/refresh) ----------


async def test_google_token_dispatch(monkeypatch):
    def fake_blocking(info, subject, scope="https://mail.google.com/"):
        assert subject == "analyst@acme.com"
        assert info["client_email"] == "sa@p.iam"
        return "GTOKEN"

    monkeypatch.setattr(oauth, "_get_google_dwd_token_blocking", fake_blocking)
    o = OAuthSettings(provider="google", mailbox="analyst@acme.com",
                      service_account_info={"client_email": "sa@p.iam"})
    token = await get_access_token(o)
    assert token == "GTOKEN"


async def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        await get_access_token(OAuthSettings(provider="aol", mailbox="x@y"))
