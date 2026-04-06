from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, DateTime, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ScheduledPrompt(BaseSchema):
    __tablename__ = 'scheduled_prompts'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    prompt = Column(JSON, nullable=False)  # PromptSchema-compatible JSON: {"content": "...", ...}
    cron_schedule = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True, default=None)
    notification_subscribers = Column(JSON, nullable=True, default=None)  # [{type, id/address}]

    report = relationship("Report", back_populates="scheduled_prompts", lazy='selectin')
    user = relationship("User", lazy='select')
    completions = relationship("Completion", back_populates="scheduled_prompt", lazy='select')
