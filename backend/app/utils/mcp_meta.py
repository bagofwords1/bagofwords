"""Caller metadata forwarded to MCP servers as the ``_meta`` of a tool call.

A completion request may carry an opaque ``prompt.metadata`` object (e.g. the
calling app's chat and message ids). It is stored with the completion and sent,
unchanged, as ``_meta`` on every MCP ``tools/call`` of that run. BOW never
interprets the keys and never shows them to the LLM.

Dependency-free so request schemas can validate it without importing the MCP
client.
"""
import json
from typing import Any, Dict, Optional

MAX_META_BYTES = 8 * 1024

# MCP puts ``progressToken`` in a request's ``_meta`` itself; a caller value
# there would be read as a progress token by the server.
_PROTOCOL_KEYS = {"progressToken"}

# MCP reserves key prefixes whose second-to-last label is ``modelcontextprotocol``
# or ``mcp`` (``modelcontextprotocol.io/``, ``api.mcp.dev/`` ...).
_RESERVED_LABELS = {"modelcontextprotocol", "mcp"}


def _is_reserved_key(key: str) -> bool:
    if key in _PROTOCOL_KEYS:
        return True
    if "/" not in key:
        return False
    labels = key.rsplit("/", 1)[0].split(".")
    return len(labels) >= 2 and labels[-2] in _RESERVED_LABELS


def validate_mcp_meta(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate caller metadata. Raises ValueError (a 422 at the API) when the
    object is too large or uses a key MCP reserves."""
    if value is None:
        return None
    reserved = sorted(k for k in value if _is_reserved_key(k))
    if reserved:
        raise ValueError(
            f"metadata keys reserved by the MCP protocol: {', '.join(reserved)}"
        )
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > MAX_META_BYTES:
        raise ValueError(
            f"metadata is {size} bytes; the limit is {MAX_META_BYTES} bytes"
        )
    return value


def completion_mcp_meta(completion: Any) -> Optional[Dict[str, Any]]:
    """The caller metadata stored on a completion's prompt, or None."""
    prompt = getattr(completion, "prompt", None)
    if not isinstance(prompt, dict):
        return None
    meta = prompt.get("metadata")
    return meta if isinstance(meta, dict) and meta else None


def without_caller_meta(prompt: Any) -> Any:
    """A prompt dict minus its caller metadata — for views outside the
    report's own members (the public ``/c/{token}`` share page)."""
    if isinstance(prompt, dict) and "metadata" in prompt:
        return {k: v for k, v in prompt.items() if k != "metadata"}
    return prompt
