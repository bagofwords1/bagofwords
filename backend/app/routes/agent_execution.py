from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.agent.context_snapshot_service import ContextSnapshotService
from app.models.agent_execution import AgentExecution
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.console_access import ConsoleScope, console_scope
from app.schemas.agent_execution_schema import ContextSnapshotSchema
from app.schemas.agent_execution_trace_schema import AgentExecutionTraceResponse, ConversationTraceResponse
from app.services.console_service import ConsoleService

import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent_execution"])
context_snapshot_service = ContextSnapshotService()
console_service = ConsoleService()


async def _assert_execution_visible(
    db: AsyncSession, organization: Organization, scope: ConsoleScope, agent_execution_id: str
) -> None:
    """Gate an execution-level drill-down by the report it belongs to.

    Org-wide callers short-circuit; a scoped agent manager may only open runs on
    reports that draw on an agent they manage.
    """
    if scope.is_org_wide:
        return
    report_id = (await db.execute(
        select(AgentExecution.report_id).where(
            AgentExecution.id == agent_execution_id,
            AgentExecution.organization_id == organization.id,
        )
    )).scalar_one_or_none()
    if not report_id:
        raise HTTPException(status_code=404, detail="Agent execution not found")
    await scope.assert_report_visible(db, str(report_id))


@router.get("/console/agent_executions/{agent_execution_id}/context_snapshot/{id}", response_model=ContextSnapshotSchema)
async def get_context_snapshot(
    agent_execution_id: str,
    id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get context snapshot for an agent execution"""
    await _assert_execution_visible(db, organization, scope, agent_execution_id)
    return await context_snapshot_service.get_context_snapshot(db, agent_execution_id, id)

@router.get("/console/agent_executions/{agent_execution_id}", response_model=AgentExecutionTraceResponse)
async def get_agent_execution_trace(
    agent_execution_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get agent execution with completion blocks and minimal metadata for the trace modal."""
    await _assert_execution_visible(db, organization, scope, agent_execution_id)
    return await console_service.get_agent_execution_trace(db, organization, agent_execution_id)

@router.get("/console/agent_executions/by-completion/{completion_id}", response_model=AgentExecutionTraceResponse)
async def get_agent_execution_trace_by_completion(
    completion_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get agent execution trace for the latest execution attached to a completion."""
    if not scope.is_org_wide:
        ae_id = (await db.execute(
            select(AgentExecution.id).where(
                AgentExecution.completion_id == completion_id,
                AgentExecution.organization_id == organization.id,
            ).order_by(AgentExecution.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if not ae_id:
            raise HTTPException(status_code=404, detail="Agent execution not found for completion")
        await _assert_execution_visible(db, organization, scope, str(ae_id))
    return await console_service.get_agent_execution_trace_by_completion(db, organization, completion_id)

@router.get("/console/reports/{report_id}/conversation", response_model=ConversationTraceResponse)
async def get_report_conversation(
    report_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get the whole report conversation as ordered turns for the trace modal."""
    await scope.assert_report_visible(db, report_id)
    return await console_service.get_report_conversation(db, organization, report_id)
