from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import Optional
from app.dependencies import get_db
from app.services.completion_service import CompletionService
from app.schemas.completion_schema import CompletionCreate
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
async def create_completion(report_id: str, 
                             completion: CompletionCreate, 
                             background_tasks: BackgroundTasks,
                             current_user: User = Depends(current_user),
                             organization: Organization = Depends(get_current_organization),
                             db: AsyncSession = Depends(get_async_db)):
    return await completion_service.create_completion(db, report_id, completion, current_user, organization, background_tasks)

@router.get("/api/reports/{report_id}/completions")
@requires_permission('view_reports', model=Report)
async def get_completions(report_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.get_completions(db, report_id, organization, current_user)

@router.websocket("/ws/api/reports/{report_id}")
async def websocket_endpoint(websocket: WebSocket, report_id: str):
    print(f"=== websocket_endpoint for report {report_id} ===")
    try:
        await websocket_manager.connect(websocket, report_id)
        
        # Start keep-alive task
        keep_alive_task = asyncio.create_task(websocket_manager.keep_alive(websocket))
        
        while True:
            try:
                data = await websocket.receive_text()
                if data == "pong":  # Handle ping-pong
                    continue
                print(f"Received data: {data}")
                # Handle incoming data if necessary
            except WebSocketDisconnect:
                break
    except Exception as e:
        print(f"Error in WebSocket connection: {e}")
    finally:
        websocket_manager.disconnect(websocket, report_id)
        if 'keep_alive_task' in locals():
            keep_alive_task.cancel()

@requires_permission('modify_settings')
@router.get("/api/completions/{completion_id}/plan")
async def get_completion_plan(completion_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.get_completion_plan(db, current_user, organization, completion_id)
