"""Check-in judge: run or skip a due follow-up, always with a reason.

An answer without a reason — or anything unparseable — counts as ``skip`` with
reason ``invalid_judge_output``.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from app.ai.agents.checkins._llm import call_small_model, extract_json
from app.ai.agents.checkins.prompts import JUDGE_SYSTEM, render_judge_prompt
from app.models.agent_checkin import REASON_INVALID_JUDGE_OUTPUT

logger = logging.getLogger(__name__)

USAGE_SCOPE = "checkin_judge"


class JudgeOutput(BaseModel):
    decision: Literal["run", "skip"]
    reason: str
    focus: Optional[str] = None
    # Set only when the model's answer was rejected (see parse_judge_output).
    invalid: bool = False

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("reason required")
        return v.strip()


INVALID = JudgeOutput(
    decision="skip",
    reason="The judge's answer was not valid (missing decision or reason); skipped by default.",
    invalid=True,
)


def parse_judge_output(raw) -> JudgeOutput:
    data = extract_json(raw)
    if data is None:
        return INVALID
    if isinstance(data.get("decision"), str):
        data["decision"] = data["decision"].strip().lower()
    data.pop("invalid", None)
    try:
        out = JudgeOutput(**data)
    except Exception:
        return INVALID
    out.reason = out.reason[:2000]
    out.focus = ((out.focus or "").strip()[:2000]) or None
    return out


async def run_judge(model, ctx: dict, *, checkin_id: str) -> JudgeOutput:
    try:
        raw = await call_small_model(
            model, system=JUDGE_SYSTEM, prompt=render_judge_prompt(ctx),
            usage_scope=USAGE_SCOPE, usage_scope_ref_id=checkin_id,
        )
    except Exception as e:
        logger.warning("checkin judge call failed: %s", e)
        return INVALID
    return parse_judge_output(raw)


__all__ = ["JudgeOutput", "parse_judge_output", "run_judge", "INVALID", "REASON_INVALID_JUDGE_OUTPUT"]
