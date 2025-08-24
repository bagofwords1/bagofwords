from sqlalchemy import Column, String, JSON, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseSchema


class ContextSnapshot(BaseSchema):
    __tablename__ = 'context_snapshots'

    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=False, index=True)
    agent_execution = relationship('AgentExecution', back_populates='context_snapshots')

    kind = Column(String, nullable=False, default='initial')  # initial | pre_tool | post_tool | final

    context_view_json = Column(JSON, nullable=False, default=dict)
    prompt_text = Column(String, nullable=True)
    prompt_tokens = Column(String, nullable=True)
    hash = Column(String, nullable=True)


