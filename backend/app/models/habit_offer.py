from sqlalchemy import Column, DateTime, ForeignKey, Index, String

from app.models.base import BaseSchema

OFFER_OFFERED = "offered"
OFFER_ACCEPTED = "accepted"
OFFER_DECLINED = "declined"

CADENCE_DAILY = "daily"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class HabitOffer(BaseSchema):
    """A one-click offer to prepare something the user asks for repeatedly
    (e.g. "weekly pipeline by region, Mondays"). Accepting creates a normal
    scheduled prompt; nothing is scheduled without that click."""

    __tablename__ = "habit_offers"
    __table_args__ = (
        Index("ix_habit_offers_org_user_status", "organization_id", "user_id", "status"),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False, index=True)
    intent_text = Column(String(200), nullable=False)
    # "daily" or "weekly:<mon..sun>"
    cadence = Column(String(16), nullable=False)
    # "HH:MM" in the org timezone.
    suggested_time = Column(String(5), nullable=False)
    status = Column(String(12), nullable=False, default=OFFER_OFFERED)
    scheduled_prompt_id = Column(String(36), ForeignKey("scheduled_prompts.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    dream_run_id = Column(String(36), ForeignKey("dream_runs.id"), nullable=True)
