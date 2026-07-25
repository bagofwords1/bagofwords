from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.services.slack_event_service import handle_slack_event, find_platform_by_team_id
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
import json

router = APIRouter(tags=["slack-webhook"])


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

        # Verify the request signature when the platform has a signing secret
        # (always true for Events API installs; socket-mode installs don't
        # receive webhooks at all).
        team_id = event_data.get('team_id')
        platform = await find_platform_by_team_id(db, team_id) if team_id else None
        if platform:
            credentials = platform.decrypt_credentials()
            if credentials.get("signing_secret"):
                adapter = PlatformAdapterFactory.create_adapter(platform)
                is_valid = await adapter.verify_webhook_signature(
                    body,
                    request.headers.get('x-slack-signature', ''),
                    request.headers.get('x-slack-request-timestamp', ''),
                )
                if not is_valid:
                    raise HTTPException(status_code=401, detail="Invalid signature")

        return await handle_slack_event(db, event_data)

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in Slack webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
