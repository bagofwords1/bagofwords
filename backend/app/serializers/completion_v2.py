from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.completion_block import CompletionBlock
from app.models.plan_decision import PlanDecision
from app.models.tool_execution import ToolExecution

from app.schemas.agent_execution_schema import PlanDecisionSchema
from app.schemas.tool_execution_schema import ToolExecutionSchema
from app.schemas.completion_v2_schema import (
    CompletionBlockV2Schema,
    ToolExecutionUISchema,
)


async def serialize_block_v2(db: AsyncSession, block: CompletionBlock) -> CompletionBlockV2Schema:
    """Serialize a CompletionBlock to the v2 UI schema with joined decision/tool info.

    Note: For efficiency, callers who already have joined objects can inline them.
    This helper performs minimal fetches by ID.
    """
    # Fetch linked decision and tool execution if present
    plan_decision: Optional[PlanDecision] = None
    tool_execution: Optional[ToolExecution] = None

    if getattr(block, "plan_decision_id", None):
        plan_decision = await db.get(PlanDecision, block.plan_decision_id)
    if getattr(block, "tool_execution_id", None):
        tool_execution = await db.get(ToolExecution, block.tool_execution_id)

    # Map to schemas
    pd_schema = PlanDecisionSchema.from_orm(plan_decision) if plan_decision else None
    te_schema: Optional[ToolExecutionUISchema] = None
    if tool_execution:
        te_schema = ToolExecutionUISchema.model_validate(ToolExecutionSchema.from_orm(tool_execution))

    # seq primarily comes from decision.seq if available
    seq = plan_decision.seq if plan_decision is not None else None

    return CompletionBlockV2Schema(
        id=str(block.id),
        completion_id=str(block.completion_id),
        agent_execution_id=str(block.agent_execution_id) if block.agent_execution_id else None,
        seq=seq,
        block_index=block.block_index,
        loop_index=block.loop_index,
        title=block.title,
        status=block.status,
        icon=block.icon,
        content=block.content,
        reasoning=block.reasoning,
        plan_decision=pd_schema,
        tool_execution=te_schema,
        artifact_changes=None,
        started_at=block.started_at,
        completed_at=block.completed_at,
        created_at=block.created_at,
        updated_at=block.updated_at,
    )


