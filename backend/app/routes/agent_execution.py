from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.agent.context_snapshot_service import ContextSnapshotService
from app.models.agent_execution import AgentExecution
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.console_access import ConsoleScope, console_identity
from app.schemas.agent_execution_schema import ContextSnapshotSchema
from app.schemas.agent_execution_trace_schema import AgentExecutionTraceResponse, ConversationTraceResponse
from app.services.console_service import ConsoleService
from app.services.diagnosis.service import diagnosis_service

import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent_execution"])
context_snapshot_service = ContextSnapshotService()
console_service = ConsoleService()


async def _assert_execution_visible(
    db: AsyncSession, organization: Organization, scope: ConsoleScope, agent_execution_id: str
) -> None:
    """Gate an execution-level drill-down by the report it belongs to.

    Org-wide callers short-circuit; a scoped agent manager may only open a run
    that reached only agents they manage, with no earlier turn of its report
    that reached another one (ConsoleScope.assert_run_visible).
    """
    report_id = (await db.execute(
        select(AgentExecution.report_id).where(
            AgentExecution.id == agent_execution_id,
            AgentExecution.organization_id == organization.id,
        )
    )).scalar_one_or_none()
    if not report_id:
        raise HTTPException(status_code=404, detail="Agent execution not found")
    await scope.assert_run_visible(db, agent_execution_id, str(report_id))


async def _trace(db, organization, user, execution_id):
    from app.core.permission_resolver import resolve_permissions
    from app.models.folder import Folder
    from app.models.data_source import DataSource
    from app.models.agent_execution import folder_execution_agents

    trace = await console_service.get_agent_execution_trace(db, organization, execution_id)
    trace.agents = (await diagnosis_service.agents_for_runs(db, [execution_id])).get(str(execution_id), [])
    primary, folders = (await db.execute(select(AgentExecution.starting_folder_id, AgentExecution.starting_folder_ids)
        .where(AgentExecution.id == execution_id))).one()
    if primary:
        rights = await resolve_permissions(db, user.id, organization.id)
        visible = [fid for fid in folders or [primary] if rights.has_resource_permission("folder", fid, "view")]
        names = dict((await db.execute(select(Folder.id, Folder.name).where(Folder.id.in_(visible),
            Folder.organization_id == organization.id, Folder.deleted_at.is_(None)))).all())
        trace.folder_names = [names[fid] for fid in visible if fid in names]
        trace.folder_name = names.get(primary)
        trace.used_agents = list(await db.scalars(select(DataSource.name).join(folder_execution_agents,
            folder_execution_agents.c.agent_id == DataSource.id).where(
                folder_execution_agents.c.execution_id == execution_id,
                DataSource.organization_id == organization.id, DataSource.deleted_at.is_(None)).order_by(DataSource.name)))
    return trace


@router.get("/console/agent_executions/{agent_execution_id}/context_snapshot/{id}", response_model=ContextSnapshotSchema)
async def get_context_snapshot(
    agent_execution_id: str,
    id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_identity)
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
    scope: ConsoleScope = Depends(console_identity)
):
    """Get agent execution with completion blocks and minimal metadata for the trace modal."""
    await _assert_execution_visible(db, organization, scope, agent_execution_id)
    return await _trace(db, organization, current_user, agent_execution_id)

@router.get("/console/agent_executions/by-completion/{completion_id}", response_model=AgentExecutionTraceResponse)
async def get_agent_execution_trace_by_completion(
    completion_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_identity)
):
    """Get agent execution trace for the latest execution attached to a completion."""
    ae_id = (await db.execute(
        select(AgentExecution.id).where(
            AgentExecution.completion_id == completion_id,
            AgentExecution.organization_id == organization.id,
        ).order_by(AgentExecution.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not ae_id:
        raise HTTPException(status_code=404, detail="Agent execution not found for completion")
    await _assert_execution_visible(db, organization, scope, str(ae_id))
    return await _trace(db, organization, current_user, ae_id)

@router.get("/console/reports/{report_id}/conversation", response_model=ConversationTraceResponse)
async def get_report_conversation(
    report_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_identity)
):
    """Get the whole report conversation as ordered turns for the trace modal."""
    runs = await scope.assert_report_partly_visible(db, report_id)
    return await console_service.get_report_conversation(
        db, organization, report_id, viewer_id=str(current_user.id), visible_run_ids=runs
    )
