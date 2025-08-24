from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseSchema


class CompletionBlock(BaseSchema):
    __tablename__ = 'completion_blocks'
    __table_args__ = (
        # Prevent duplicate projection rows per source within an execution
        UniqueConstraint('agent_execution_id', 'source_type', 'plan_decision_id', 'tool_execution_id', name='uq_blocks_source'),
        UniqueConstraint('completion_id', 'block_index', name='uq_blocks_completion_block_index'),
    )

    # Ownership
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False, index=True)
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True, index=True)

    # Source linkage (exactly one of these should be set)
    source_type = Column(String, nullable=False)  # 'decision' | 'tool' | 'final'
    plan_decision_id = Column(String(36), ForeignKey('plan_decisions.id'), nullable=True)
    tool_execution_id = Column(String(36), ForeignKey('tool_executions.id'), nullable=True)

    # Ordering and grouping
    block_index = Column(Integer, nullable=False, default=0)  # order within completion
    loop_index = Column(Integer, nullable=True)

    # Render fields (denormalized for fast UI)
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default='in_progress')  # in_progress | completed | error
    icon = Column(String, nullable=True)
    content = Column(String, nullable=True)  # from plan_decision.assistant
    reasoning = Column(String, nullable=True)  # from plan_decision.reasoning

    # Timing
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


