"""Shared small-model call + JSON extraction for the planner and the judge."""
from __future__ import annotations

import asyncio
import functools
import json
import re
from typing import Any, Optional

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def extract_json(raw: Any) -> Optional[dict]:
    """Best-effort: the first JSON object in ``raw`` (fences stripped). None on failure."""
    if raw is None:
        return None
    text = str(raw).strip()
    text = _FENCE.sub("", text).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def call_small_model(
    model, *, system: str, prompt: str, usage_scope: str, usage_scope_ref_id: Optional[str],
) -> str:
    """One structured call on ``model``; usage is recorded under ``usage_scope``
    with ``usage_scope_ref_id`` (the check-in id) so the trace can price it."""
    from app.ai.llm import LLM
    from app.dependencies import async_session_maker

    from app.ai.llm.llm import bind_usage_loop

    # The check-in judge is often the first LLM call in a scheduler fire; bind
    # the loop so its usage (priced on the TraceModal card) is not dropped.
    bind_usage_loop()
    llm = LLM(model, reasoning_effort="off", usage_session_maker=async_session_maker)
    # LLM.inference is sync (see Reporter.generate_report_title) — off-load it.
    return await asyncio.to_thread(
        functools.partial(
            llm.inference, prompt, system=system,
            usage_scope=usage_scope, usage_scope_ref_id=usage_scope_ref_id,
        )
    )
