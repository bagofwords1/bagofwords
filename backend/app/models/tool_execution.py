import logging

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    event,
    inspect,
)
from sqlalchemy.orm import relationship

from .base import BaseSchema


logger = logging.getLogger(__name__)


class ToolExecution(BaseSchema):
    __tablename__ = 'tool_executions'

    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=False, index=True)
    agent_execution = relationship('AgentExecution', back_populates='tool_executions')

    plan_decision_id = Column(String(36), ForeignKey('plan_decisions.id'), nullable=True)

    # Native provider call identity. These fields make a tool turn replayable
    # after a worker/process boundary without copying the whole transcript into
    # a second JSON blob. Historical rows leave them NULL and are assigned a
    # stable synthetic call id by the context loader.
    provider_call_id = Column(String, nullable=True)
    provider_name = Column(String, nullable=True)
    provider_signature = Column(String, nullable=True)
    action_index = Column(Integer, nullable=True)

    tool_name = Column(String, nullable=False)
    tool_action = Column(String, nullable=True)
    arguments_json = Column(JSON, nullable=False, default=dict)

    status = Column(String, nullable=False, default='in_progress')
    success = Column(Boolean, nullable=False, default=False)

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Float, nullable=True)

    attempt_number = Column(Integer, nullable=False, default=1)
    max_retries = Column(Integer, nullable=False, default=0)

    token_usage_json = Column(JSON, nullable=True)
    sub_timings_json = Column(JSON, nullable=True)

    result_summary = Column(String, nullable=True)
    result_json = Column(JSON, nullable=True)
    # Immutable, bounded projection consumed by conversation history. The full
    # result_json remains untouched for the UI/audit trail. Row-heavy tools use
    # specialized output shapes; ordinary tools prefer the exact model-visible
    # observation and fall back to a generic result projection when needed.
    context_summary_json = Column(JSON(none_as_null=True), nullable=True)
    artifact_refs_json = Column(JSON, nullable=True)

    created_widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=True)
    created_step_id = Column(String(36), ForeignKey('steps.id'), nullable=True)

    # Post-tool context snapshot for replay/audit
    context_snapshot_id = Column(String(36), ForeignKey('context_snapshots.id'), nullable=True)

    error_message = Column(String, nullable=True)


def before_write_tool_context_summary(mapper, connection, target):
    """Fill a missing projection when only the canonical result is available.

    Agent execution paths snapshot the richer model-visible observation before
    the flush. Never replace that snapshot with a projection rebuilt from the
    tool's different output shape.
    """
    try:
        if target.context_summary_json is not None:
            return
        state = inspect(target)
        if state.attrs.result_json.history.has_changes():
            from app.ai.persisted_summary import build_tool_context_summary

            target.context_summary_json = build_tool_context_summary(
                target.tool_name,
                target.result_json,
            )
    except Exception as exc:
        # Tool persistence is canonical; a derived optimization is fail-open.
        logger.warning(
            "Failed to build context summary for tool execution %s: %s",
            target.id,
            exc,
        )


event.listen(ToolExecution, 'before_insert', before_write_tool_context_summary)
event.listen(ToolExecution, 'before_update', before_write_tool_context_summary)
