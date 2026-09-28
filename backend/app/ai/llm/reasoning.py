"""Shared effort policy; adapters translate this configuration to their API."""
from typing import Optional, Sequence, Tuple

# Every effort the backend understands, weakest to strongest. Providers accept
# different subsets (see native_efforts); clamp_effort maps a request onto the
# subset a given model accepts, so a user's pick never becomes a 400.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# The levels the product offers users. "max" means "the strongest this model
# has" (max, else xhigh, else high) — see clamp_effort.
USER_EFFORTS = ("low", "medium", "high", "max")

# Values meaning "no explicit choice": resolve from the report / trigger words /
# model default instead.
_DEFAULT_ALIASES = {"", "default", "auto"}


def normalize_effort(value) -> Optional[str]:
    """Validate an effort coming from an API payload or stored JSON.

    Returns None for "no explicit choice", "off" for off/none, otherwise one of
    EFFORT_ORDER. Raises ValueError for anything else so a typo is a 422, not a
    silently ignored setting.
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
        f"reasoning_effort must be one of: default, off, {', '.join(EFFORT_ORDER[1:])}"
    )

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

# Token budgets for models that take a thinking budget instead of an effort
# (Claude <= 4.5, Gemini 2.5). xhigh/max share the largest budget: 31,999 is the
# biggest Anthropic accepts without streaming-only constraints, and adapters cap
# it to the model's own output limit.
THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 1024,
    "medium": 5000,
    "high": 15000,
    "xhigh": 31999,
    "max": 31999,
}

# Shared configuration retains legacy budget fields for existing adapters.
# ``effort`` is metadata: adapters must not put it inside API thinking objects.
# "off" returns None (no thinking sent). Anthropic 4.6+ supports
# ``adaptive`` (model decides budget); older 4.x needs an explicit
# budget_tokens. On Sonnet 5 / Opus 4.7+ / Fable 5, budget_tokens is removed
# from the API (400 if sent) — adaptive is the only thinking mode, so those
# models must always get adaptive regardless of effort.
def _effort_to_thinking_config(effort: Optional[str], model_id: Optional[str]) -> Optional[dict]:
    if not effort or str(effort).lower() in ("off", "none"):
        return None
    e = str(effort).lower()
    if e not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
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
    return {"type": "enabled", "budget_tokens": THINKING_BUDGETS[e], "effort": e}


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
    if thinking.get("effort") in {"minimal", "low", "medium", "high", "xhigh", "max"}:
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


# ── Per-model capability ──────────────────────────────────────────────────
#
# Which efforts each model family's API accepts. Matched on the model id (or an
# admin-set ``reasoning_model_id`` for opaque Azure/gateway deployment names),
# after stripping gateway namespaces ("openai/…") and Bedrock/Vertex prefixes
# ("us.anthropic.…", "…@20250101"). Checked top to bottom, first match wins, so
# more specific families come first. ``()`` = the model does not reason; no
# match = unknown (the admin can opt it in with ``reasoning_supported``).
_ADAPTIVE_FULL = ("low", "medium", "high", "xhigh", "max")
_ADAPTIVE_NO_XHIGH = ("low", "medium", "high", "max")
# Budget-only models: every level maps to a token budget (THINKING_BUDGETS).
_BUDGET = ("low", "medium", "high", "max")

_FAMILIES: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], ...] = (
    # Anthropic — adaptive thinking + output_config.effort
    (("claude-fable-5", "claude-mythos", "claude-opus-5", "claude-sonnet-5",
      "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-7"), _ADAPTIVE_FULL),
    (("claude-opus-4-6", "claude-sonnet-4-6"), _ADAPTIVE_NO_XHIGH),
    # Anthropic — budget_tokens only
    (("claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1",
      "claude-opus-4", "claude-sonnet-4", "claude-3-7-sonnet"), _BUDGET),
    (("claude-",), ()),
    # OpenAI — reasoning.effort / reasoning_effort
    (("gpt-6-astra",), ("low", "medium", "high", "xhigh", "max")),
    (("gpt-6",), ("none", "low", "medium", "high", "xhigh", "max")),
    (("gpt-5.6",), ("none", "low", "medium", "high", "xhigh", "max")),
    (("gpt-5.5", "gpt-5.4", "gpt-5.3", "gpt-5.2"), ("none", "low", "medium", "high", "xhigh")),
    (("gpt-5.1",), ("none", "low", "medium", "high")),
    (("gpt-5-chat",), ()),
    (("gpt-5",), ("minimal", "low", "medium", "high")),
    (("o1-mini", "o1-preview"), ()),
    (("o1", "o3", "o4"), ("low", "medium", "high")),
    (("gpt-4", "gpt-3.5", "gpt-image"), ()),
    # Google — thinking_level (3.x) / thinking_budget (2.5)
    (("gemini-3",), ("low", "medium", "high")),
    (("gemini-2.5",), _BUDGET),
    (("gemini-",), ()),
)

# Families whose API takes a token budget rather than an effort name.
_BUDGET_PREFIXES = ("gemini-2.5",)


def _capability_key(model_id: Optional[str]) -> str:
    m = (model_id or "").strip().lower()
    m = m.rsplit("/", 1)[-1]            # openai/gpt-5.5, publishers/…/models/x
    m = m.split("@", 1)[0]              # vertex claude-…@20250101
    if "anthropic." in m:               # us.anthropic.claude-…-v1:0 (bedrock)
        m = m.split("anthropic.", 1)[1]
    return m


def native_efforts(model_id: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Efforts the model's API accepts; ``()`` if it does not reason; None if unknown."""
    key = _capability_key(model_id)
    if not key:
        return None
    for prefixes, efforts in _FAMILIES:
        for p in prefixes:
            # OpenAI/Gemini ids are prefixes; Claude family names can sit
            # behind a gateway prefix, so match those anywhere.
            if key.startswith(p) or (p.startswith("claude-") and p in key):
                return efforts
    return None


