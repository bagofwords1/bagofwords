import logging
from typing import Optional, Dict, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tool_execution import ToolExecution
from app.utils.json_sanitize import sanitize_json_strings, sanitize_utf8

logger = logging.getLogger(__name__)


class ToolExecutionService:
    async def start(
        self,
        db: AsyncSession,
        *,
        agent_execution_id: str,
        plan_decision_id: Optional[str],
        tool_name: str,
        tool_action: Optional[str],
        arguments_json: Dict[str, Any],
        attempt_number: int = 1,
        max_retries: int = 0,
    ) -> ToolExecution:
        te = ToolExecution(
            agent_execution_id=agent_execution_id,
            plan_decision_id=plan_decision_id,
            tool_name=tool_name,
            tool_action=tool_action,
            # Persistence boundary: strings Python tolerates but JSON transport
            # doesn't (lone surrogates, NUL) must never reach the DB — a single
            # poisoned row 500s every later load of the report.
            arguments_json=sanitize_json_strings(arguments_json),
            status='in_progress',
            success=False,
            started_at=datetime.utcnow(),
            attempt_number=attempt_number,
            max_retries=max_retries,
        )
        db.add(te)
        await db.commit()
        await db.refresh(te)
        return te

    async def finish(
        self,
        db: AsyncSession,
        te: ToolExecution,
        *,
        status: str,
        success: bool,
        result_summary: Optional[str] = None,
        result_json: Optional[Dict[str, Any]] = None,
        artifact_refs_json: Optional[Dict[str, Any]] = None,
        created_widget_id: Optional[str] = None,
        created_step_id: Optional[str] = None,
        error_message: Optional[str] = None,
        token_usage_json: Optional[Dict[str, Any]] = None,
    ) -> ToolExecution:
        te.status = status
        te.success = success
        te.completed_at = datetime.utcnow()
        if te.started_at and te.completed_at:
            te.duration_ms = (te.completed_at - te.started_at).total_seconds() * 1000.0
        # Same persistence-boundary guarantee as `start`: tool payloads (file
        # text, extracted documents, MCP results) must be UTF-8-serializable
        # before they're stored, or the report becomes permanently unloadable.
        te.result_summary = sanitize_utf8(result_summary) if result_summary else result_summary
        te.result_json = sanitize_json_strings(result_json)
        te.artifact_refs_json = sanitize_json_strings(artifact_refs_json)
        te.created_widget_id = created_widget_id
        te.created_step_id = created_step_id
        te.error_message = sanitize_utf8(error_message) if error_message else error_message
        te.token_usage_json = token_usage_json
        db.add(te)
        await db.commit()
        await db.refresh(te)
        # Surface a Review item when a data query ran over the latency budget.
        # Cheap-guarded inside the emitter; never fatal to tool execution.
        try:
            from app.services.review_producers import emit_slow_query_for_tool_execution
            await emit_slow_query_for_tool_execution(db, te)
        except Exception as e:  # noqa: BLE001
            logger.warning("review: emit_slow_query failed for tool_execution %s: %s", te.id, e)
        return te


