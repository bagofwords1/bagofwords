"""Build a native multi-turn transcript from the planner's existing state.

Migration bridge. The agent loop already records everything a transcript needs
— `observation_builder.tool_observations` holds `tool_name`, `tool_input` and
the observation per `loop_index` — it is just re-serialized into one user
message instead of being replayed as turns.

This module reconstructs turns from that record, so the native path can be
switched on without the agent loop having to maintain a parallel structure
first. Once the loop keeps a `Transcript` directly, this becomes unnecessary.

Off unless enabled: `BOW_PLANNER_TRANSCRIPT=1`, or `PlannerInput.use_transcript`.

Known limitation: provider tool_use ids are dropped after pairing in
planner_v3, so ids are synthesized here. They only have to be internally
consistent within a single request, which they are.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from app.ai.context.parts import (
    Outcome,
    ToolCallPart,
    ToolResultPart,
    estimate_tokens,
)
from app.ai.context.transcript import Transcript

# Observation keys that are bookkeeping/UI rather than model-visible result.
_METADATA_KEYS = {"images", "images_provided_as_vision"}


# Share of the model's context window the transcript may occupy before the
# decay ladder starts trimming. The rest is headroom for the system prompt,
# tools, static context and the response.
_DEFAULT_BUDGET_RATIO = 0.5

# Fallback window for a model whose context_window_tokens is unset. A NULL
# previously disabled decay ENTIRELY and silently — observed on an Azure
# deployment, where the transcript could then grow without limit until the
# provider rejected it. Assume the smallest window we would realistically be
# pointed at rather than assume none.
_ASSUMED_WINDOW_TOKENS = 128_000


def transcript_budget_tokens(planner_input: Any = None) -> int:
    """Token budget for the transcript.

    ``BOW_TRANSCRIPT_BUDGET_RATIO`` overrides the share (used to force the
    ladder to fire in testing — a real run rarely approaches the default).
    ``BOW_TRANSCRIPT_BUDGET_TOKENS`` overrides the absolute number.
    """
    absolute = os.environ.get("BOW_TRANSCRIPT_BUDGET_TOKENS")
    if absolute:
        try:
            value = int(absolute)
            if value > 0:
                return value
        except ValueError:
            pass

    window = getattr(planner_input, "context_window_tokens", None)
    if not isinstance(window, int) or window <= 0:
        window = _ASSUMED_WINDOW_TOKENS

    ratio = _DEFAULT_BUDGET_RATIO
    override = os.environ.get("BOW_TRANSCRIPT_BUDGET_RATIO")
    if override:
        try:
            parsed = float(override)
            if 0 < parsed <= 1:
                ratio = parsed
        except ValueError:
            pass
    return max(int(window * ratio), 1)


def enabled(planner_input: Any = None) -> bool:
    """On by default; ``BOW_PLANNER_TRANSCRIPT=0`` restores the legacy path.

    Flipped on after an A/B over 10-turn runs on Anthropic and OpenAI: same
    grade on a numeric, prompt-matched eval (7/10 and 6/10 on both paths) for
    ~20% fewer billed-fresh input tokens. The kill switch stays because this
    changes the request shape for every provider at once.
    """
    # Knowledge mode is a single-shot harness, not a tool loop: it has no prior
    # steps to replay, and its ask lives in _build_knowledge_user_message rather
    # than in user_message. Routing it through the transcript replaced that ask
    # with the generic static context, so the harness's own instructions never
    # reached the model — silently, since the system prompt still looked right.
    if planner_input is not None and (getattr(planner_input, "mode", "") or "") == "knowledge":
        return False
    if planner_input is not None and getattr(planner_input, "use_transcript", False):
        return True
    flag = os.environ.get("BOW_PLANNER_TRANSCRIPT")
    if flag is None or flag == "":
        return True
    return flag.strip().lower() in ("1", "true", "yes", "on")


def _outcome_of(observation: dict) -> Outcome:
    if not isinstance(observation, dict):
        return Outcome.SUCCESS
    if observation.get("success") is False or observation.get("error"):
        return Outcome.FAILED
    return Outcome.SUCCESS


def _split_result(observation: dict) -> tuple:
    """(model-visible content, metadata, images) for one observation."""
    if not isinstance(observation, dict):
        return str(observation or ""), {}, []
    metadata = {k: observation[k] for k in _METADATA_KEYS if k in observation}
    images = observation.get("images") or []
    visible = {k: v for k, v in observation.items() if k not in _METADATA_KEYS}
    try:
        content = json.dumps(visible, default=str)
    except Exception:
        content = str(visible)
    return content, metadata, list(images) if isinstance(images, list) else []


def _digest_of(tool_name: str, observation: dict) -> Optional[str]:
    """Compact form for an aged-out result.

    Until each tool declares its own digest (the `_digest_*` functions move next
    to their tools), derive one from the fields that make a result
    *referenceable*: its summary plus any ids a later step can act on.
    """
    if not isinstance(observation, dict):
        return None
    bits = []
    summary = observation.get("summary")
    if summary:
        bits.append(str(summary))
    for key in (
        "step_id", "query_id", "artifact_id", "visualization_id",
        "created_visualization_ids", "note_id", "file_id", "row_count",
    ):
        val = observation.get(key)
        if val:
            bits.append(f"{key}: {val}")
    if not bits:
        return None
    return f"{tool_name} — " + "; ".join(bits)


def build_transcript(planner_input: Any, static_context: str, ask: str) -> Transcript:
    """Return the run's transcript, prefixed with static context and the ask.

    Prefers the live transcript the agent loop keeps — it carries the
    provider's own tool_use ids and signatures, which a reconstruction from
    observations cannot. Falls back to reconstructing from
    ``past_observations`` for callers that have no loop (token estimation, the
    knowledge harness, tests).

    Turn 0 carries the static context — byte-stable for the run, so it is the
    natural cache prefix. Turn 1 is the ask.
    """
    t = Transcript()
    if static_context:
        t.add_user_text(static_context)
    # Earlier completions' tool results, before the ask: the run's own steps
    # continue after it, so the whole thing reads in chronological order and
    # turn 0 stays the byte-stable cache prefix.
    hydrated = getattr(planner_input, "hydrated_turns", None)
    if hydrated:
        t.turns.extend(hydrated)
    if ask:
        t.add_user_text(ask)

    live = getattr(planner_input, "transcript", None)
    if live is not None and getattr(live, "turns", None):
        t.turns.extend(live.turns)
        t.repair()
        return t

    observations = list(getattr(planner_input, "past_observations", None) or [])

    # Group by loop_index so a parallel batch becomes ONE assistant turn with
    # N calls and ONE user turn with N results — matching what the provider
    # emitted and what every provider expects back.
    grouped: list = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        loop = obs.get("loop_index")
        if grouped and loop is not None and grouped[-1][0] == loop:
            grouped[-1][1].append(obs)
        else:
            grouped.append((loop, [obs]))

    for seq, (loop, batch) in enumerate(grouped):
        calls, results = [], []
        for idx, obs in enumerate(batch):
            call_id = f"call_{seq}_{idx}"
            tool_name = obs.get("tool_name") or "unknown_tool"
            calls.append(ToolCallPart(
                id=call_id,
                tool_name=tool_name,
                args=obs.get("tool_input") or {},
            ))
            observation = obs.get("observation") or {}
            content, metadata, images = _split_result(observation)
            results.append(ToolResultPart(
                call_id=call_id,
                tool_name=tool_name,
                outcome=_outcome_of(observation),
                content=content,
                digest=_digest_of(tool_name, observation),
                metadata=metadata,
                tokens=estimate_tokens(content),
                images=images,
            ))
        t.add_assistant_step(calls=calls)
        t.add_tool_results(results)

    # Any call without a result (a cancelled step) gets an explicit
    # `interrupted` result rather than being left dangling.
    t.repair()
    return t
