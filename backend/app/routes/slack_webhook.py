from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.external_platform_service import ExternalPlatformService
from app.models.external_platform import ExternalPlatform
import json
import hmac
import hashlib
import time

router = APIRouter(tags=["slack-webhook"])

platform_manager = ExternalPlatformManager()
platform_service = ExternalPlatformService()

# Simple in-memory set to track processed events
processed_events = set()

@router.post("/api/settings/integrations/slack/webhook")
async def slack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_async_db)
):
    """Handle Slack webhook events"""
    
    try:
        # Get request body
        body = await request.body()
        body_text = body.decode('utf-8')
        
        # Get headers
        slack_signature = request.headers.get('x-slack-signature', '')
        slack_timestamp = request.headers.get('x-slack-request-timestamp', '')
        
        # Parse JSON
        event_data = json.loads(body_text)
        
        # Handle URL verification challenge
        if event_data.get('type') == 'url_verification':
            return {"challenge": event_data.get('challenge')}
        
        # Verify signature (we'll implement this later)
        # if not verify_slack_signature(body, slack_signature, slack_timestamp):
        #     raise HTTPException(status_code=401, detail="Invalid signature")
        
        # Get team ID to find the right platform
        team_id = event_data.get('team_id')
        if not team_id:
            raise HTTPException(status_code=400, detail="No team_id in event")
        
        # Find platform by team ID
        platform = await find_platform_by_team_id(db, team_id)
        if not platform:
            print(f"No platform found for team_id: {team_id}")
            return {"ok": True}  # Don't fail, just ignore
        
        # Check if this is a bot message (ignore bot messages to prevent loops)
        event = event_data.get("event", {})
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            print("Ignoring bot message to prevent loops")
            return {"ok": True}

        # Check if this is an event_callback
        if event_data.get("type") != "event_callback":
            print(f"Ignoring non-event_callback: {event_data.get('type')}")
            return {"ok": True}

        # Accept both 'message' (DMs) and 'app_mention' (channel mentions) events
        event_type = event.get("type")
        if event_type not in ("message", "app_mention"):
            print(f"Ignoring unsupported event type: {event_type}")
            return {"ok": True}

        # Simple deduplication using event_id
        event_id = event_data.get('event_id')
        if event_id in processed_events:
            print(f"Event {event_id} already processed, skipping")
            return {"ok": True}

        # Mark as processed
        processed_events.add(event_id)

        # Clean up if set gets too large (keep only last 1000 events)
        if len(processed_events) > 1000:
            processed_events.clear()

        # For DMs (message events), only process if channel_type is 'im'
        # For app_mention events, process regardless of channel type (they come from channels)
        if event_type == "message" and event.get('channel_type') != 'im':
            print(f"Ignoring non-DM message: {event.get('channel_type')}")
            return {"ok": True}

        # Skip if message is empty
        if not event.get('text', '').strip():
            print("Ignoring empty message")
            return {"ok": True}
        
        # Handle the event
        result = await platform_manager.handle_incoming_message(
            db, "slack", platform.organization_id, event_data
        )
        
        return {"ok": True, "result": result}
        
    except Exception as e:
        print(f"Error in Slack webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def find_platform_by_team_id(db: AsyncSession, team_id: str) -> ExternalPlatform:
    """Find platform by Slack team ID"""
    
    from sqlalchemy import select
    
    stmt = select(ExternalPlatform).where(
        ExternalPlatform.platform_type == "slack"
    )
    result = await db.execute(stmt)
    platforms = result.scalars().all()
    
    for platform in platforms:
        config = platform.platform_config
        if config.get("team_id") == team_id:
            return platform
    
    return None
