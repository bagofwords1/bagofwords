"""Per-call LLM I/O trace — diagnostic only, off unless the env var is set.

Writes one JSON line per ``inference_stream_v2`` call to ``BOW_LLM_TRACE_FILE``
capturing the exact request that went over the wire and what came back. This is
the ground truth for "what did the model actually see, what did it cost, and how
long did it take" — the alternative is inferring it from the prompt builders,
which is how the missing cache breakpoints went unnoticed.

Records the request AFTER PII redaction, so a trace file is safe to keep
alongside the run it describes.

Enable with::

    BOW_LLM_TRACE_FILE=/path/to/trace.jsonl
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.settings.logging_config import get_logger

logger = get_logger(__name__)


def enabled() -> bool:
    return bool(os.environ.get("BOW_LLM_TRACE_FILE"))


def _message_shape(messages: Optional[list]) -> list:
    """Per-message summary: role, block types, and sizes.

    Deliberately structural — the full text lives in ``messages_full`` only when
    BOW_LLM_TRACE_FULL is set, since a schema-heavy prompt is ~100KB per call
    and a 10-turn run would otherwise write hundreds of megabytes.
    """
    out = []
    for m in messages or []:
        content = getattr(m, "content", None)
        if isinstance(content, str):
            out.append({
                "role": getattr(m, "role", None),
                "kind": "str",
                "chars": len(content),
            })
        elif isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict):
                    btype = b.get("type")
                    size = 0
                    for key in ("text", "content"):
                        v = b.get(key)
                        if isinstance(v, str):
                            size += len(v)
                        elif v is not None:
                            size += len(json.dumps(v, default=str))
                    blocks.append({"type": btype, "chars": size})
                else:
                    blocks.append({"type": "unknown", "chars": 0})
            out.append({
                "role": getattr(m, "role", None),
                "kind": "blocks",
                "blocks": blocks,
                "chars": sum(b["chars"] for b in blocks),
            })
    return out


def write(record: dict) -> None:
    """Append one trace record. Never raises — diagnostics must not break a run."""
    path = os.environ.get("BOW_LLM_TRACE_FILE")
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with open(path, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.debug("llm trace write failed", exc_info=True)


def build_record(
    *,
    provider: str,
    model_id: str,
    client: str,
    system: Optional[str],
    messages: Optional[list],
    tools: Optional[list],
    usage_scope: Optional[str],
    **extra: Any,
) -> dict:
    """Assemble the request side of a trace record."""
    system_chars = len(system) if isinstance(system, str) else 0
    shape = _message_shape(messages)
    record = {
        "provider": provider,
        "model_id": model_id,
        "client": client,
        "usage_scope": usage_scope,
        "system_chars": system_chars,
        "tool_count": len(tools or []),
        "tools_chars": sum(
            len(getattr(t, "name", "") or "")
            + len(getattr(t, "description", "") or "")
            + len(json.dumps(getattr(t, "input_schema", {}) or {}, default=str))
            for t in (tools or [])
        ),
        "message_count": len(shape),
        "messages_shape": shape,
        "request_chars": system_chars + sum(m.get("chars", 0) for m in shape),
    }
    if os.environ.get("BOW_LLM_TRACE_FULL"):
        record["system_full"] = system
        record["messages_full"] = [
            {"role": getattr(m, "role", None), "content": getattr(m, "content", None)}
            for m in (messages or [])
        ]
        record["tools_full"] = [
            {
                "name": getattr(t, "name", None),
                "description": getattr(t, "description", None),
                "input_schema": getattr(t, "input_schema", None),
            }
            for t in (tools or [])
        ]
    record.update(extra)
    return record
