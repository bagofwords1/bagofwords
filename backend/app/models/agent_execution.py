from sqlalchemy import Column, String, DateTime, Integer, Float, JSON, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseSchema


class AgentExecution(BaseSchema):
    __tablename__ = 'agent_executions'

    # Links
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True)

    # Status and timing
    status = Column(String, nullable=False, default='in_progress')
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    total_duration_ms = Column(Float, nullable=True)
    first_token_ms = Column(Float, nullable=True)
    thinking_ms = Column(Float, nullable=True)

    # Streaming resume
    latest_seq = Column(Integer, nullable=False, default=0)

    # Metrics and config
    token_usage_json = Column(JSON, nullable=True, default=dict)
    error_json = Column(JSON, nullable=True)
    config_json = Column(JSON, nullable=True)
    
    # Version tracking
    bow_version = Column(String, nullable=True, index=True)

    # Relationships (optional lazy loading)
    plan_decisions = relationship('PlanDecision', back_populates='agent_execution', lazy='select')
    tool_executions = relationship('ToolExecution', back_populates='agent_execution', lazy='select')
    context_snapshots = relationship('ContextSnapshot', back_populates='agent_execution', lazy='select')
    instructions = relationship('Instruction', back_populates='agent_execution', lazy='select')


