"""Dependency-free helpers for the MCP resource tools.

Kept import-light (no app/SQLAlchemy imports) so the pure logic — content
formatting, truncation, connection selection — is unit-testable without
importing the full implementations package. Mirrors _search_mcps_query.py.
"""
from typing import Any, Dict, List, Optional, Tuple

# Cap inline content so a large resource can't blow up the agent's context.
# The Pulse rulebook is ~20 KB, so this returns typical docs whole.
MAX_RESOURCE_CHARS = 50_000


def format_resource_contents(
    contents: List[Dict[str, Any]],
    max_chars: int = MAX_RESOURCE_CHARS,
) -> Tuple[str, Optional[str], bool]:
    """Join text blocks; render binary blocks as a placeholder.

    `contents` blocks are shaped by McpClient.aread_resource:
    text  -> {"type": "text", "text", "mime_type", "uri"}
    binary-> {"type": "binary", "byte_size", "mime_type", "uri"}

    Returns (content, mime_type, truncated).
    """
    parts: List[str] = []
    mime_type: Optional[str] = None
    for c in contents:
        if mime_type is None:
            mime_type = c.get("mime_type")
        if c.get("type") == "binary":
            mt = c.get("mime_type") or "application/octet-stream"
            parts.append(f"[binary resource: {mt}, {c.get('byte_size', 0)} bytes]")
        else:
            parts.append(c.get("text") or "")
    content = "\n".join(parts)
    truncated = False
    if len(content) > max_chars:
        content = content[:max_chars] + (
            f"\n… [truncated, {len(content)} total chars — read a more specific resource/URI]"
        )
        truncated = True
    return content, mime_type, truncated


def select_mcp_connection(connections: List[Any], connection_id: Optional[str]):
    """Pick the target MCP connection from those attached to a report.

    Returns the matching connection object, or an actionable error *string*
    when the choice is ambiguous or no match is found. Connection objects only
    need `.id` and `.name` attributes.
    """
    if connection_id:
        wanted = str(connection_id)
        match = next((c for c in connections if str(c.id) == wanted or c.name == wanted), None)
        if not match:
            return f"MCP connection '{connection_id}' not found on this report's data sources."
        return match
    if not connections:
        return "No MCP connections found on linked data sources."
    if len(connections) == 1:
        return connections[0]
    names = ", ".join(f"{c.name} ({c.id})" for c in connections)
    return f"Multiple MCP connections attached — specify connection_id. Options: {names}"
