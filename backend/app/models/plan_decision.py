from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseSchema


class PlanDecision(BaseSchema):
    __tablename__ = 'plan_decisions'
    __table_args__ = (
        UniqueConstraint('agent_execution_id', 'seq', name='uq_plan_decisions_execution_seq'),
    )

    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=False, index=True)
    agent_execution = relationship('AgentExecution', back_populates='plan_decisions')

    seq = Column(Integer, nullable=False, default=0)
    loop_index = Column(Integer, nullable=False, default=0)

    plan_type = Column(String, nullable=True)  # research | action
    analysis_complete = Column(Boolean, nullable=False, default=False)

    reasoning = Column(String, nullable=True)  # user-facing reasoning text
    assistant = Column(String, nullable=True)  # user-facing assistant message
    final_answer = Column(String, nullable=True)

    action_name = Column(String, nullable=True)
    action_args_json = Column(JSON, nullable=True)

    metrics_json = Column(JSON, nullable=True)
    context_snapshot_id = Column(String(36), ForeignKey('context_snapshots.id'), nullable=True)


