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

        # Find platform by tenant ID, or fall back to first active Teams platform
        # (Web Chat and Bot Framework Emulator don't send channelData.tenant.id)
        platform = None
        if tenant_id:
            platform = await find_platform_by_tenant_id(db, tenant_id)
        if not platform:
            platform = await find_any_teams_platform(db)
        if not platform:
            print(f"TEAMS: No platform found for tenant_id: {tenant_id}")
            return {"ok": True}

        # Verify JWT signature via adapter
        auth_header = request.headers.get("Authorization", "")
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
            db, "teams", platform.organization_id, activity
        )

        return {"ok": True, "result": result}

    except HTTPException:
        raise
    except Exception as e:
        print(f"TEAMS: Error in webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def find_platform_by_tenant_id(db: AsyncSession, tenant_id: str) -> ExternalPlatform:
    """Find platform by Teams tenant ID"""
    from sqlalchemy import select

    stmt = select(ExternalPlatform).where(
        ExternalPlatform.platform_type == "teams"
    )
    result = await db.execute(stmt)
    platforms = result.scalars().all()

    for platform in platforms:
        config = platform.platform_config
        if config.get("tenant_id") == tenant_id:
            return platform

    return None


async def find_any_teams_platform(db: AsyncSession) -> ExternalPlatform:
    """Fallback: find any active Teams platform (for Web Chat / Emulator testing)"""
    from sqlalchemy import select

    stmt = select(ExternalPlatform).where(
        ExternalPlatform.platform_type == "teams",
        ExternalPlatform.is_active == True,
    )
    result = await db.execute(stmt)
    return result.scalars().first()
