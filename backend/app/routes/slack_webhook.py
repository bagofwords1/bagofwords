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
        
        # Check if this is a message event
        if event_data.get("type") != "event_callback" or event.get("type") != "message":
            print(f"Ignoring non-message event: {event_data.get('type')} - {event.get('type')}")
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
