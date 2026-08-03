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


def enabled(planner_input: Any = None) -> bool:
    if planner_input is not None and getattr(planner_input, "use_transcript", False):
        return True
    return os.environ.get("BOW_PLANNER_TRANSCRIPT", "").lower() in ("1", "true", "yes")


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
    """Reconstruct the run as turns.

    Turn 0 carries the static context (instructions, schemas, files, resources)
    — byte-stable for the run, so it is the natural cache prefix. Turn 1 is the
    ask. Every recorded loop then becomes an assistant(tool_call) /
    user(tool_result) pair.
    """
    t = Transcript()
    if static_context:
        t.add_user_text(static_context)
    if ask:
        t.add_user_text(ask)

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
