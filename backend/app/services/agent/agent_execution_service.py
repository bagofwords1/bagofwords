from typing import Optional, Dict, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.agent_execution import AgentExecution


class AgentExecutionService:
    async def start_run(
        self,
        db: AsyncSession,
        *,
        completion_id: str,
        organization_id: Optional[str] = None,
        user_id: Optional[str] = None,
        report_id: Optional[str] = None,
        config_json: Optional[Dict[str, Any]] = None,
    ) -> AgentExecution:
        run = AgentExecution(
            completion_id=completion_id,
            organization_id=organization_id,
            user_id=user_id,
            report_id=report_id,
            status='in_progress',
            started_at=datetime.utcnow(),
            config_json=config_json or {},
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    async def next_seq(self, db: AsyncSession, run: AgentExecution) -> int:
        run.latest_seq = (run.latest_seq or 0) + 1
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.latest_seq

    async def finish_run(
        self,
        db: AsyncSession,
        run: AgentExecution,
        *,
        status: str,
        first_token_ms: Optional[float] = None,
        thinking_ms: Optional[float] = None,
        token_usage_json: Optional[Dict[str, Any]] = None,
        error_json: Optional[Dict[str, Any]] = None,
    ) -> AgentExecution:
        run.status = status
        run.completed_at = datetime.utcnow()
        if run.started_at and run.completed_at:
            run.total_duration_ms = (run.completed_at - run.started_at).total_seconds() * 1000.0
        run.first_token_ms = first_token_ms
        run.thinking_ms = thinking_ms
        run.token_usage_json = token_usage_json or run.token_usage_json
        run.error_json = error_json
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run


