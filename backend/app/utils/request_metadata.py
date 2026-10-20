"""Per-request metadata a caller attaches to a completion.

A completions API call may carry ``prompt.metadata``: a free-form JSON object
of values that only the calling app knows at question time (e.g. its chat id
or the ticket being discussed). It is stored with the completion and, on MCP
connections that enable *Dynamic params*, merged into the tool-call metadata
argument (see app.services.mcp_context_injection). BOW never interprets the
keys and never shows them to the LLM.

Dependency-free so request schemas can validate it.
"""
import json
from typing import Any, Dict, Optional

MAX_METADATA_BYTES = 8 * 1024


def validate_request_metadata(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Raise ValueError (a 422 at the API) for empty keys or an oversized object."""
    if value is None:
        return None
    if any(not str(k).strip() for k in value):
        raise ValueError("metadata keys must not be empty")
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > MAX_METADATA_BYTES:
        raise ValueError(f"metadata is {size} bytes; the limit is {MAX_METADATA_BYTES} bytes")
    return value


def completion_request_metadata(completion: Any) -> Dict[str, Any]:
    """The metadata stored on a completion's prompt, or ``{}``."""
    prompt = getattr(completion, "prompt", None)
    if not isinstance(prompt, dict):
        return {}
    meta = prompt.get("metadata")
    return meta if isinstance(meta, dict) else {}


def without_request_metadata(prompt: Any) -> Any:
    """A prompt dict minus its metadata, for views outside the report's own
    members (the public ``/c/{token}`` share page)."""
    if isinstance(prompt, dict) and "metadata" in prompt:
        return {k: v for k, v in prompt.items() if k != "metadata"}
    return prompt
