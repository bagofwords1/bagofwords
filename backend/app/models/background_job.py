"""Durable delivery intent. A schedule is only a timer; this is its receipt."""

from sqlalchemy import Column, String, JSON, DateTime, Integer, Text, Index
from app.models.base import BaseSchema


class BackgroundJob(BaseSchema):
    __tablename__ = "background_jobs"
    # Deterministic for one-shot wakes/provider delivery IDs; random for manual runs.
    key = Column(String(512), nullable=False, unique=True)
    kind = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    available_at = Column(DateTime, nullable=False)
    lease_until = Column(DateTime, nullable=True)
    owner = Column(String(36), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    # Written in the SAME transaction that creates the agent system completion.
    # Once present, recovery must never replay arbitrary agent side effects.
    event_id = Column(String(36), nullable=True)
    completion_id = Column(String(36), nullable=True)
    error = Column(Text, nullable=True)
    __table_args__ = (Index("ix_background_jobs_due", "status", "available_at"),)
