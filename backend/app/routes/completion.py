from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import Optional
from app.dependencies import get_db
from app.services.completion_service import CompletionService
from app.schemas.completion_v2_schema import CompletionCreate
from app.schemas.sse_schema import SSEEvent, format_sse_event
from app.streaming.completion_stream import CompletionEventQueue
from app.websocket_manager import websocket_manager
from app.models.user import User
from app.core.auth import current_user
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
import json
import asyncio
from fastapi import Request
from fastapi.responses import StreamingResponse
import time
from app.core.permissions_decorator import requires_permission
from app.models.organization import Organization
from app.dependencies import get_current_organization
from app.models.report import Report

router = APIRouter(tags=["completions"])

completion_service = CompletionService()

@router.post("/api/reports/{report_id}/completions")
@requires_permission('create_reports')
async def create_completion(
    report_id: str,
    completion: CompletionCreate,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Unified completion endpoint.

    - Streams if: body `stream: true`, or `Accept: text/event-stream`, or `?stream=true`
    - Otherwise returns JSON response
    """
    accept_header = request.headers.get("accept", "")
    body_stream_flag = getattr(completion, "stream", None)
    query_stream_flag = request.query_params.get("stream", "false").lower() == "true"
    wants_stream = (
        (body_stream_flag is True)
        or ("text/event-stream" in accept_header.lower())
        or query_stream_flag
    )

    if wants_stream:
        return await completion_service.create_completion_stream(
            db,
            report_id,
            completion,
            current_user,
            organization,
        )

    # Default to no background execution unless explicitly overridden via `?background=true`
    background = request.query_params.get("background", "false").lower() == "true"
    return await completion_service.create_completion(
        db,
        report_id,
        completion,
        current_user,
        organization,
        background=background,
    )

@router.get("/api/reports/{report_id}/completions.legacy")
@requires_permission('view_reports', model=Report)
async def get_completions(report_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.get_completions(db, report_id, organization, current_user)

@router.websocket("/ws/api/reports/{report_id}")
async def websocket_endpoint(websocket: WebSocket, report_id: str):
    from app.settings.logging_config import get_logger
    logger = get_logger(__name__)
    
    logger.info(f"WebSocket connection initiated for report {report_id}")
    try:
        await websocket_manager.connect(websocket, report_id)
        
        # Start keep-alive task
        keep_alive_task = asyncio.create_task(websocket_manager.keep_alive(websocket))
        
        while True:
            try:
                data = await websocket.receive_text()
                if data == "pong":  # Handle ping-pong
                    continue
                logger.debug(f"Received WebSocket data for report {report_id}: {data}")
                # Handle incoming data if necessary
            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected for report {report_id}")
                break
    except Exception as e:
        logger.error(f"Error in WebSocket connection for report {report_id}: {e}")
    finally:
        websocket_manager.disconnect(websocket, report_id)
        if 'keep_alive_task' in locals():
            keep_alive_task.cancel()

@requires_permission('modify_settings')
@router.get("/api/completions/{completion_id}/plans")
async def get_completion_plans(completion_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.get_completion_plans(db, current_user, organization, completion_id)


@router.get("/api/reports/{report_id}/completions")
@requires_permission('view_reports', model=Report)
async def get_completions_v2(
    report_id: str,
    limit: int = 10,
    before: str | None = None,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """New UI-focused completions response with ordered blocks and artifacts.

    - limit: last N completions (user+system), default 10
    - before: ISO datetime cursor to fetch items strictly before it
    """
    return await completion_service.get_completions_v2(db, report_id, organization, current_user, limit=limit, before=before)

@requires_permission('create_reports')
@router.post("/api/completions/{completion_id}/sigkill")
async def update_completion_sigkill(completion_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.update_completion_sigkill(db, completion_id)