"""Generality gate for AI-authored instructions.

The knowledge harness (agent v2, mode="knowledge") and training mode capture
instructions via the create_instruction / edit_instruction tools. Instructions
are standing rules loaded into future sessions for all users, so they must be
reusable rules — not record-level facts ("customer X's last name is Y",
"exclude invoice 384", "there are 59 customers") that live in the data and go
stale.

The prompts already instruct the model to generalize; this module adds an
independent server-side check so an overfit instruction is rejected at the
tool boundary with ``rejected_reason="overfit"`` and a reason the planner can
act on (generalize or skip). The critic is a single small-model call and
FAILS OPEN: if no LLM is available or the call errors, the instruction is
allowed — capture must never break because the gate is unavailable.
"""

import asyncio
import json
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


GENERALITY_CRITIC_PROMPT = """You review a proposed standing instruction for an AI data analyst. \
Standing instructions are loaded into future analysis sessions for ALL users, so they must be \
reusable rules, not facts about individual records.

ACCEPT (verdict "general") when the instruction is a reusable rule, e.g.:
- business term or metric definitions ("'churn' means no purchase in 90 days", "revenue is net of VAT")
- filter/join/aggregation conventions ("exclude cancelled orders from revenue", "attribute sales to reps via the support rep foreign key")
- enum/code meanings ("status 1=active, 2=inactive"), column semantics and unit conversions ("amounts are stored in cents")
- formatting, visualization, or agent-behavior rules
A rule may reference numbers or thresholds when they are part of a definition the user gave \
("high-value customer means total spend over $500").

REJECT (verdict "overfit") when the instruction's substance is a fact about a specific record, \
person, or observed value, e.g.:
- an attribute of one person/customer/entity ("customer John Doe lives in Berlin", "Maria's last name is Novak")
- a hardcoded reference to one specific row or id ("exclude order 9174", "invoice 55 is a duplicate")
- an observed data value or count stated as fact ("there are 41 suppliers", "Q2 revenue was $87,300")
Such facts belong in the data, go stale as data changes, and do not guide any future query beyond \
that one record. If the instruction mixes a general rule with a record-level fact, judge the text \
as written: if a record-level fact is part of the instruction text, reject it so it can be \
resubmitted with only the general rule.

Proposed instruction:
---
{text}
---

Respond with ONLY a JSON object: {{"verdict": "general" | "overfit", "reason": "<one short sentence>"}}"""


def resolve_gate_llm(runtime_ctx: dict):
    """Build an LLM for the gate from runtime context, or None.

    Prefers the org's small model when the agent provided one; falls back
    to the main model. Returns None when neither is available (tool being
    driven outside an agent run) — the gate then allows by default.
    """
    model = runtime_ctx.get("small_model") or runtime_ctx.get("model")
    if model is None:
        return None
    try:
        from app.ai.llm import LLM

        return LLM(model)
    except Exception as e:
        logger.warning(f"Instruction generality gate: could not build LLM ({e}); gate disabled")
        return None


async def check_instruction_generality(text: str, llm) -> Tuple[bool, Optional[str]]:
    """Ask an independent critic whether ``text`` is a reusable rule.

    Returns ``(ok, reason)``: ``ok=False`` means the critic judged the
    instruction overfit (record-level fact). Fails open — any error, missing
    LLM, or unparseable critic output yields ``(True, None)``.
    """
    if llm is None:
        return True, None
    try:
        prompt = GENERALITY_CRITIC_PROMPT.format(text=(text or "").strip()[:4000])
        raw = await asyncio.to_thread(llm.inference, prompt, should_record=False)
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return True, None
        data = json.loads(match.group(0))
        verdict = str(data.get("verdict", "")).strip().lower()
        reason = str(data.get("reason") or "").strip() or None
        if verdict == "overfit":
            return False, reason
        return True, reason
    except Exception as e:
        logger.warning(f"Instruction generality gate failed open: {e}")
        return True, None
