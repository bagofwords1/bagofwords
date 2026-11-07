"""In-run efficiency readout for the planner.

The agent's token use is recorded per LLM call, but the planner never sees it
while it works, so it cannot notice that it keeps re-running the same call or
that its input stopped coming from cache. This module keeps a small running
tally for one agent run and renders it as a short ``<run_efficiency>`` line in
the volatile per-turn head (see ``PromptBuilderV3._build_turn_head``), which
sits below the cached prefix — adding it never invalidates the cache.

The line is diagnostic, not a budget: it is shown only once a run is long
enough to be worth looking at (in steps or in input tokens), or as soon as an
identical call repeats, and it tells the model that correctness comes first and
that the numbers are not for the user.

``BOW_RUN_EFFICIENCY=1`` turns it on (off by default until an eval A/B shows
pass rate holds).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Iterations before the line appears when nothing repeats. Short runs pay
# nothing; a run this long is where waste accumulates.
SHOW_AFTER_ITERATIONS = 4

# ...or once the run has read this many input tokens, whichever comes first.
# Parallel tool batches keep the step count low on runs that are still large.
SHOW_AFTER_INPUT_TOKENS = 200_000

# A call counts as repeated from its second identical occurrence.
REPEAT_THRESHOLD = 2

# Tools whose identical re-call is expected (polling, waiting) — never flagged.
_REPEAT_EXEMPT = frozenset({"wait", "cancel_wait", "read_sub_agent", "message_sub_agent"})

# At most this many repeated calls are named, so the line stays one line.
_MAX_REPEATS_LISTED = 3


def enabled() -> bool:
    flag = os.environ.get("BOW_RUN_EFFICIENCY")
    if flag is None or flag.strip() == "":
        return False
    return flag.strip().lower() in ("1", "true", "yes", "on")


def _call_signature(tool_name: str, tool_input: Any) -> str:
    try:
        if hasattr(tool_input, "model_dump"):
            tool_input = tool_input.model_dump()
        payload = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        payload = str(tool_input)
    digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()
    return f"{tool_name}:{digest}"


@dataclass
class RunEfficiency:
    """Running tally for one agent run."""

    planner_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    _calls: Dict[str, int] = field(default_factory=dict)
    _names: Dict[str, str] = field(default_factory=dict)

    def add_planner_usage(self, token_usage: Any, provider: Optional[str] = None) -> None:
        """Add one planner call. Anthropic reports ``prompt_tokens`` without
        the cached part (cache reads and writes come separately); other
        providers include it. Normalize to total input so the cache share
        means the same thing on every provider (as ``LLM._quota_total_tokens``
        does)."""
        if token_usage is None:
            return
        prompt = int(getattr(token_usage, "prompt_tokens", 0) or 0)
        cache_read = int(getattr(token_usage, "cache_read_tokens", 0) or 0)
        if provider == "anthropic":
            prompt += cache_read + int(getattr(token_usage, "cache_creation_tokens", 0) or 0)
        self.planner_calls += 1
        self.input_tokens += prompt
        self.output_tokens += int(getattr(token_usage, "completion_tokens", 0) or 0)
        self.cache_read_tokens += cache_read

    def add_tool_call(self, tool_name: Optional[str], tool_input: Any) -> None:
        if not tool_name or tool_name in _REPEAT_EXEMPT:
            return
        sig = _call_signature(tool_name, tool_input)
        self._calls[sig] = self._calls.get(sig, 0) + 1
        self._names[sig] = tool_name

    def repeated_calls(self) -> Dict[str, int]:
        """Tool name → highest repeat count among its identical calls."""
        out: Dict[str, int] = {}
        for sig, count in self._calls.items():
            if count >= REPEAT_THRESHOLD:
                name = self._names[sig]
                out[name] = max(out.get(name, 0), count)
        return out

    def cache_share(self) -> Optional[float]:
        if self.input_tokens <= 0:
            return None
        return min(self.cache_read_tokens / self.input_tokens, 1.0)

    def render(self, iteration: int) -> str:
        """The ``<run_efficiency>`` line for planner iteration ``iteration``
        (0-based), or "" when there is nothing worth showing yet."""
        repeats = self.repeated_calls()
        large = self.input_tokens >= SHOW_AFTER_INPUT_TOKENS
        if not repeats and not large and iteration < SHOW_AFTER_ITERATIONS:
            return ""
        if self.planner_calls == 0 and not repeats:
            return ""

        bits = [f"step {iteration + 1}"]
        if self.planner_calls:
            share = self.cache_share()
            cache = f" ({round(share * 100)}% from cache)" if share is not None else ""
            bits.append(f"input so far {self.input_tokens:,} tokens{cache}")
            bits.append(f"output {self.output_tokens:,}")
        guidance = "Diagnostic only: getting the answer right comes first; never mention these numbers to the user."
        if repeats:
            ranked = sorted(repeats.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_REPEATS_LISTED]
            listed = ", ".join(f"{name} ×{count}" for name, count in ranked)
            bits.append(f"identical calls repeated: {listed}")
            guidance += (
                " An identical call returns what it returned before unless something it reads "
                "has changed since — reuse that earlier result instead of calling again."
            )
        return f"<run_efficiency>{' · '.join(bits)}. {guidance}</run_efficiency>"
