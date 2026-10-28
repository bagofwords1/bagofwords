from sqlalchemy import Column, String, JSON, ForeignKey
from sqlalchemy.orm import relationship
from app.ee.encryption import EncryptedJSON, EncryptedString
from .base import BaseSchema


class ContextSnapshot(BaseSchema):
    __tablename__ = 'context_snapshots'

    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=False, index=True)
    agent_execution = relationship('AgentExecution', back_populates='context_snapshots')

    kind = Column(String, nullable=False, default='initial')  # initial | pre_tool | post_tool | final

    # The warm sections of the agent's ContextView: data previews of widgets and
    # queries, entity sample rows, tool observations. That is the same data
    # Step.data / Entity.data protect, so it is encrypted under the same key.
    context_view_json = Column(EncryptedJSON, nullable=False, default=dict)
    prompt_text = Column(EncryptedString, nullable=True)
    prompt_tokens = Column(String, nullable=True)
    hash = Column(String, nullable=True)


