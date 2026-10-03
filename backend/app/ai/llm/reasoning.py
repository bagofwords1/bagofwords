"""Shared effort policy; adapters translate this configuration to their API."""
from typing import Optional, Sequence, Tuple

from app.utils.reasoning_effort import EFFORT_ORDER, USER_EFFORTS, normalize_effort  # noqa: F401 (re-exported)

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
    # GPT-6.1 Sol dropped "none" (GPT-6 Sol still has it).
    (("gpt-6.1",), ("low", "medium", "high", "xhigh", "max")),
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


# How an admin says a model reasons (``LLMModel.config["reasoning_mode"]``):
#   auto    — look the model id up in _FAMILIES (the default)
#   like    — look ``reasoning_model_id`` up instead (opaque Azure/Bedrock/
#             gateway deployment names that hide a known model)
#   generic — an OpenAI-compatible endpoint that takes reasoning_effort
#             low|medium|high (Ollama, vLLM, Groq, DeepSeek, …)
#   custom  — only the admin's raw per-level request fields are sent
#   off     — never send reasoning parameters
REASONING_MODES = ("auto", "like", "generic", "custom", "off")
_GENERIC = ("low", "medium", "high")


def reasoning_mode(config: Optional[dict]) -> str:
    cfg = config if isinstance(config, dict) else {}
    mode = cfg.get("reasoning_mode")
    if mode in REASONING_MODES:
        return mode
    # Configs written before modes existed: a capability model means "like".
    return "like" if cfg.get("reasoning_model_id") else "auto"


def reasoning_params(config: Optional[dict]) -> dict:
    """Admin raw request fields per user level: {"high": {...}, ...}."""
    cfg = config if isinstance(config, dict) else {}
    params = cfg.get("reasoning_params")
    if not isinstance(params, dict):
        return {}
    return {k: v for k, v in params.items() if k in USER_EFFORTS and isinstance(v, dict) and v}


def _efforts(model_id: Optional[str], mode: str, like_id: Optional[str], params: dict) -> Optional[Tuple[str, ...]]:
    if mode == "off":
        return ()
    if mode == "custom":
        return tuple(e for e in EFFORT_ORDER if e in params)
    if mode == "generic":
        return _GENERIC
    native = native_efforts((like_id if mode == "like" else None) or model_id)
    if native is None and params:
        # Unknown model, but the admin wrote raw fields: those levels exist.
        return tuple(e for e in EFFORT_ORDER if e in params)
    return native


def model_efforts(model_id: Optional[str], config: Optional[dict] = None) -> Tuple[str, ...]:
    """Efforts to offer for a configured model (``config`` = ``LLMModel.config``)."""
    cfg = config if isinstance(config, dict) else {}
    return _efforts(model_id, reasoning_mode(cfg), cfg.get("reasoning_model_id"), reasoning_params(cfg)) or ()


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


def lightest_effort(efforts: Optional[Sequence[str]]) -> Optional[str]:
    """What reasoning "off" sends to a model whose effort we know.

    "none" where the model accepts it, else its lightest level: OpenAI reasoning
    models run at their default (medium) when the request carries no effort, so
    leaving it out does not turn reasoning off. None when the efforts are
    unknown or the model does not reason.
    """
    if not efforts:
        return None
    if "none" in efforts:
        return "none"
    usable = [x for x in EFFORT_ORDER if x in efforts]
    return usable[0] if usable else None


def uses_thinking_budget(model_id: Optional[str]) -> bool:
    key = _capability_key(model_id)
    return key.startswith(_BUDGET_PREFIXES)


def reasoning_info(model_id: Optional[str], config: Optional[dict] = None) -> dict:
    """What the model picker and admin card need about a model's reasoning."""
    cfg = config if isinstance(config, dict) else {}
    efforts = model_efforts(model_id, cfg)
    try:
        default = normalize_effort(cfg.get("reasoning_effort"))
    except ValueError:
        default = None
    return {
        "supported": bool(efforts),
        "mode": reasoning_mode(cfg),
        "like_model_id": cfg.get("reasoning_model_id"),
        "efforts": list(efforts),
        "levels": {lvl: clamp_effort(lvl, efforts) for lvl in USER_EFFORTS} if efforts else {},
        "default": default,
        "params": reasoning_params(cfg),
    }


