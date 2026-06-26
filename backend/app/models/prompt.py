from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Boolean, JSON, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.models.prompt_data_source_association import prompt_data_source_association


class Prompt(BaseSchema):
    """Org prompt catalog item AND execution spec.

    A Prompt is a reusable, optionally-published instruction tied to one or more
    agents (data sources). Users can run it once ("try it now") or subscribe to
    it (recurring), and privileged users can assign it to others. The execution
    spec (``text``/``mode``/``model_id``/``mentions``) is PromptSchema-compatible
    so the scheduled-run path can execute it unchanged.
    """
    __tablename__ = 'prompts'

    title = Column(String, nullable=True)
    # The instruction text (PromptSchema.content). Kept as `text` for backward compat.
    text = Column(String, nullable=False)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)  # author / created_by
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)

    # ── Execution spec (materialized into PromptSchema at run time) ──
    mode = Column(String, nullable=False, default='chat')   # 'chat' | 'deep' | 'training'
    model_id = Column(String(36), nullable=True)            # LLM override; null = org default
    mentions = Column(JSON, nullable=True)                   # pinned context references (PromptSchema.mentions)
    parameters = Column(JSON, nullable=True)                 # [{name,label,type,required,default,options}] template params

    # ── Catalog / discovery ──
    scope = Column(String, nullable=False, default='agent')    # 'agent' | 'global' | 'private'
    is_starter = Column(Boolean, nullable=False, default=False) # surface as a home conversation starter
    status = Column(String, nullable=False, default='draft')    # 'draft' | 'published'
    default_cron = Column(String, nullable=True)
    default_channel = Column(String, nullable=True)             # 'teams'|'slack'|'ai_mailbox'|'smtp'
    category = Column(String, nullable=True)
    tags = Column(JSON, nullable=True)

    # Keep the relationship, but reference Organization by string to avoid circular imports
    organization = relationship("Organization", back_populates="prompts")
    data_sources = relationship(
        "DataSource",
        secondary="prompt_data_source_association",
        back_populates="prompts",
        lazy="selectin",
    )
    scheduled_prompts = relationship("ScheduledPrompt", back_populates="prompt_obj", lazy="select")
