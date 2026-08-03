"""Turn a tool observation into a typed ``ToolResultPart``.

One place decides three things a raw observation dict leaves implicit:

* **outcome** — success / failed / denied / interrupted, as a field rather than
  something later code re-derives by sniffing keys.
* **the two views** — what the model may see (``content``) versus what is only
  for the UI and the audit trail (``metadata``), which must never reach the
  model *or* the compaction summarizer.
* **the digest** — the compact form used once a result ages out of the context
  window. A tool declares its own via ``ToolMetadata.digest_keys``; the generic
  fallback keeps the summary plus whatever ids make the result referenceable
  later, which is what a decayed result is *for*.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.ai.context.parts import Outcome, ToolResultPart, estimate_tokens

# Observation keys that are bookkeeping / UI payload, not model-visible result.
METADATA_KEYS = {"images", "images_provided_as_vision", "bookkeeping_ack"}

# Ids and shape markers worth keeping when a result decays: they are what lets
# a later step point back at this one (load_step, read_query, edit_artifact…).
_REFERENCE_KEYS = (
    "step_id", "query_id", "artifact_id", "visualization_id",
    "created_visualization_ids", "visualization_ids", "note_id", "file_id",
    "session_file_id", "row_count", "col_count", "mode", "title",
)


def outcome_of(observation: Any) -> Outcome:
    """Classify a raw observation.

    ``denied`` and ``interrupted`` are set explicitly by their call sites (a
    rejected confirmation, a cancelled step); everything else is success unless
    the observation carries an error marker.
    """
    if not isinstance(observation, dict):
        return Outcome.SUCCESS
    explicit = observation.get("outcome")
    if isinstance(explicit, str):
        try:
            return Outcome(explicit)
        except ValueError:
            pass
    err = observation.get("error")
    if isinstance(err, dict) and err.get("type") == "mode_not_allowed":
        return Outcome.DENIED
    if observation.get("success") is False or err:
        return Outcome.FAILED
    return Outcome.SUCCESS


def split_views(observation: Any) -> tuple:
    """(model-visible content, metadata, images)."""
    if not isinstance(observation, dict):
        return (str(observation or ""), {}, [])
    metadata = {k: observation[k] for k in METADATA_KEYS if k in observation}
    images = observation.get("images") or []
    visible = {k: v for k, v in observation.items() if k not in METADATA_KEYS}
    try:
        content = json.dumps(visible, default=str)
    except Exception:
        content = str(visible)
    return (content, metadata, list(images) if isinstance(images, list) else [])


def digest_of(tool_name: str, observation: Any, digest_keys: Optional[tuple] = None) -> Optional[str]:
    """Compact, still-referenceable form of a result.

    ``digest_keys`` lets a tool name the fields that matter for it; without one
    the generic reference keys apply. Either way the summary leads, so the model
    remembers *what* the step did even when the payload is gone.
    """
    if not isinstance(observation, dict):
        return None
    bits = []
    summary = observation.get("summary")
    if summary:
        bits.append(str(summary))
    for key in (digest_keys or _REFERENCE_KEYS):
        val = observation.get(key)
        if val not in (None, "", [], {}):
            bits.append(f"{key}: {val}")
    if not bits:
        return None
    return f"{tool_name} — " + "; ".join(bits)


def build_result_part(
    *,
    call_id: str,
    tool_name: str,
    observation: Any,
    registry: Any = None,
) -> ToolResultPart:
    digest_keys = None
    if registry is not None:
        try:
            meta = registry.get_metadata(tool_name)
            digest_keys = tuple(getattr(meta, "digest_keys", None) or ()) or None
        except Exception:
            digest_keys = None

    content, metadata, images = split_views(observation)
    return ToolResultPart(
        call_id=call_id,
        tool_name=tool_name,
        outcome=outcome_of(observation),
        content=content,
        digest=digest_of(tool_name, observation, digest_keys),
        metadata=metadata,
        tokens=estimate_tokens(content),
        images=images,
    )
