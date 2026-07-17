from sqlalchemy import Column, String, JSON, ForeignKey, Integer, DateTime
from sqlalchemy.orm import relationship
from .base import BaseSchema


class ReportContextState(BaseSchema):
    """Rolling compaction state for a report's conversation history.

    One row per report. `summary_json` is the structured rolling summary
    (goal/progress/decisions/entities/next_steps/critical_context) that
    replaces completions at or before the `covers_until_completion_id`
    watermark in the planner's message context. Original completions are
    never deleted — compaction only changes what the context builders render.
    """
    __tablename__ = 'report_context_states'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, unique=True, index=True)
    report = relationship('Report', foreign_keys=[report_id])

    summary_json = Column(JSON, nullable=False, default=dict)
    # Watermark: completions with created_at <= this completion are covered
    # by summary_json and excluded from the detailed message window.
    covers_until_completion_id = Column(String(36), ForeignKey('completions.id'), nullable=True)

    covered_turns = Column(Integer, nullable=False, default=0)
    # Cumulative token estimate of digests folded into the summary; drives
    # the "Compacted · Nk" display in the UI.
    tokens_compacted_total = Column(Integer, nullable=False, default=0)
    last_compaction_at = Column(DateTime, nullable=True, default=None)
