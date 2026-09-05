"""Agent-scoped examples from recorded step/table lineage, never name matching."""
from sqlalchemy import select, func, case, and_
from sqlalchemy.orm import aliased
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.report import Report
from app.models.table_usage_event import TableUsageEvent
from app.models.tool_execution import ToolExecution
from app.errors import AppError, ErrorCode


async def get_table_prompts(db, organization_id: str, data_source_id: str, table_id: str, offset: int, limit: int):
    table = await db.scalar(select(DataSourceTable.id).join(DataSource).where(
        DataSourceTable.id == table_id, DataSourceTable.datasource_id == data_source_id,
        DataSource.organization_id == organization_id, DataSource.deleted_at.is_(None), DataSourceTable.deleted_at.is_(None),
    ))
    if table is None:
        raise AppError.not_found(ErrorCode.DATA_SOURCE_NOT_FOUND)

    # Only exact, persisted agent/table attribution qualifies. Legacy events
    # without a schema-row link are intentionally excluded, as are executions
    # without the tool -> step link. A report's current roster is not evidence.
    usage = (select(
        AgentExecution.id.label('execution_id'), AgentExecution.completion_id,
        AgentExecution.report_id, func.max(TableUsageEvent.used_at).label('used_at'),
        func.min(case((and_(ToolExecution.success.is_(True), TableUsageEvent.success.is_(True)), 1), else_=0)).label('success'),
    ).join(ToolExecution, ToolExecution.agent_execution_id == AgentExecution.id)
      .join(TableUsageEvent, TableUsageEvent.step_id == ToolExecution.created_step_id)
      .join(Report, Report.id == AgentExecution.report_id)
      .where(
          AgentExecution.organization_id == organization_id, Report.organization_id == organization_id,
          AgentExecution.deleted_at.is_(None), Report.deleted_at.is_(None),
          ToolExecution.deleted_at.is_(None), TableUsageEvent.deleted_at.is_(None),
          TableUsageEvent.org_id == organization_id,
          TableUsageEvent.data_source_id == data_source_id,
          TableUsageEvent.datasource_table_id == table_id,
          TableUsageEvent.report_id == AgentExecution.report_id,
      ).group_by(AgentExecution.id, AgentExecution.completion_id, AgentExecution.report_id).subquery())
    response = aliased(Completion)
    prompt = aliased(Completion)
    query = (select(usage.c.execution_id, usage.c.used_at, usage.c.success, prompt.prompt)
        .join(response, response.id == usage.c.completion_id)
        .join(prompt, prompt.id == response.parent_id)
        .where(prompt.deleted_at.is_(None), response.deleted_at.is_(None), prompt.role == 'user', prompt.report_id == usage.c.report_id, response.report_id == usage.c.report_id)
        .order_by(usage.c.used_at.desc(), usage.c.execution_id.desc())
        .offset(offset).limit(limit + 1))
    rows = (await db.execute(query)).all()
    return {'items': [dict(execution_id=row.execution_id,
        prompt=str(row.prompt.get('content') or '') if isinstance(row.prompt, dict) else str(row.prompt or ''),
        used_at=row.used_at, success=bool(row.success)) for row in rows[:limit]],
        'next_offset': offset + limit if len(rows) > limit else None}
