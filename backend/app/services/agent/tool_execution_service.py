from typing import Optional, Dict, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tool_execution import ToolExecution


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
            arguments_json=arguments_json,
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
        te.result_summary = result_summary
        te.result_json = result_json
        te.artifact_refs_json = artifact_refs_json
        te.created_widget_id = created_widget_id
        te.created_step_id = created_step_id
        te.error_message = error_message
        te.token_usage_json = token_usage_json
        db.add(te)
        await db.commit()
        await db.refresh(te)
        return te


