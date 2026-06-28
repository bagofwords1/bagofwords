from sqlalchemy import Column, String, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class PromptRun(BaseSchema):
    """A single execution of a saved Prompt — usage tracking + run-for provenance.

    Self-run:  actor_id == user_id == the caller.
    Run-for:   actor_id == the admin who triggered; user_id == the target user
               who owns (and privately reads) the produced report.

    `report_id` points at the report created for the run (nullable to keep the
    audit row even if report creation was deferred/failed).
    """
    __tablename__ = 'prompt_runs'

    prompt_id = Column(String(36), ForeignKey('prompts.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)   # report owner / whose run
    actor_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)  # who triggered
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    parameters = Column(JSON, nullable=True)  # the values filled at run time

    prompt = relationship("Prompt", lazy='select')
    report = relationship("Report", lazy='select')
    user = relationship("User", foreign_keys=[user_id], lazy='select')
    actor = relationship("User", foreign_keys=[actor_id], lazy='select')
