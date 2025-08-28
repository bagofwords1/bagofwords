from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.agent.context_snapshot_service import ContextSnapshotService
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.schemas.agent_execution_schema import ContextSnapshotSchema
from app.schemas.agent_execution_trace_schema import AgentExecutionTraceResponse
from app.services.console_service import ConsoleService

import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent_execution"])
context_snapshot_service = ContextSnapshotService()
console_service = ConsoleService()

@router.get("/console/agent_executions/{agent_execution_id}/context_snapshot/{id}", response_model=ContextSnapshotSchema)
@requires_permission('view_organization_overview')
async def get_context_snapshot(
    agent_execution_id: str,
    id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get context snapshot for an agent execution"""
    return await context_snapshot_service.get_context_snapshot(db, agent_execution_id, id)

@router.get("/console/agent_executions/{agent_execution_id}", response_model=AgentExecutionTraceResponse)
@requires_permission("view_organization_overview")
async def get_agent_execution_trace(
    agent_execution_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get agent execution with completion blocks and minimal metadata for the trace modal."""
    return await console_service.get_agent_execution_trace(db, organization, agent_execution_id)

@router.get("/console/agent_executions/by-completion/{completion_id}", response_model=AgentExecutionTraceResponse)
@requires_permission("view_organization_overview")
async def get_agent_execution_trace_by_completion(
    completion_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get agent execution trace for the latest execution attached to a completion."""
    return await console_service.get_agent_execution_trace_by_completion(db, organization, completion_id)
