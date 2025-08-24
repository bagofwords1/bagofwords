from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey, Float, DateTime
from sqlalchemy.orm import relationship
from .base import BaseSchema


class ToolExecution(BaseSchema):
    __tablename__ = 'tool_executions'

    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=False, index=True)
    agent_execution = relationship('AgentExecution', back_populates='tool_executions')

    plan_decision_id = Column(String(36), ForeignKey('plan_decisions.id'), nullable=True)

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

    result_summary = Column(String, nullable=True)
    result_json = Column(JSON, nullable=True)
    artifact_refs_json = Column(JSON, nullable=True)

    created_widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=True)
    created_step_id = Column(String(36), ForeignKey('steps.id'), nullable=True)

    # Post-tool context snapshot for replay/audit
    context_snapshot_id = Column(String(36), ForeignKey('context_snapshots.id'), nullable=True)

    error_message = Column(String, nullable=True)


