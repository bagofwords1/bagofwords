"""Reasoning-effort vocabulary shared by API schemas and the LLM layer.

Dependency-free so request schemas can validate an effort without importing
the LLM package.
"""
from typing import Optional

# Every effort the backend understands, weakest to strongest. Providers accept
# different subsets; app.ai.llm.reasoning.clamp_effort maps a request onto the
# subset a model accepts, so a user's pick never becomes a 400.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# The levels the product offers users. "max" means "the strongest this model
# has" (max, else xhigh, else its top level).
USER_EFFORTS = ("low", "medium", "high", "max")

# Values meaning "no explicit choice": resolve from the report / trigger words /
# model default instead.
_DEFAULT_ALIASES = {"", "default", "auto"}


def normalize_effort(value) -> Optional[str]:
    """Validate an effort from an API payload or stored JSON.

    Returns None for "no explicit choice", "off" for off/none, otherwise one of
    EFFORT_ORDER. Raises ValueError for anything else, so a typo is a 422
    instead of a silently ignored setting.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in _DEFAULT_ALIASES:
        return None
    if v in ("off", "none"):
        return "off"
    if v in EFFORT_ORDER:
        return v
    raise ValueError(
        "reasoning_effort must be one of: default, off, " + ", ".join(EFFORT_ORDER[1:])
    )


def normalize_prompt_json_effort(prompt: Optional[dict]) -> Optional[dict]:
    """Validate ``reasoning_effort`` inside a stored prompt JSON (scheduled
    prompts, eval cases) at write time, so a bad value is a 422 instead of a
    run that fails when the prompt is replayed."""
    if not isinstance(prompt, dict) or "reasoning_effort" not in prompt:
        return prompt
    out = dict(prompt)
    effort = normalize_effort(out.get("reasoning_effort"))
    if effort is None:
        out.pop("reasoning_effort", None)
    else:
        out["reasoning_effort"] = effort
    return out
