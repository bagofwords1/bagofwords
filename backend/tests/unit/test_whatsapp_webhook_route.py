"""Route-level tests for the WhatsApp webhook.

Bypasses the full app startup by building a tiny FastAPI app that only mounts
the `whatsapp_webhook.router`, and stubs the ExternalPlatform DB lookup and
platform manager so behaviour under various payloads is exercised in
isolation.
"""
import hmac
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ---------- Stub the dep chain before importing the route ----------


def _install_route_stubs():
    # Stub app.dependencies.get_async_db
    deps_mod = types.ModuleType("app.dependencies")

    async def _fake_db():  # not actually used because we override via dependency_overrides
        yield None

    deps_mod.get_async_db = _fake_db
    sys.modules.setdefault("app", types.ModuleType("app"))
    sys.modules.setdefault("app.dependencies", deps_mod)

    # Stub services.external_platform_manager with a module containing a
    # controllable class.
    epm_mod = types.ModuleType("app.services.external_platform_manager")

    class ExternalPlatformManager:
        last_call = None

        async def handle_incoming_message(self, db, platform_type, org_id, event_data):
            type(self).last_call = (platform_type, org_id, event_data)
            return {"success": True, "action": "message_processed"}

    epm_mod.ExternalPlatformManager = ExternalPlatformManager
    sys.modules.setdefault("app.services", types.ModuleType("app.services"))
    sys.modules.setdefault("app.services.external_platform_manager", epm_mod)

    # Stub external_platform_service
    eps_mod = types.ModuleType("app.services.external_platform_service")

    class ExternalPlatformService:
        pass

    eps_mod.ExternalPlatformService = ExternalPlatformService
    sys.modules.setdefault("app.services.external_platform_service", eps_mod)

    # Stub models.external_platform
    models_mod = types.ModuleType("app.models")
    ep_mod = types.ModuleType("app.models.external_platform")

    class ExternalPlatform:
        platform_type = "whatsapp"

        def __init__(self, config, credentials, org_id="org-1"):
            self.platform_config = config
            self._credentials = credentials
            self.organization_id = org_id

        def decrypt_credentials(self):
            return dict(self._credentials)

    ep_mod.ExternalPlatform = ExternalPlatform
    sys.modules.setdefault("app.models", models_mod)
    sys.modules.setdefault("app.models.external_platform", ep_mod)
    return ExternalPlatform


_ExternalPlatform = _install_route_stubs()

# Now load the route module directly from file
import importlib.util

spec = importlib.util.spec_from_file_location(
    "app.routes.whatsapp_webhook",
    BACKEND_ROOT / "app" / "routes" / "whatsapp_webhook.py",
)
wh = importlib.util.module_from_spec(spec)
sys.modules["app.routes.whatsapp_webhook"] = wh
spec.loader.exec_module(wh)


# ---------- Fixtures ----------


@pytest.fixture
def platform():
    return _ExternalPlatform(
        config={"phone_number_id": "PNID_1", "waba_id": "WABA_1"},
        credentials={"app_secret": "topsecret", "verify_token": "verify-xyz"},
        org_id="org-1",
    )


@pytest.fixture
def app(monkeypatch, platform):
    # Patch the platform lookup to return our fake platform (or None)
    async def _fake_lookup(db, phone_number_id):
        if phone_number_id == platform.platform_config["phone_number_id"]:
            return platform
        return None

    monkeypatch.setattr(wh, "_find_platform_by_phone_number_id", _fake_lookup)

    async def _fake_list(db):
        return [platform]

    monkeypatch.setattr(wh, "_list_whatsapp_platforms", _fake_list)

    # Patch the GET-handshake DB scan so no real DB is touched
    class _Scalars:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

    class _Result:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return _Scalars(self._items)

    class _FakeDB:
        platforms = [platform]

        async def execute(self, stmt):
            return _Result(self.platforms)

    async def _fake_db_dep():
        yield _FakeDB()

    # Reset dedupe between tests
    wh.processed_message_ids.clear()

    application = FastAPI()
    application.include_router(wh.router)
    # Override the get_async_db dependency the route uses
    from app.dependencies import get_async_db  # stubbed

    application.dependency_overrides[get_async_db] = _fake_db_dep
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- Helpers ----------


