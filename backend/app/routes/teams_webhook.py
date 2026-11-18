import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.models.external_platform import ExternalPlatform
import json

router = APIRouter(tags=["teams-webhook"])

platform_manager = ExternalPlatformManager()

# Simple in-memory set to track processed events
processed_events = set()


@router.post("/api/settings/integrations/teams/webhook")
async def teams_webhook(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """Handle Microsoft Teams webhook activities (Bot Connector v4)"""

    try:
        body = await request.body()
        activity = json.loads(body.decode("utf-8"))

        # Only process message activities; return 200 for everything else
        activity_type = activity.get("type")
        if activity_type != "message":
            print(f"TEAMS: Ignoring activity type: {activity_type}")
            return {"ok": True}

        # Extract tenant ID to find the right platform
        tenant_id = activity.get("channelData", {}).get("tenant", {}).get("id")

        # Find the platform this activity is for. One tenant can host a bot
        # per organization, so the bot's app id is part of the routing key.
        auth_header = request.headers.get("Authorization", "")
        platform = await resolve_teams_platform(
            db, tenant_id, _claimed_bot_app_ids(activity, auth_header)
        )
        if not platform:
            print(f"TEAMS: No platform found for tenant_id: {tenant_id}")
            return {"ok": True}

        # Verify JWT signature via adapter (audience = this platform's app id)
        adapter = PlatformAdapterFactory.create_adapter(platform)
        is_valid = await adapter.verify_webhook_signature(body, auth_header, "")
        if not is_valid:
            raise HTTPException(status_code=401, detail="Invalid token")

        # Skip bot's own messages
        from_id = activity.get("from", {}).get("id")
        app_id = platform.platform_config.get("app_id") or platform.decrypt_credentials().get("app_id")
        if from_id == app_id:
            print("TEAMS: Ignoring bot's own message")
            return {"ok": True}

        # Deduplication by activity ID
        activity_id = activity.get("id")
        if activity_id in processed_events:
            print(f"TEAMS: Activity {activity_id} already processed, skipping")
            return {"ok": True}
        processed_events.add(activity_id)
        if len(processed_events) > 1000:
            processed_events.clear()

        # Skip empty messages
        text = activity.get("text", "").strip()
        if not text:
            print("TEAMS: Ignoring empty message")
            return {"ok": True}

        # Persist serviceUrl and bot identity on platform_config if changed
        service_url = activity.get("serviceUrl")
        bot_recipient = activity.get("recipient", {})
        updated_config = {**platform.platform_config}
        changed = False
        if service_url and updated_config.get("service_url") != service_url:
            updated_config["service_url"] = service_url
            changed = True
        if bot_recipient.get("id") and updated_config.get("bot_id") != bot_recipient["id"]:
            updated_config["bot_id"] = bot_recipient["id"]
            updated_config["bot_name"] = bot_recipient.get("name", "")
            changed = True
        if changed:
            platform.platform_config = updated_config
            await db.commit()

        # Route to manager
        result = await platform_manager.handle_incoming_message(
            db, "teams", platform.organization_id, activity,
            platform_id=str(platform.id),
        )

        return {"ok": True, "result": result}

    except HTTPException:
        raise
    except Exception:
        # Unauthenticated caller: log the detail, return nothing about it.
        logging.getLogger(__name__).exception("Teams: Error in webhook")
        raise HTTPException(status_code=500, detail="Webhook processing failed")


def _platform_app_id(platform: ExternalPlatform) -> str:
    # Same precedence as TeamsAdapter, which validates the token audience.
    try:
        from_credentials = (platform.decrypt_credentials() or {}).get("app_id")
    except Exception:
        from_credentials = None
    return from_credentials or (platform.platform_config or {}).get("app_id") or ""


def _claimed_bot_app_ids(activity: dict, auth_header: str) -> list:
    """App ids the activity says it is addressed to, most trustworthy first.

    Only used to pick the candidate platform; the adapter then verifies the
    token against that platform's app id, so a wrong claim cannot pass.
    """
    claimed = []
    if auth_header.startswith("Bearer "):
        try:
            import jwt

            aud = jwt.decode(auth_header[7:], options={"verify_signature": False}).get("aud")
            for value in aud if isinstance(aud, list) else [aud]:
                if isinstance(value, str) and value:
                    claimed.append(value)
        except Exception:
            pass
    recipient_id = (activity.get("recipient") or {}).get("id")
    if isinstance(recipient_id, str) and recipient_id:
        # Teams addresses bots as "28:<app id>"; other channels send the bare id.
        claimed.append(recipient_id.split(":", 1)[1] if recipient_id.startswith("28:") else recipient_id)
    return claimed


def _teams_routing_order(platform: ExternalPlatform):
    created = getattr(platform, "created_at", None)
    return (
        0 if platform.is_active else 1,
        created.isoformat() if created is not None else "",
        str(platform.id),
    )


async def resolve_teams_platform(db: AsyncSession, tenant_id, claimed_app_ids) -> ExternalPlatform:
    """Pick the Teams platform for an inbound activity.

    With a single Teams platform on the deployment the historical behaviour
    is kept (tenant match, else the platform if active — Web Chat and the
    Bot Framework Emulator don't send ``channelData.tenant.id``). With
    several, the activity must name the bot's app id: there is no
    "any platform" fallback, since that would hand one organization's
    messages to another.
    """
    from sqlalchemy import select

    stmt = select(ExternalPlatform).where(ExternalPlatform.platform_type == "teams")
    result = await db.execute(stmt)
    platforms = sorted(result.scalars().all(), key=_teams_routing_order)
    if not platforms:
        return None

    if len(platforms) == 1:
        only = platforms[0]
        if tenant_id and (only.platform_config or {}).get("tenant_id") == tenant_id:
            return only
        return only if only.is_active else None

    matches = []
    for app_id in claimed_app_ids:
        matches = [p for p in platforms if _platform_app_id(p).lower() == app_id.lower()]
        if matches:
            break
    if not matches:
        logging.getLogger(__name__).warning(
            "TEAMS: activity for tenant %s does not name a connected bot app id; not routed",
            tenant_id,
        )
        return None

    if tenant_id:
        same_tenant = [p for p in matches if (p.platform_config or {}).get("tenant_id") == tenant_id]
        if same_tenant:
            matches = same_tenant
    if len(matches) > 1:
        logging.getLogger(__name__).warning(
            "TEAMS: %d platforms share bot app id %s (organizations: %s); using platform %s",
            len(matches), _platform_app_id(matches[0]),
            ", ".join(sorted({str(p.organization_id) for p in matches})), matches[0].id,
        )
    return matches[0]
