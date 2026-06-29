from sqlalchemy import Column, String, ForeignKey, Boolean, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.models.prompt_data_source_association import prompt_data_source_association


class Prompt(BaseSchema):
    """A reusable prompt: a saved, completion-shaped instruction.

    Scope:
      - 'agent'  → associated with one or more agents (data sources); visible to
                   users who can access ALL of its ACTIVE agents. These are the
                   per-agent prompts (a.k.a. conversation starters).
      - 'global' → org-wide; visible to every member. Admin-managed.
      - 'private'→ visible only to its creator.

    The body is PromptSchema-compatible (content/mentions/model/mode), so running
    a prompt is just building a completion request. `parameters` lets a prompt be
    a template whose values the user fills at run time.
    """
    __tablename__ = 'prompts'

    title = Column(String, nullable=True)
    text = Column(String, nullable=False)              # the instruction (PromptSchema.content)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)          # author
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)

    # ── completion-shaped execution spec ──
    mode = Column(String, nullable=False, default='chat')   # 'chat' | 'deep' | 'training'
    model_id = Column(String(36), nullable=True)            # LLM override; null = org default
    mentions = Column(JSON, nullable=True)                   # PromptSchema.mentions
    parameters = Column(JSON, nullable=True)                 # [{name,label,type,required,default,options}]

    # ── scope / classification ──
    scope = Column(String, nullable=False, default='agent')  # 'agent' | 'global' | 'private'
    is_starter = Column(Boolean, nullable=False, default=False)  # surface as a conversation starter

    organization = relationship("Organization", back_populates="prompts")
    data_sources = relationship(
        "DataSource",
        secondary="prompt_data_source_association",
        back_populates="prompts",
        lazy="selectin",
    )
