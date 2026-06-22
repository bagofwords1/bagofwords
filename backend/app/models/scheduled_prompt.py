from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, DateTime, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ScheduledPrompt(BaseSchema):
    __tablename__ = 'scheduled_prompts'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)  # runs AS this user
    prompt = Column(JSON, nullable=False)  # PromptSchema-compatible JSON: {"content": "...", ...}
    cron_schedule = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True, default=None)
    notification_subscribers = Column(JSON, nullable=True, default=None)  # [{type, id/address}]

    # ── Catalog subscription linkage + delivery ──
    prompt_id = Column(String(36), ForeignKey('prompts.id'), nullable=True, index=True)  # catalog link
    channel = Column(String, nullable=True)        # 'teams'|'slack'|'ai_mailbox'|'smtp'; null = no channel push
    run_mode = Column(String, nullable=False, default='append')  # 'append' | 'new_report'
    created_by = Column(String(36), ForeignKey('users.id'), nullable=True)  # self-subscribe vs admin-assign

    report = relationship("Report", back_populates="scheduled_prompts", lazy='selectin', foreign_keys=[report_id])
    user = relationship("User", lazy='select', foreign_keys=[user_id])
    prompt_obj = relationship("Prompt", back_populates="scheduled_prompts", lazy='selectin')
    completions = relationship("Completion", back_populates="scheduled_prompt", lazy='select')
