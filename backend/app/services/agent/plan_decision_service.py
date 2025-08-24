from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.plan_decision import PlanDecision


class PlanDecisionService:
    async def upsert_frame(
        self,
        db: AsyncSession,
        *,
        agent_execution_id: str,
        seq: int,
        loop_index: int,
        plan_type: Optional[str],
        analysis_complete: bool,
        reasoning: Optional[str],
        assistant: Optional[str],
        final_answer: Optional[str],
        action_name: Optional[str],
        action_args_json: Optional[Dict[str, Any]],
        metrics_json: Optional[Dict[str, Any]],
        context_snapshot_id: Optional[str] = None,
    ) -> PlanDecision:
        frame = PlanDecision(
            agent_execution_id=agent_execution_id,
            seq=seq,
            loop_index=loop_index,
            plan_type=plan_type,
            analysis_complete=analysis_complete,
            reasoning=reasoning,
            assistant=assistant,
            final_answer=final_answer,
            action_name=action_name,
            action_args_json=action_args_json,
            metrics_json=metrics_json,
            context_snapshot_id=context_snapshot_id,
        )
        db.add(frame)
        await db.commit()
        await db.refresh(frame)
        return frame


