"""Unit tests for WhatsAppAdapter.

These tests import `WhatsAppAdapter` while stubbing the heavy dependency
chain (SQLAlchemy models, settings loader) so they run independently of the
full app stack. This mirrors how Slack is exercised in other unit tests.
"""
import hmac
import hashlib
import importlib
import json
import os
import sys
import types
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _install_stubs():
    """Install minimal stand-ins for the app modules the adapter imports."""
    # app.models.external_platform — only needs ExternalPlatform symbol for type hint
    models_pkg = types.ModuleType("app.models")
    ep_mod = types.ModuleType("app.models.external_platform")
    class _EP:  # noqa: N801
        pass
    ep_mod.ExternalPlatform = _EP
    eum_mod = types.ModuleType("app.models.external_user_mapping")
    class _EUM:  # noqa: N801
        pass
    eum_mod.ExternalUserMapping = _EUM

    # app.settings.config with a minimal settings.bow_config.base_url
    settings_pkg = types.ModuleType("app.settings")
    config_mod = types.ModuleType("app.settings.config")
    bow_cfg = types.SimpleNamespace(base_url="http://localhost:3000")
    config_mod.settings = types.SimpleNamespace(bow_config=bow_cfg)

    sys.modules.setdefault("app", types.ModuleType("app"))
    sys.modules["app.models"] = models_pkg
    sys.modules["app.models.external_platform"] = ep_mod
    sys.modules["app.models.external_user_mapping"] = eum_mod
    sys.modules["app.settings"] = settings_pkg
    sys.modules["app.settings.config"] = config_mod


_install_stubs()

# Load base_adapter then whatsapp_adapter directly from file paths so we don't
# drag in the real `app` package.
import importlib.util


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_base = _load(
    "app.services.platform_adapters.base_adapter",
    BACKEND_ROOT / "app" / "services" / "platform_adapters" / "base_adapter.py",
)
# Make the package path resolve so `.base_adapter` works inside whatsapp_adapter.
pa_pkg = types.ModuleType("app.services")
pa_pkg2 = types.ModuleType("app.services.platform_adapters")
pa_pkg2.__path__ = [str(BACKEND_ROOT / "app" / "services" / "platform_adapters")]
sys.modules["app.services"] = pa_pkg
sys.modules["app.services.platform_adapters"] = pa_pkg2
sys.modules["app.services.platform_adapters.base_adapter"] = _base

wa_mod = _load(
    "app.services.platform_adapters.whatsapp_adapter",
    BACKEND_ROOT / "app" / "services" / "platform_adapters" / "whatsapp_adapter.py",
)
WhatsAppAdapter = wa_mod.WhatsAppAdapter


# ---------- Fake platform ----------


class FakePlatform:
    def __init__(self, credentials=None, config=None):
        self._credentials = credentials or {}
        self.platform_config = config or {}

    def decrypt_credentials(self):
        return dict(self._credentials)


def make_adapter(creds=None, cfg=None):
    creds = creds or {
        "access_token": "EAAtoken",
        "phone_number_id": "PNID_1",
        "waba_id": "WABA_1",
        "app_secret": "topsecret",
        "verify_token": "verify-xyz",
    }
    return WhatsAppAdapter(FakePlatform(credentials=creds, config=cfg or {"phone_number_id": "PNID_1"}))


# ---------- process_incoming_message ----------


def _inbound_text(wa_id="15551234567", text="hello", msg_id="wamid.AAA", reply_to=None):
    msg = {
        "from": wa_id,
        "id": msg_id,
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": text},
    }
    if reply_to:
        msg["context"] = {"id": reply_to}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": "PNID_1",
                                "display_phone_number": "+15550000001",
                            },
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Alice"}}],
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_process_incoming_top_level_message():
    a = make_adapter()
    out = await a.process_incoming_message(_inbound_text())
    assert out["platform_type"] == "whatsapp"
    assert out["external_user_id"] == "15551234567"
    assert out["channel_id"] == "15551234567"
    assert out["channel_type"] == "im"
    assert out["message_text"] == "hello"
    assert out["message_ts"] == "wamid.AAA"
    assert out["thread_ts"] == "wamid.AAA"  # self -> new thread root
    assert out["is_thread_reply"] is False
    assert out["phone_number_id"] == "PNID_1"
    assert out["profile_name"] == "Alice"