# ── Provider-client helpers ───────────────────────────────────────────────
#
# ``LLM.__init__`` copies the model's reasoning config onto the provider client
# (``reasoning_model_id``, ``reasoning_mode``, ``reasoning_params``) so every
# adapter reads the same admin settings.


def _client_cfg(client) -> Tuple[str, Optional[str], dict]:
    params = getattr(client, "reasoning_params", None)
    params = params if isinstance(params, dict) else {}
    like_id = getattr(client, "reasoning_model_id", None)
    mode = getattr(client, "reasoning_mode", None)
    if mode not in REASONING_MODES:
        mode = "like" if like_id else "auto"
    return mode, like_id, params


def efforts_for_client(client, model_id: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Accepted efforts for a client's model; None when unknown (pass through)."""
    mode, like_id, params = _client_cfg(client)
    return _efforts(model_id, mode, like_id, params)


def client_mode(client) -> str:
    return _client_cfg(client)[0]


def capability_model(client, model_id: Optional[str]) -> Optional[str]:
    mode, like_id, _ = _client_cfg(client)
    return (like_id if mode == "like" else None) or model_id


def client_reasons(client, model_id: Optional[str]) -> bool:
    """Whether an OpenAI-shaped client should send reasoning params for this model."""
    mode, like_id, params = _client_cfg(client)
    if mode == "off":
        return False
    if mode in ("generic", "custom"):
        return True
    return is_openai_reasoning_model((like_id if mode == "like" else None) or model_id)


def raw_params_for(client, requested: Optional[str], sent: Optional[str]) -> dict:
    """The admin's raw fields for this call: the requested level's, else the clamped one's."""
    _, _, params = _client_cfg(client)
    if not params:
        return {}
    for key in (requested, sent):
        if key and key in params:
            return params[key]
    return {}


def deep_merge(base: dict, extra: dict) -> dict:
    """Merge ``extra`` into a copy of ``base``; nested dicts merge, other values replace."""
    out = dict(base or {})
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Families whose function tools only work with reasoning_effort "none" on Chat
# Completions (verified live on OpenAI and Azure v1: gpt-5.6-*, gpt-6-* — gpt-6
# rejects tools there even at its default effort). They take the Responses API.
def needs_responses_for_tools(model_id: Optional[str]) -> bool:
    return _capability_key(model_id).startswith(("gpt-6", "gpt-5.6"))


def chat_completions_efforts(efforts: Optional[Tuple[str, ...]]) -> Optional[Tuple[str, ...]]:
    """Chat Completions accepts no "max" (verified live); clamp it to xhigh there."""
    if efforts is None:
        return None
    return tuple(e for e in efforts if e != "max")


def merge_raw_params(request_kwargs: dict, raw: dict, passthrough_key: Optional[str] = "extra_body") -> dict:
    """Apply an admin's raw per-level fields to an SDK request, in place.

    Keys the request already sets are deep-merged into it (so ``{"reasoning":
    {"summary": "detailed"}}`` keeps the effort we computed); anything else goes
    to ``passthrough_key`` (the SDK's ``extra_body``), which the SDK appends to
    the JSON body verbatim. ``passthrough_key=None`` merges everything at the
    top level (for bodies we build ourselves, e.g. Bedrock's
    additionalModelRequestFields).
    """
    for key, value in (raw or {}).items():
        if key in request_kwargs or passthrough_key is None:
            current = request_kwargs.get(key)
            request_kwargs[key] = deep_merge(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
        else:
            extra = dict(request_kwargs.get(passthrough_key) or {})
            extra[key] = deep_merge(extra[key], value) if isinstance(extra.get(key), dict) and isinstance(value, dict) else value
            request_kwargs[passthrough_key] = extra
    return request_kwargs
