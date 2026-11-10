"""One context budget for the planner prompt.

Every size decision about the planner prompt — when to decay old tool output,
when to fold history into the rolling summary, how hard the last-resort trim
cuts, how big the protected tail and the summary may be — derives from a
single ``ContextBudget`` built from the MAIN model's context window. The
summarizer (usually the small model) never decides when compaction fires; its
own window only bounds what it is handed.

Token counting is "anchor + delta": the provider reports the real prompt size
of every planner call, and the gap between that number and our cheap estimate
(``offset``) is mostly the static prompt we do not estimate (system prompt,
tool catalog). ``measure`` = cheap estimate of the current input + the last
known offset, so the count tracks reality without re-rendering the prompt.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 200_000

# Shares of the usable window (window minus output reserve).
CHEAP_RATIO = 0.60       # decay old tool output / history rendering (no LLM)
SUMMARIZE_RATIO = 0.75   # fold history into the rolling summary (LLM)
TARGET_RATIO = 0.40      # aim here after compacting (batches folds → cache-friendly)

# Protected recent tail (tokens): 2.5% of the window clamped 2k..25k, but
# never more than a fifth of the usable window (small/overridden windows).
TAIL_RATIO = 0.025
TAIL_MIN = 2_000
TAIL_MAX = 25_000
TAIL_MAX_SHARE_OF_USABLE = 0.20

# Rolling summary size: 5% of the window clamped 2k..12k, and at most a
# tenth of the usable window.
SUMMARY_RATIO = 0.05
SUMMARY_MIN = 2_000
SUMMARY_MAX = 12_000
SUMMARY_MAX_SHARE_OF_USABLE = 0.10

OUTPUT_RESERVE_MAX = 20_000


def _env_ratio(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = float(raw)
        return val if 0 < val <= 1 else default
    except ValueError:
        return default


def model_window(model: Any) -> int:
    try:
        w = int(getattr(model, "context_window_tokens", None) or 0)
        return w if w > 0 else DEFAULT_WINDOW
    except Exception:
        return DEFAULT_WINDOW


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output_reserve: int
    usable: int
    cheap_at: int
    summarize_at: int
    target: int
    tail_tokens: int
    summary_max: int

    def as_dict(self) -> dict:
        return asdict(self)


def budget_for(model: Any = None, *, window: Optional[int] = None, factor: float = 1.0) -> ContextBudget:
    """Budget for the model that will RECEIVE the prompt.

    ``factor`` (<1) tightens the usable window after a provider overflow
    rejection, so the next attempt provably aims lower.
    """
    w = int(window) if window else model_window(model)
    reserve = min(OUTPUT_RESERVE_MAX, max(w // 4, 1))
    usable = max(int((w - reserve) * max(min(factor, 1.0), 0.05)), 1)
    cheap = _env_ratio("BOW_CONTEXT_CHEAP_RATIO", CHEAP_RATIO)
    summarize = _env_ratio("BOW_CONTEXT_SUMMARIZE_RATIO", SUMMARIZE_RATIO)
    target = _env_ratio("BOW_CONTEXT_TARGET_RATIO", TARGET_RATIO)
    tail = min(max(int(w * TAIL_RATIO), TAIL_MIN), TAIL_MAX, int(usable * TAIL_MAX_SHARE_OF_USABLE))
    summary_max = min(
        max(int(w * SUMMARY_RATIO), SUMMARY_MIN),
        SUMMARY_MAX,
        max(int(usable * SUMMARY_MAX_SHARE_OF_USABLE), 500),
    )
    return ContextBudget(
        window=w,
        output_reserve=reserve,
        usable=usable,
        cheap_at=int(usable * cheap),
        summarize_at=int(usable * summarize),
        target=int(usable * target),
        tail_tokens=max(tail, 1),
        summary_max=summary_max,
    )


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

# Free-text PlannerInput fields that reach the prompt. Shared with
# trim_context_to_budget so both see the same size.
PLANNER_TEXT_FIELDS = (
    "instructions", "schemas_combined", "schemas_excerpt",
    "files_context", "messages_context", "resources_context",
    "resources_combined", "mentions_context", "entities_context",
    "history_summary", "user_message",
)


def estimate_text_tokens(text: Optional[str]) -> int:
    """3 chars/token — deliberately conservative (dense data, JSON and
    non-Latin text tokenize closer to 2-3 chars/token)."""
    return len(text) // 3 if text else 0


def estimate_planner_input_tokens(planner_input: Any) -> int:
    """Cheap estimate of the variable part of the planner prompt.

    The live transcript (native tool turns, history included) and the JSON
    observations describe the same steps on the two prompt paths, so the
    larger of the two counts — never both."""
    total = 0
    for f in PLANNER_TEXT_FIELDS:
        val = getattr(planner_input, f, None)
        if isinstance(val, str):
            total += estimate_text_tokens(val)
    obs = 0
    past = getattr(planner_input, "past_observations", None)
    if past:
        try:
            obs += estimate_text_tokens(json.dumps(past, default=str))
        except Exception:
            obs += len(past) * 200
    last = getattr(planner_input, "last_observation", None)
    if last:
        try:
            obs += estimate_text_tokens(json.dumps(last, default=str))
        except Exception:
            obs += 500
    transcript_tokens = 0
    transcript = getattr(planner_input, "transcript", None)
    if transcript is not None and getattr(transcript, "turns", None):
        try:
            transcript_tokens = int(transcript.tokens())
        except Exception:
            transcript_tokens = 0
    return total + max(obs, transcript_tokens)


def total_input_tokens(token_usage: Any, provider: Optional[str] = None) -> Optional[int]:
    """The whole prompt the provider processed, cache included.

    Anthropic reports ``prompt_tokens`` without the cached part — cache reads
    and writes come in separate fields — while the other providers include
    it. Same normalization as RunEfficiency.add_planner_usage and
    LLM._quota_total_tokens; without it, a well-cached request looks tiny and
    the context budget would stop seeing most of the prompt."""
    if token_usage is None:
        return None
    prompt = getattr(token_usage, "prompt_tokens", None)
    if prompt is None:
        return None
    total = int(prompt or 0)
    if provider == "anthropic":
        total += int(getattr(token_usage, "cache_read_tokens", 0) or 0)
        total += int(getattr(token_usage, "cache_creation_tokens", 0) or 0)
    return total


class TokenAnchor:
    """Provider-reported size of the last request vs. our estimate of it."""

    def __init__(self, offset: int = 0, known: bool = False) -> None:
        self.offset = int(offset)
        self.known = bool(known)

    def record(self, actual_prompt_tokens: Optional[int], estimate: Optional[int]) -> None:
        if not actual_prompt_tokens or estimate is None:
            return
        self.offset = int(actual_prompt_tokens) - int(estimate)
        self.known = True

    def measure(self, estimate: int) -> int:
        # A negative offset means our estimate over-counts; never let it
        # erase more than half of the estimate.
        if not self.known:
            return int(estimate)
        return max(int(estimate) + self.offset, int(estimate) // 2)