def model_efforts(model_id: Optional[str], config: Optional[dict] = None) -> Tuple[str, ...]:
    """Efforts to offer/send for a configured model, honoring admin overrides.

    ``config`` is ``LLMModel.config``: ``reasoning_model_id`` names the real
    model behind an opaque deployment, ``reasoning_supported`` (True/False)
    forces the capability on or off (e.g. a custom OpenAI-compatible model that
    takes ``reasoning_effort``).
    """
    cfg = config if isinstance(config, dict) else {}
    capability_model = cfg.get("reasoning_model_id") or model_id
    native = native_efforts(capability_model)
    override = cfg.get("reasoning_supported")
    if override is False:
        return ()
    if override is True and not native:
        return ("low", "medium", "high")
    return native or ()


def clamp_effort(effort: Optional[str], efforts: Optional[Sequence[str]]) -> Optional[str]:
    """Map a requested effort onto what the model accepts.

    "max" means the strongest the model has (max, else xhigh, else its top
    level). Any other level snaps to the nearest accepted one (ties go up).
    Returns None when nothing should be sent: no effort, "off", or a model that
    does not reason. With ``efforts`` unknown (None) the request passes through.
    """
    if not effort:
        return None
    e = str(effort).lower()
    if e in ("off", "none"):
        return None
    if efforts is None:
        return e
    usable = [x for x in EFFORT_ORDER if x in efforts and x != "none"]
    if not usable:
        return None
    if e == "max":
        for top in ("max", "xhigh"):
            if top in usable:
                return top
        return usable[-1]
    if e in usable:
        return e
    if e not in EFFORT_ORDER:
        return None
    want = EFFORT_ORDER.index(e)
    return min(usable, key=lambda x: (abs(EFFORT_ORDER.index(x) - want), -EFFORT_ORDER.index(x)))


def uses_thinking_budget(model_id: Optional[str]) -> bool:
    key = _capability_key(model_id)
    return key.startswith(_BUDGET_PREFIXES)


def reasoning_info(model_id: Optional[str], config: Optional[dict] = None) -> dict:
    """What the model picker needs: whether effort applies and what each level runs as."""
    efforts = model_efforts(model_id, config)
    cfg = config if isinstance(config, dict) else {}
    default = cfg.get("reasoning_effort")
    try:
        default = normalize_effort(default)
    except ValueError:
        default = None
    return {
        "supported": bool(efforts),
        "efforts": list(efforts),
        "levels": {lvl: clamp_effort(lvl, efforts) for lvl in USER_EFFORTS} if efforts else {},
        "default": default,
    }
