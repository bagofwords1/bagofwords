"""End-to-end sandbox debug harness for the WhatsApp integration.

Runs **without** the full bagofwords app stack. It wires together:

  1. A mock Meta Graph API (captures outbound /messages and /media calls)
  2. The real `WhatsAppAdapter` pointed at the mock via WHATSAPP_GRAPH_BASE_URL
  3. The real `whatsapp_webhook` FastAPI route, with an in-memory fake DB
  4. A scripted sequence of inbound webhooks (new user -> verification ->
     verified message -> reply-in-thread) exercising every code path

Run:   python backend/scripts/whatsapp_sandbox_debug.py
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


# ---------------------------------------------------------------------------
# 1. Stub the heavy app dep chain so we can import the adapter + route cleanly
# ---------------------------------------------------------------------------


def _install_stubs():
    # app.models.external_platform / external_user_mapping
    models_pkg = types.ModuleType("app.models")
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
    eum_mod = types.ModuleType("app.models.external_user_mapping")

    class ExternalUserMapping:  # sentinel
        pass

    eum_mod.ExternalUserMapping = ExternalUserMapping

    # app.settings.config
    settings_pkg = types.ModuleType("app.settings")
    config_mod = types.ModuleType("app.settings.config")
    config_mod.settings = types.SimpleNamespace(
        bow_config=types.SimpleNamespace(base_url="http://localhost:3000")
    )

    # app.dependencies.get_async_db (not actually hit; overridden below)
    deps_mod = types.ModuleType("app.dependencies")

    async def _fake_db():
        yield None

    deps_mod.get_async_db = _fake_db

    # app.services.external_platform_manager — record inbound calls and
    # drive the adapter end-to-end (reply back via the mock Meta).
    epm_mod = types.ModuleType("app.services.external_platform_manager")

    class ExternalPlatformManager:
        """Minimal in-process version: verified-user loop only.

        Behaviour:
          - First inbound from an unknown wa_id -> "send verification message"
          - Subsequent inbound from a verified wa_id -> "reply with summary"
        """

        verified_users: set = set()
        events: list = []

        async def handle_incoming_message(self, db, platform_type, org_id, event_data):
            # Late import so we use the same stubbed modules.
            from app.services.platform_adapters.whatsapp_adapter import WhatsAppAdapter  # type: ignore

            # The route already fetched the platform; reconstruct here from the
            # harness-global `HARNESS_PLATFORM`.
            adapter = WhatsAppAdapter(HARNESS_PLATFORM)
            parsed = await adapter.process_incoming_message(event_data)
            if not parsed:
                return {"success": True, "action": "noop"}
            wa_id = parsed["external_user_id"]
            thread_ts = parsed["thread_ts"]
            message_ts = parsed["message_ts"]

            if wa_id not in type(self).verified_users:
                type(self).events.append(("verification_sent", wa_id))
                await adapter.send_verification_message(wa_id, None, "sandbox-token-123")
                type(self).verified_users.add(wa_id)  # auto-"verify" for harness
                return {"success": True, "action": "verification_sent"}

            type(self).events.append(("message_processed", wa_id, parsed["message_text"], thread_ts))
            await adapter.add_reaction(wa_id, message_ts, "eyes")
            await adapter.send_dm_in_thread(
                wa_id,
                f"You said: {parsed['message_text']}",
                thread_ts=thread_ts,
            )
            await adapter.remove_reaction(wa_id, message_ts, "eyes")
            return {"success": True, "action": "message_processed"}

    epm_mod.ExternalPlatformManager = ExternalPlatformManager

    # app.services.external_platform_service (only the class ref is needed)
    eps_mod = types.ModuleType("app.services.external_platform_service")

    class ExternalPlatformService:
        pass

    eps_mod.ExternalPlatformService = ExternalPlatformService

    # Register everything
    sys.modules.setdefault("app", types.ModuleType("app"))
    sys.modules["app.models"] = models_pkg
    sys.modules["app.models.external_platform"] = ep_mod
    sys.modules["app.models.external_user_mapping"] = eum_mod
    sys.modules["app.settings"] = settings_pkg
    sys.modules["app.settings.config"] = config_mod
    sys.modules["app.dependencies"] = deps_mod
    sys.modules["app.services"] = types.ModuleType("app.services")
    sys.modules["app.services.external_platform_manager"] = epm_mod
    sys.modules["app.services.external_platform_service"] = eps_mod

    # platform_adapters package path
    pa_pkg = types.ModuleType("app.services.platform_adapters")
    pa_pkg.__path__ = [str(BACKEND_ROOT / "app" / "services" / "platform_adapters")]
    sys.modules["app.services.platform_adapters"] = pa_pkg

    # Load base_adapter and whatsapp_adapter from file
    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    _load(
        "app.services.platform_adapters.base_adapter",
        BACKEND_ROOT / "app" / "services" / "platform_adapters" / "base_adapter.py",
    )
    _load(
        "app.services.platform_adapters.whatsapp_adapter",
        BACKEND_ROOT / "app" / "services" / "platform_adapters" / "whatsapp_adapter.py",
    )

    # Load the route
    _load(
        "app.routes.whatsapp_webhook",
        BACKEND_ROOT / "app" / "routes" / "whatsapp_webhook.py",
    )

    return ExternalPlatform, ExternalPlatformManager


_ExternalPlatform, _EPM = _install_stubs()

# Harness-global platform row used by both the route (via a patched lookup)
# and the stub manager (so the adapter gets real creds).
APP_SECRET = "topsecret"
VERIFY_TOKEN = "verify-xyz"
PHONE_NUMBER_ID = "PNID_1"
HARNESS_PLATFORM = _ExternalPlatform(
    config={
        "phone_number_id": PHONE_NUMBER_ID,
        "waba_id": "WABA_1",
        "display_phone_number": "+15550000001",
        "verified_name": "Sandbox Co",
    },
    credentials={
        "access_token": "EAAsandbox",
        "phone_number_id": PHONE_NUMBER_ID,
        "waba_id": "WABA_1",
        "app_secret": APP_SECRET,
        "verify_token": VERIFY_TOKEN,
    },
    org_id="org-1",
)


# ---------------------------------------------------------------------------
# 2. Mock Meta Graph API server
# ---------------------------------------------------------------------------


mock_meta = FastAPI(title="Mock Meta Graph")
OUTBOUND: list = []


@mock_meta.post("/{phone_number_id}/messages")
async def mock_messages(phone_number_id: str, request: Request):
    body = await request.json()
    OUTBOUND.append(("messages", phone_number_id, body))
    return {"messages": [{"id": f"wamid.MOCK{len(OUTBOUND)}"}]}


@mock_meta.post("/{phone_number_id}/media")
async def mock_media(phone_number_id: str, request: Request):
    OUTBOUND.append(("media", phone_number_id, "<multipart>"))
    return {"id": f"media-mock-{len(OUTBOUND)}"}


@mock_meta.get("/{phone_number_id}")
async def mock_phone_info(phone_number_id: str):
    return {
        "id": phone_number_id,
        "display_phone_number": "+15550000001",
        "verified_name": "Sandbox Co",
    }


# ---------------------------------------------------------------------------
# 3. Build the BoW app exposing only the whatsapp_webhook route
# ---------------------------------------------------------------------------


def build_bow_app():
    wh = sys.modules["app.routes.whatsapp_webhook"]
    from app.dependencies import get_async_db

    app = FastAPI(title="BoW sandbox")
    app.include_router(wh.router)

    # Fake DB (never actually queried after our patches)
    class _FakeDB:
        async def execute(self, stmt):
            raise RuntimeError("DB not available in sandbox")

    async def _db_dep():
        yield _FakeDB()

    app.dependency_overrides[get_async_db] = _db_dep

    # Patch the platform lookup helpers to return our in-memory HARNESS_PLATFORM
    async def _find(db, phone_number_id):
        if phone_number_id == PHONE_NUMBER_ID:
            return HARNESS_PLATFORM
        return None

    async def _list(db):
        return [HARNESS_PLATFORM]

    wh._find_platform_by_phone_number_id = _find  # type: ignore
    wh._list_whatsapp_platforms = _list  # type: ignore
    wh.processed_message_ids.clear()
    return app


# ---------------------------------------------------------------------------
# 4. Scripted client: replay inbound events
# ---------------------------------------------------------------------------


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def make_inbound(text: str, msg_id: str, reply_to: str | None = None):
    msg = {
        "from": "15551234567",
        "id": msg_id,
        "timestamp": str(int(time.time())),
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
                                "phone_number_id": PHONE_NUMBER_ID,
                                "display_phone_number": "+15550000001",
                            },
                            "contacts": [
                                {"wa_id": "15551234567", "profile": {"name": "Sandbox User"}}
                            ],
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }


async def run_scenarios(bow_base: str):
    async with httpx.AsyncClient() as client:
        # --- GET handshake ---
        r = await client.get(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "sandbox-challenge",
            },
        )
        print(f"[GET handshake]             status={r.status_code} body={r.text!r}")
        assert r.status_code == 200 and r.text == "sandbox-challenge"

        r = await client.get(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong"},
        )
        print(f"[GET handshake bad token]   status={r.status_code}")
        assert r.status_code == 403

        # --- POST invalid signature ---
        body = json.dumps(make_inbound("hi", "wamid.X1")).encode()
        r = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body,
            headers={"x-hub-signature-256": "sha256=deadbeef"},
        )
        print(f"[POST bad signature]        status={r.status_code}")
        assert r.status_code == 401

        # --- POST unverified user -> verification sent ---
        body = json.dumps(make_inbound("hello bot", "wamid.M1")).encode()
        r = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body,
            headers={"x-hub-signature-256": sign(body)},
        )
        print(f"[POST new user]             status={r.status_code} json={r.json()}")
        assert r.status_code == 200

        # --- POST verified user top-level message ---
        body = json.dumps(make_inbound("show orders", "wamid.M2")).encode()
        r = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body,
            headers={"x-hub-signature-256": sign(body)},
        )
        print(f"[POST verified top-level]   status={r.status_code}")

        # --- POST thread reply ---
        body = json.dumps(make_inbound("and last month?", "wamid.M3", reply_to="wamid.M2")).encode()
        r = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body,
            headers={"x-hub-signature-256": sign(body)},
        )
        print(f"[POST thread reply]         status={r.status_code}")

        # --- POST status-only (delivered) — should be no-op ---
        status_payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                                "statuses": [{"id": "wamid.S1", "status": "delivered"}],
                            },
                        }
                    ]
                }
            ],
        }
        body = json.dumps(status_payload).encode()
        r = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body,
            headers={"x-hub-signature-256": sign(body)},
        )
        print(f"[POST status-only]          status={r.status_code} json={r.json()}")

        # --- POST duplicate (dedupe) ---
        body = json.dumps(make_inbound("dup", "wamid.DUP")).encode()
        headers = {"x-hub-signature-256": sign(body)}
        r1 = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body, headers=headers,
        )
        r2 = await client.post(
            f"{bow_base}/api/settings/integrations/whatsapp/webhook",
            content=body, headers=headers,
        )
        print(f"[POST dedupe]               first={r1.status_code} second={r2.status_code}")


def _run_server(app, port: int):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())


def main():
    # Point the adapter at our mock Meta server
    mock_port = 8765
    bow_port = 8766
    os.environ["WHATSAPP_GRAPH_BASE_URL"] = f"http://127.0.0.1:{mock_port}"

    # Start mock Meta Graph in a background thread
    meta_thread = threading.Thread(
        target=_run_server, args=(mock_meta, mock_port), daemon=True
    )
    meta_thread.start()

    # Start BoW webhook server in a background thread
    bow_app = build_bow_app()
    bow_thread = threading.Thread(
        target=_run_server, args=(bow_app, bow_port), daemon=True
    )
    bow_thread.start()

    # Wait for both servers
    time.sleep(1.2)

    print("=" * 70)
    print("WhatsApp sandbox debug harness")
    print(f"  Mock Meta Graph: http://127.0.0.1:{mock_port}")
    print(f"  BoW webhook:     http://127.0.0.1:{bow_port}")
    print("=" * 70)

    asyncio.run(run_scenarios(f"http://127.0.0.1:{bow_port}"))

    print()
    print("-- Manager events recorded --")
    for e in _EPM.events:
        print(f"  {e}")
    print()
    print("-- Outbound calls captured by mock Meta --")
    for kind, pnid, body in OUTBOUND:
        if kind == "messages":
            typ = body.get("type")
            if typ == "text":
                print(f"  [text  ] to={body['to']} body={body['text']['body']!r} ctx={body.get('context')}")
            elif typ == "reaction":
                print(f"  [react ] to={body['to']} msg={body['reaction']['message_id']} emoji={body['reaction']['emoji']!r}")
            else:
                print(f"  [{typ}] {body}")
        else:
            print(f"  [media ] pnid={pnid}")

    print()
    print("PASS — all scenarios executed")


if __name__ == "__main__":
    main()
