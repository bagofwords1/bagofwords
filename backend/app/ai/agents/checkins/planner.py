"""Check-in planner: one structured small-model call after a chat turn.

Output is validated with pydantic; anything invalid means no check-in (the
caller records nothing). ``propose=false`` is a valid answer and is recorded as
a ``not_proposed`` row so the trace shows the planner's "no" too.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.ai.agents.checkins._llm import call_small_model, extract_json
from app.ai.agents.checkins.prompts import PLANNER_SYSTEM, render_planner_prompt

logger = logging.getLogger(__name__)

USAGE_SCOPE = "checkin_planner"


class PlannerOutput(BaseModel):
    propose: bool
    due_in_hours: Optional[float] = Field(default=None)
    note: Optional[str] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _proposal_complete(self):
        if self.propose:
            if not (self.note or "").strip():
                raise ValueError("a proposal needs a note")
            if self.due_in_hours is None or self.due_in_hours <= 0:
                raise ValueError("a proposal needs a positive due_in_hours")
        return self


def parse_planner_output(raw) -> Optional[PlannerOutput]:
    data = extract_json(raw)
    if data is None:
        return None
    try:
        out = PlannerOutput(**data)
    except Exception:
        return None
    out.note = (out.note or "").strip()[:4000] or None
    out.reason = (out.reason or "").strip()[:1000] or None
    return out


async def run_planner(model, ctx: dict, *, checkin_id: str) -> Optional[PlannerOutput]:
    try:
        raw = await call_small_model(
            model, system=PLANNER_SYSTEM, prompt=render_planner_prompt(ctx),
            usage_scope=USAGE_SCOPE, usage_scope_ref_id=checkin_id,
        )
    except Exception as e:
        logger.warning("checkin planner call failed: %s", e)
        return None
    out = parse_planner_output(raw)
    if out is None:
        logger.info("checkin planner returned invalid output; no check-in")
    return out