def _inbound_text_payload(
    phone_number_id="PNID_1", msg_id="wamid.AAA", text="hello", msg_type="text"
):
    msg = {
        "from": "15551234567",
        "id": msg_id,
        "timestamp": "1700000000",
        "type": msg_type,
    }
    if msg_type == "text":
        msg["text"] = {"body": text}
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
                                "phone_number_id": phone_number_id,
                                "display_phone_number": "+15550000001",
                            },
                            "contacts": [
                                {"wa_id": "15551234567", "profile": {"name": "Alice"}}
                            ],
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }


def _sign(body: bytes, secret="topsecret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------- GET handshake ----------


def test_verify_handshake_success(client):
    r = client.get(
        "/api/settings/integrations/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-xyz",
            "hub.challenge": "42",
        },
    )
    assert r.status_code == 200
    assert r.text == "42"


def test_verify_handshake_bad_token(client):
    r = client.get(
        "/api/settings/integrations/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "nope",
            "hub.challenge": "42",
        },
    )
    assert r.status_code == 403


def test_verify_handshake_bad_mode(client):
    r = client.get(
        "/api/settings/integrations/whatsapp/webhook",
        params={"hub.mode": "unsubscribe", "hub.verify_token": "verify-xyz"},
    )
    assert r.status_code == 400


# ---------- POST webhook ----------


def test_post_unknown_phone_number_id_is_noop(client):
    body = json.dumps(_inbound_text_payload(phone_number_id="UNKNOWN")).encode()
    r = client.post(
        "/api/settings/integrations/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_post_invalid_signature_rejected(client):
    body = json.dumps(_inbound_text_payload()).encode()
    r = client.post(
        "/api/settings/integrations/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_post_valid_text_message_dispatches(client):
    from app.services.external_platform_manager import ExternalPlatformManager  # stubbed

    ExternalPlatformManager.last_call = None
    body = json.dumps(_inbound_text_payload()).encode()
    r = client.post(
        "/api/settings/integrations/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert ExternalPlatformManager.last_call is not None
    platform_type, org_id, event = ExternalPlatformManager.last_call
    assert platform_type == "whatsapp"
    assert org_id == "org-1"
    assert event["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] == "hello"


def test_post_status_only_payload_is_noop(client):
    from app.services.external_platform_manager import ExternalPlatformManager  # stubbed

    ExternalPlatformManager.last_call = None
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PNID_1"},
                            "statuses": [
                                {"id": "wamid.S", "status": "delivered"}
                            ],
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode()
    r = client.post(
        "/api/settings/integrations/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert ExternalPlatformManager.last_call is None


def test_post_non_text_message_is_noop(client):
    from app.services.external_platform_manager import ExternalPlatformManager  # stubbed

    ExternalPlatformManager.last_call = None
    body = json.dumps(_inbound_text_payload(msg_type="image")).encode()
    r = client.post(
        "/api/settings/integrations/whatsapp/webhook",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert r.status_code == 200
    assert ExternalPlatformManager.last_call is None


def test_post_deduplication(client):
    from app.services.external_platform_manager import ExternalPlatformManager  # stubbed

    calls = []

    async def _tracking(self, db, pt, oid, ev):
        calls.append(ev["entry"][0]["changes"][0]["value"]["messages"][0]["id"])
        return {"success": True}

    ExternalPlatformManager.handle_incoming_message = _tracking  # type: ignore

    body = json.dumps(_inbound_text_payload(msg_id="wamid.DEDUP")).encode()
    headers = {"x-hub-signature-256": _sign(body)}
    r1 = client.post("/api/settings/integrations/whatsapp/webhook", content=body, headers=headers)
    r2 = client.post("/api/settings/integrations/whatsapp/webhook", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls == ["wamid.DEDUP"]  # second call skipped
