import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.webhook import Webhook
from app.services.webhook_service import webhook_service
from app.services.webhook_adapters.factory import WebhookAdapterFactory
from app.settings.logging_config import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/webhooks/{token}")
async def receive_webhook(token: str, request: Request):
    """Inbound webhook endpoint (no session auth — per-webhook HMAC/token verified).

    Verifies fast and synchronously (so spoofed/duplicate/disabled requests get a
    proper status), then does classification + agent work in the background.
    """
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    query = dict(request.query_params)

    async with async_session_maker() as db:
        wh = (await db.execute(
            select(Webhook).where(Webhook.token == token, Webhook.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not wh or not wh.is_active:
            return JSONResponse(status_code=404, content={"detail": "Webhook not found"})

        # Org-wide master switch
        if not await webhook_service._flag_enabled(db, str(wh.organization_id)):
            return JSONResponse(status_code=403, content={"detail": "Report webhooks are disabled for this organization"})

        # Verify signature / token (per auth_mode)
        if not webhook_service.verify(wh, raw_body, headers, query):
            logger.warning("Webhook %s: signature/token verification failed", wh.id)
            return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

        # Parse JSON payload (after verification)
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

        # Protocol handshake (e.g. GitHub ping) → 200 no-op
        adapter = WebhookAdapterFactory.create(wh.source)
        if adapter.is_handshake(headers, payload):
            return JSONResponse(status_code=200, content={"status": "pong"})

        # Per-org rate limit
        limit = await webhook_service._rate_limit(db, str(wh.organization_id))
        if not webhook_service.check_rate_limit(str(wh.organization_id), limit):
            logger.warning("Webhook %s: rate limited (org %s)", wh.id, wh.organization_id)
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

        webhook_id = str(wh.id)

    # Heavy work (event entry, classify, agent) runs in the background.
    asyncio.create_task(webhook_service.process_delivery(webhook_id, payload, headers))
    return JSONResponse(status_code=200, content={"status": "accepted"})
