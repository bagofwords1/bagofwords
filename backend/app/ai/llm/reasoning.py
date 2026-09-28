"""Shared effort policy; adapters translate this configuration to their API."""
from typing import Optional

# Substring triggers that bump a completion's reasoning_effort to "high".
# Matched case-insensitive against the user-submitted prompt text only —
# not system prompts, instructions, or rendered context. See
# _detect_thinking_trigger / _resolve_reasoning_effort below.
THINKING_TRIGGERS = (
    "think hard",
    "think harder",
    "ultrathink",
    "think step by step",
    "think carefully",
    "think deeply",
    "deep dive",
    "be thorough",
)

# Effort sent when reasoning is "off" to a model that cannot stop thinking
# (Sonnet 5, Opus 4.7+, Fable 5). Omitting it leaves the provider default,
# which is high — the opposite of what "off" asks for.
OFF_EFFORT_FOR_ALWAYS_THINKING = "low"

# Shared configuration retains legacy budget fields for existing adapters.
# ``effort`` is metadata: adapters must not put it inside API thinking objects.
# "off" returns None (no thinking sent). Anthropic 4.6+ supports
# ``adaptive`` (model decides budget); older 4.x needs an explicit
# budget_tokens. On Sonnet 5 / Opus 4.7+ / Fable 5, budget_tokens is removed
# from the API (400 if sent) — adaptive is the only thinking mode, so those
# models must always get adaptive regardless of effort.
def _effort_to_thinking_config(effort: Optional[str], model_id: Optional[str]) -> Optional[dict]:
    if not effort or str(effort).lower() == "off":
        return None
    e = str(effort).lower()
    if e not in {"low", "medium", "high"}:
        return None
    supports_adaptive = bool(model_id) and any(
        tag in model_id
        for tag in (
            "sonnet-4-6", "opus-4-6", "opus-4-7", "sonnet-4-7",
            "sonnet-5", "opus-5", "opus-4-8", "fable-5", "mythos",
        )
    )
    if supports_adaptive:
        return {"type": "adaptive", "effort": e}
    if e == "low":
        return {"type": "enabled", "budget_tokens": 1024, "effort": e}
    if e == "medium":
        return {"type": "enabled", "budget_tokens": 5000, "effort": e}
    if e == "high":
        return {"type": "enabled", "budget_tokens": 15000, "effort": e}
    return None


def _detect_thinking_trigger(prompt_text: Optional[str]) -> bool:
    if not prompt_text:
        return False
    p = prompt_text.lower()
    return any(kw in p for kw in THINKING_TRIGGERS)


def _resolve_reasoning_effort(
    *,
    per_completion: Optional[str],
    prompt_text: Optional[str],
    model_default: Optional[str],
) -> str:
    """Resolution order: per-completion > trigger words > model default > off."""
    if per_completion:
        return per_completion.lower()
    if _detect_thinking_trigger(prompt_text):
        return "high"
    if model_default:
        return str(model_default).lower()
    return "off"


def selected_effort(thinking: Optional[dict]) -> Optional[str]:
    """Accept both explicit effort and legacy budget-based callers."""
    if not thinking or thinking.get("type") == "disabled":
        return None
    if thinking.get("effort") in {"low", "medium", "high"}:
        return thinking["effort"]
    budget = thinking.get("budget_tokens")
    if thinking.get("type") == "adaptive" or not budget:
        return "medium"
    return "high" if budget >= 10000 else "medium" if budget >= 3000 else "low"


def is_openai_reasoning_model(model_id: str) -> bool:
    # Strip gateway namespace, but do not guess opaque Azure deployment names.
    model = (model_id or "").lower().rsplit("/", 1)[-1]
    return model.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")) and not model.startswith(("o1-mini", "o1-preview", "gpt-5-chat"))


def supports_openai_summary(model_id: str) -> bool:
    return is_openai_reasoning_model(model_id) and not model_id.lower().rsplit("/", 1)[-1].startswith("o1")
