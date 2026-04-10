"""WhatsApp Cloud API webhook.

Mirrors slack_webhook.py's style:
- Router is always mounted; effective "enablement" is the existence of an
  ExternalPlatform row of type `whatsapp` whose `platform_config.phone_number_id`
  matches the inbound event's metadata.
- GET handles Meta's verification handshake.
- POST verifies X-Hub-Signature-256 (when an app_secret is configured), dedupes
  by message id, then dispatches to ExternalPlatformManager.
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import json
import hmac
import hashlib

from app.dependencies import get_async_db
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.external_platform_service import ExternalPlatformService
from app.models.external_platform import ExternalPlatform


router = APIRouter(tags=["whatsapp-webhook"])

platform_manager = ExternalPlatformManager()
platform_service = ExternalPlatformService()

# Simple in-memory dedupe (same pattern as Slack webhook)
processed_message_ids: set = set()


async def _list_whatsapp_platforms(db: AsyncSession):
    """List all WhatsApp ExternalPlatform rows (used by verify handshake)."""
    stmt = select(ExternalPlatform).where(ExternalPlatform.platform_type == "whatsapp")
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _find_platform_by_phone_number_id(
    db: AsyncSession, phone_number_id: str
) -> ExternalPlatform | None:
    """Look up the WhatsApp ExternalPlatform row for a given phone_number_id."""
    if not phone_number_id:
        return None
    for platform in await _list_whatsapp_platforms(db):
        config = platform.platform_config or {}
        if config.get("phone_number_id") == phone_number_id:
            return platform
    return None


@router.get("/api/settings/integrations/whatsapp/webhook")
async def whatsapp_webhook_verify(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """Meta verification handshake.

    Meta calls GET with query params:
      hub.mode=subscribe
      hub.verify_token=<what-you-configured>
      hub.challenge=<random>
    We must echo back the challenge if the verify_token matches any configured
    WhatsApp platform's stored verify_token.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode != "subscribe" or not token:
        raise HTTPException(status_code=400, detail="Invalid verification request")

    # Match against any configured WhatsApp platform
    for platform in await _list_whatsapp_platforms(db):
        try:
            creds = platform.decrypt_credentials() or {}
        except Exception:
            continue
        if creds.get("verify_token") == token:
            # Meta expects the bare challenge as plain text
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(challenge or "")

    raise HTTPException(status_code=403, detail="Verify token mismatch")


@router.post("/api/settings/integrations/whatsapp/webhook")
async def whatsapp_webhook(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """Handle WhatsApp inbound events."""
    try:
        body = await request.body()
        try:
            event_data = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        if event_data.get("object") != "whatsapp_business_account":
            # Not a WhatsApp payload — acknowledge and ignore
            return {"ok": True}

        # Extract phone_number_id from the payload to find the right platform
        entry = (event_data.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}
        metadata = value.get("metadata") or {}
        phone_number_id = metadata.get("phone_number_id")

        platform = await _find_platform_by_phone_number_id(db, phone_number_id)
        if not platform:
            print(f"No WhatsApp platform found for phone_number_id: {phone_number_id}")
            return {"ok": True}

        # Signature verification
        signature = request.headers.get("x-hub-signature-256", "")
        try:
            creds = platform.decrypt_credentials() or {}
        except Exception:
            creds = {}
        app_secret = creds.get("app_secret")
        if app_secret:
            expected = "sha256=" + hmac.new(
                app_secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            if not signature or not hmac.compare_digest(expected, signature):
                raise HTTPException(status_code=401, detail="Invalid signature")

        # Status-only payloads (delivered/read/sent) have no `messages`
        messages = value.get("messages") or []
        if not messages:
            return {"ok": True}

        # Dedupe by message id
        message_id = messages[0].get("id")
        if message_id and message_id in processed_message_ids:
            print(f"WhatsApp message {message_id} already processed, skipping")
            return {"ok": True}
        if message_id:
            processed_message_ids.add(message_id)
            if len(processed_message_ids) > 1000:
                processed_message_ids.clear()

        # Only text messages flow to the agent in v1
        if messages[0].get("type") != "text":
            print(f"Ignoring non-text WhatsApp message type: {messages[0].get('type')}")
            return {"ok": True}

        if not (messages[0].get("text") or {}).get("body", "").strip():
            return {"ok": True}

        result = await platform_manager.handle_incoming_message(
            db, "whatsapp", platform.organization_id, event_data
        )
        return {"ok": True, "result": result}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in WhatsApp webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
