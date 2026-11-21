import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.settings.config import settings
from app.services.slack_event_service import (
    find_platforms_by_team_id,
    handle_slack_event,
    resolve_slack_candidates,
)
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
import json

router = APIRouter(tags=["slack-webhook"])

logger = logging.getLogger(__name__)


# Platforms already warned about in this process (one warning each).
_unsigned_warned: set = set()


async def _legacy_unsigned_platform(db: AsyncSession, team_id: str):
    """An Events API platform stored without a signing secret, kept working.

    Only when it is the sole Slack platform for the workspace on this
    deployment (so the event cannot belong to another organization), it is
    not a Socket Mode platform, and ``security.slack_require_signed_events``
    is off. Returns None otherwise.
    """
    if settings.bow_config.security.slack_require_signed_events:
        return None
    matches = await find_platforms_by_team_id(db, team_id)
    if len(matches) != 1:
        return None
    only = matches[0]
    if (only.platform_config or {}).get("connection_mode") == "socket_mode":
        return None
    try:
        if only.decrypt_credentials().get("signing_secret"):
            return None  # it has a secret: the signature must verify
    except Exception:
        return None
    if str(only.id) not in _unsigned_warned:
        _unsigned_warned.add(str(only.id))
        logger.warning(
            "Slack platform %s accepts unsigned events; add a signing secret "
            "(or set security.slack_require_signed_events to refuse them)",
            only.id,
        )
    return only


def _unroutable(team_id):
    if not team_id:
        return {"ok": False, "error": "No team_id in event"}
    logger.info("SLACK: no platform for team_id %s", team_id)
    return {"ok": True, "skipped": "no_platform"}


@router.post("/api/settings/integrations/slack/webhook")
async def slack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_async_db)
):
    """Handle Slack Events API webhooks (legacy connection mode).

    Transport shim only: URL-verification challenge + request signature.
    Everything else (gating, dedupe, assistant events, manager handoff)
    lives in slack_event_service.handle_slack_event, shared with the
    Socket Mode listener.
    """
    try:
        body = await request.body()
        event_data = json.loads(body.decode('utf-8'))

        # URL verification challenge (sent by Slack when the Request URL is set)
        if event_data.get('type') == 'url_verification':
            return {"challenge": event_data.get('challenge')}

        # Every event must carry a valid signature for the platform it is
        # handled as. A workspace can host one Slack app per organization, so
        # the platform is the candidate whose signing secret verifies this
        # request — never just "the platform with this team id". Socket Mode
        # platforms receive events over their socket and are not served here;
        # the one unsigned exception is _legacy_unsigned_platform.
        team_id = event_data.get('team_id')
        candidates = await resolve_slack_candidates(
            db, team_id, event_data.get('api_app_id')
        )
        if not candidates:
            if team_id and await find_platforms_by_team_id(db, team_id):
                # The workspace is connected, but not through this Slack app.
                raise HTTPException(status_code=401, detail="Invalid signature")
            # Nothing is connected for this workspace: nothing to do.
            return _unroutable(team_id)

        signature = request.headers.get('x-slack-signature', '')
        timestamp = request.headers.get('x-slack-request-timestamp', '')
        platform = None
        for candidate in candidates:
            try:
                if not candidate.decrypt_credentials().get("signing_secret"):
                    continue
                adapter = PlatformAdapterFactory.create_adapter(candidate)
                if await adapter.verify_webhook_signature(body, signature, timestamp):
                    platform = candidate
                    break
            except Exception:
                logger.exception("Slack webhook: could not verify against platform %s", candidate.id)
        if platform is None:
            platform = await _legacy_unsigned_platform(db, team_id)
        if platform is None:
            raise HTTPException(status_code=401, detail="Invalid signature")

        return await handle_slack_event(db, event_data, platform_id=str(platform.id))

    except HTTPException:
        raise
    except Exception:
        # Unauthenticated caller: log the detail, return nothing about it.
        logging.getLogger(__name__).exception("Error in Slack webhook")
        raise HTTPException(status_code=500, detail="Webhook processing failed")
