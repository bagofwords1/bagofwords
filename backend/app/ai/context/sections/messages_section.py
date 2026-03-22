import re
from typing import ClassVar, List, Optional
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape

# Patterns to extract referenceable IDs from tool digest text
_ID_PATTERNS = [
    re.compile(r'viz_id:\s*([a-f0-9\-]+)', re.IGNORECASE),
    re.compile(r'artifact_id:\s*([a-f0-9\-]+)', re.IGNORECASE),
    re.compile(r'query_id:\s*([a-f0-9\-]+)', re.IGNORECASE),
    re.compile(r'artifact:\s*([^\|;]+)'),
    re.compile(r'query:\s*([^\|;]+)'),
]

# Number of recent messages to render in full
_RECENT_FULL = 7


class MessageItem(BaseModel):
    role: str
    timestamp: Optional[str] = None
    text: str
    mentions: Optional[str] = None


class MessagesSection(ContextSection):
    tag_name: ClassVar[str] = "conversation"

    items: List[MessageItem] = []

    def render(self) -> str:
        if not self.items:
            return ""
        total = len(self.items)
        cutoff = max(total - _RECENT_FULL, 0)
        lines: List[str] = []
        for idx, m in enumerate(self.items):
            who = "User" if m.role == "user" else "Assistant"
            ts = f" ({m.timestamp})" if m.timestamp else ""
            if idx < cutoff:
                # Minified: keep role, timestamp, and any referenceable IDs
                lines.append(f"{who}{ts}: {_minify_message(m.text)}")
            else:
                # Full render for recent messages
                suffix = f" | mentions: {xml_escape(m.mentions)}" if m.mentions else ""
                lines.append(f"{who}{ts}: {xml_escape(m.text.strip())}{suffix}")
        return xml_tag(self.tag_name, "\n".join(lines))


def _minify_message(text: str) -> str:
    """Reduce an older message to a short summary preserving referenceable IDs."""
    text = text.strip()
    if not text:
        return "[empty]"
    # Extract IDs worth preserving
    ids: List[str] = []
    for pat in _ID_PATTERNS:
        for m in pat.finditer(text):
            ids.append(m.group(0).strip())
    # Extract tool name if present (e.g. "Tool: create_data → ... (success)")
    tool_match = re.search(r'Tool:\s*(\S+)', text)
    tool_part = tool_match.group(0) if tool_match else None
    # Build minified line
    # First 120 chars of original text as context snippet
    snippet = text[:120].rstrip()
    if len(text) > 120:
        snippet += "…"
    parts = [xml_escape(snippet)]
    if tool_part and tool_part not in snippet:
        parts.append(xml_escape(tool_part))
    for id_str in ids:
        if id_str not in snippet:
            parts.append(xml_escape(id_str))
    return " | ".join(parts)


