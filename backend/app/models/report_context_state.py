from sqlalchemy import Column, String, JSON, ForeignKey, Integer, DateTime, Boolean, false
from sqlalchemy.orm import relationship
from app.ee.encryption import EncryptedJSON
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

    # Rolling LLM summary of the conversation; it quotes results, so encrypted.
    summary_json = Column(EncryptedJSON, nullable=False, default=dict)
    # Watermark: completions with created_at <= this completion are covered
    # by summary_json and excluded from the detailed message window.
    covers_until_completion_id = Column(String(36), ForeignKey('completions.id'), nullable=True)

    covered_turns = Column(Integer, nullable=False, default=0)
    # Cumulative token estimate of digests folded into the summary; drives
    # the "Compacted · Nk" display in the UI.
    tokens_compacted_total = Column(Integer, nullable=False, default=0)
    last_compaction_at = Column(DateTime, nullable=True, default=None)

    # Token anchor: the provider-reported size of the last planner request and
    # our cheap estimate of that same request. Their gap calibrates the next
    # run's measurement (see app.ai.context.context_budget).
    last_prompt_tokens = Column(Integer, nullable=True, default=None)
    last_prompt_estimate = Column(Integer, nullable=True, default=None)
    # Summarizer failure backoff (auto modes only; manual /compact ignores it).
    failure_count = Column(Integer, nullable=False, default=0, server_default="0")
    cooldown_until = Column(DateTime, nullable=True, default=None)
    # Last compaction outcome for the divider / usage popover.
    last_method = Column(String(16), nullable=True, default=None)
    degraded = Column(Boolean, nullable=False, default=False, server_default=false())
    last_tokens_before = Column(Integer, nullable=True, default=None)
    last_tokens_after = Column(Integer, nullable=True, default=None)