@pytest.mark.asyncio
async def test_process_incoming_thread_reply():
    a = make_adapter()
    out = await a.process_incoming_message(
        _inbound_text(msg_id="wamid.BBB", reply_to="wamid.AAA")
    )
    assert out["is_thread_reply"] is True
    assert out["thread_ts"] == "wamid.AAA"
    assert out["message_ts"] == "wamid.BBB"


@pytest.mark.asyncio
async def test_process_incoming_status_payload_returns_none():
    a = make_adapter()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PNID_1"},
                            "statuses": [{"id": "wamid.x", "status": "delivered"}],
                        },
                    }
                ]
            }
        ],
    }
    out = await a.process_incoming_message(payload)
    assert out is None


@pytest.mark.asyncio
async def test_process_incoming_non_text_returns_none():
    a = make_adapter()
    payload = _inbound_text()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "15551234567",
        "id": "wamid.IMG",
        "type": "image",
        "image": {"id": "media1"},
    }
    out = await a.process_incoming_message(payload)
    assert out is None


# ---------- verify_webhook_signature ----------


@pytest.mark.asyncio
async def test_verify_webhook_signature_good():
    a = make_adapter()
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert await a.verify_webhook_signature(body, sig, "") is True


@pytest.mark.asyncio
async def test_verify_webhook_signature_bad():
    a = make_adapter()
    assert await a.verify_webhook_signature(b"x", "sha256=deadbeef", "") is False
    assert await a.verify_webhook_signature(b"x", "", "") is False


# ---------- send_response (mock transport) ----------


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    # Patch httpx.AsyncClient to always use our transport.
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    return _Patched


@pytest.mark.asyncio
async def test_send_response_posts_text(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    a = make_adapter()
    ok = await a.send_response({"to": "15551234567", "text": "hi there", "thread_ts": "wamid.AAA"})
    assert ok is True
    assert "/PNID_1/messages" in captured["url"]
    assert captured["auth"] == "Bearer EAAtoken"
    body = captured["body"]
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "15551234567"
    assert body["type"] == "text"
    assert body["text"]["body"] == "hi there"
    assert body["context"] == {"message_id": "wamid.AAA"}


@pytest.mark.asyncio
async def test_send_response_handles_api_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad", "code": 131047}})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    a = make_adapter()
    ok = await a.send_response({"to": "x", "text": "y"})
    assert ok is False


@pytest.mark.asyncio
async def test_add_and_remove_reaction(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"messages": [{"id": "wamid.R"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    a = make_adapter()
    assert await a.add_reaction("15551234567", "wamid.AAA", "eyes") is True
    assert calls[-1]["reaction"]["emoji"] == "\U0001F440"
    assert calls[-1]["reaction"]["message_id"] == "wamid.AAA"
    assert await a.remove_reaction("15551234567", "wamid.AAA", "eyes") is True
    assert calls[-1]["reaction"]["emoji"] == ""


@pytest.mark.asyncio
async def test_send_verification_message(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"messages": [{"id": "wamid.V"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    a = make_adapter()
    ok = await a.send_verification_message("15551234567", None, "tok-123")
    assert ok is True
    assert "tok-123" in captured["body"]["text"]["body"]
    assert "/settings/integrations/verify/tok-123" in captured["body"]["text"]["body"]


@pytest.mark.asyncio
async def test_send_file_in_thread_uploads_and_sends(monkeypatch, tmp_path):
    f = tmp_path / "report.csv"
    f.write_text("a,b\n1,2\n")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "media-xyz"})
        # messages endpoint
        return httpx.Response(200, json={"messages": [{"id": "wamid.F"}]})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    a = make_adapter()
    ok = await a.send_file_in_thread(
        "15551234567", str(f), "Your CSV", thread_ts="wamid.AAA"
    )
    assert ok is True
    # Two calls: upload then send
    assert any(r.url.path.endswith("/media") for r in calls)
    send_req = [r for r in calls if r.url.path.endswith("/messages")][-1]
    body = json.loads(send_req.content.decode())
    assert body["type"] == "document"
    assert body["document"]["id"] == "media-xyz"
    assert body["document"]["filename"] == "report.csv"
    assert body["context"] == {"message_id": "wamid.AAA"}


@pytest.mark.asyncio
async def test_process_captures_profile_name_for_get_user_info():
    a = make_adapter()
    await a.process_incoming_message(_inbound_text())
    info = await a.get_user_info("15551234567")
    assert info["name"] == "Alice"
    assert info["email"] is None
