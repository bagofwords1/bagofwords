from typing import Optional, Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.completion_block import CompletionBlock
from app.models.plan_decision import PlanDecision
from app.models.tool_execution import ToolExecution
from app.models.widget import Widget
from app.models.step import Step
from app.models.visualization import Visualization

from app.schemas.agent_execution_schema import PlanDecisionSchema
from app.schemas.tool_execution_schema import ToolExecutionSchema
from app.schemas.completion_v2_schema import (
    CompletionBlockV2Schema,
    ToolExecutionUISchema,
)
from app.schemas.widget_schema import WidgetSchema
from app.schemas.step_schema import StepSchema
from app.schemas.visualization_schema import VisualizationSchema


def _trim_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of dict without None values; recurse for dict children and lists."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            nv = _trim_none(v)
            if nv:
                out[k] = nv
        elif isinstance(v, list):
            items: List[Any] = []
            for item in v:
                if isinstance(item, dict):
                    tv = _trim_none(item)
                    if tv:
                        items.append(tv)
                elif item is not None:
                    items.append(item)
            if items:
                out[k] = items
        else:
            out[k] = v
    return out


def serialize_block_v2_sync(
    block: CompletionBlock,
    plan_decision: Optional[PlanDecision] = None,
    tool_execution: Optional[ToolExecution] = None,
    created_widget: Optional[Widget] = None,
    widget_last_step: Optional[Step] = None,
    created_step: Optional[Step] = None,
    created_visualizations: Optional[List[Visualization]] = None,
) -> CompletionBlockV2Schema:
    """Serialize a CompletionBlock to the v2 UI schema using pre-loaded data.
    
    This is the optimized version that accepts all related objects pre-loaded
    to avoid N+1 queries.
    """
    # Map to schemas
    pd_schema = PlanDecisionSchema.from_orm(plan_decision) if plan_decision else None
    te_schema: Optional[ToolExecutionUISchema] = None
    
    if tool_execution:
        # Start from the base ToolExecution schema
        base_te = ToolExecutionSchema.from_orm(tool_execution)
        te_data = base_te.model_dump()
        # Strip heavy payloads from result_json (e.g., widget_data)
        result_json = te_data.get("result_json")
        if isinstance(result_json, dict) and "widget_data" in result_json:
            result_json.pop("widget_data", None)
        te_data["result_json"] = result_json

        # Build widget schema with last_step if provided
        created_widget_schema: Optional[WidgetSchema] = None
        if created_widget:
            last_step_schema: Optional[StepSchema] = None
            if widget_last_step:
                step_dict = {
                    **widget_last_step.__dict__,
                    "data_model": getattr(widget_last_step, "data_model", None) or {},
                    "data": getattr(widget_last_step, "data", None) or {},
                }
                last_step_schema = StepSchema.model_validate(step_dict)
            created_widget_schema = WidgetSchema.from_orm(created_widget).copy(update={"last_step": last_step_schema})

        # Build step schema if provided
        created_step_schema: Optional[StepSchema] = None
        if created_step:
            step_dict = {
                **created_step.__dict__,
                "data_model": getattr(created_step, "data_model", None) or {},
                "data": getattr(created_step, "data", None) or {},
            }
            created_step_schema = StepSchema.model_validate(step_dict)

        # Build visualizations list
        created_visualizations_schemas: list[VisualizationSchema] = []
        if created_visualizations:
            for vobj in created_visualizations:
                try:
                    created_visualizations_schemas.append(VisualizationSchema.from_orm(vobj))
                except Exception:
                    continue

        # Build the UI schema, attaching created artifacts
        te_schema = ToolExecutionUISchema(
            **te_data,
            created_widget=created_widget_schema,
            created_step=created_step_schema,
            created_visualizations=created_visualizations_schemas or None,
        )

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


async def serialize_block_v2(db: AsyncSession, block: CompletionBlock) -> CompletionBlockV2Schema:
    """Serialize a CompletionBlock to the v2 UI schema with joined decision/tool info.

    Note: For efficiency, prefer serialize_block_v2_sync with pre-loaded objects.
    This helper performs fetches by ID for backward compatibility.
    """
    # Fetch linked decision and tool execution if present
    plan_decision: Optional[PlanDecision] = None
    tool_execution: Optional[ToolExecution] = None
    created_widget: Optional[Widget] = None
    widget_last_step: Optional[Step] = None
    created_step: Optional[Step] = None
    created_visualizations: List[Visualization] = []

    if getattr(block, "plan_decision_id", None):
        plan_decision = await db.get(PlanDecision, block.plan_decision_id)
    if getattr(block, "tool_execution_id", None):
        tool_execution = await db.get(ToolExecution, block.tool_execution_id)

    if tool_execution:
        if getattr(tool_execution, "created_widget_id", None):
            created_widget = await db.get(Widget, tool_execution.created_widget_id)
            if created_widget:
                # Fetch last step for the widget
                last_step_result = await db.execute(
                    select(Step).filter(Step.widget_id == created_widget.id).order_by(Step.created_at.desc()).limit(1)
                )
                widget_last_step = last_step_result.scalar_one_or_none()

        if getattr(tool_execution, "created_step_id", None):
            created_step = await db.get(Step, tool_execution.created_step_id)

        # Visualizations list from artifact refs (supports multiple)
        try:
            refs = getattr(tool_execution, 'artifact_refs_json', None) or {}
            vis_ids = refs.get('visualizations') or []
            for vid in vis_ids:
                try:
                    vobj = await db.get(Visualization, str(vid))
                    if vobj is not None:
                        created_visualizations.append(vobj)
                except Exception:
                    continue
        except Exception:
            pass

    return serialize_block_v2_sync(
        block=block,
        plan_decision=plan_decision,
        tool_execution=tool_execution,
        created_widget=created_widget,
        widget_last_step=widget_last_step,
        created_step=created_step,
        created_visualizations=created_visualizations or None,
    )
