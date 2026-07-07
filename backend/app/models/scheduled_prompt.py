from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, DateTime, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ScheduledPrompt(BaseSchema):
    __tablename__ = 'scheduled_prompts'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    prompt = Column(JSON, nullable=False)  # PromptSchema-compatible JSON: {"content": "...", ...}
    cron_schedule = Column(String, nullable=False)
    # Routing: False (default) = run in the host report, keeping cross-run
    # memory (trend commentary via past_observations). True = spawn a fresh
    # report per run — clean dated snapshots, no context growth. Mirrors the
    # trigger webhooks' spawn mode (docs/design/agent-triggers.md §6.3).
    spawn_new_report = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True, default=None)
    notification_subscribers = Column(JSON, nullable=True, default=None)  # [{type, id/address}]

    report = relationship("Report", back_populates="scheduled_prompts", lazy='selectin')
    user = relationship("User", lazy='select')
    completions = relationship("Completion", back_populates="scheduled_prompt", lazy='select')
