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
_COMPACTION_OBSERVATION_MARKER = "Model-visible result:"


class MessageItem(BaseModel):
    role: str
    timestamp: Optional[str] = None
    text: str
    mentions: Optional[str] = None


class MessagesSection(ContextSection):
    tag_name: ClassVar[str] = "conversation"

    items: List[MessageItem] = []
    # Rolling compaction summary of turns older than the detailed window
    # (see ContextCompactionService). Rendered before <conversation> as
    # historical context, never as instructions.
    history_summary: Optional[str] = None

    # Leading items that are the report's protected opening exchange (set by
    # the builder once a compaction watermark exists). They render in their
    # own <opening_exchange> block ahead of <conversation> so the last-resort
    # trim can pin them alongside the summary.
    head_count: int = 0
    # Rows past the compaction watermark that the live window's row cap left
    # out: in neither the window nor the summary. Not rendered — the agent's
    # governor folds them so they reach the summary.
    dropped_count: int = 0

    def render(self, decay_level: int = 0) -> str:
        """Render the history.

        ``decay_level`` is the context governor's cheap, no-LLM rung for
        cross-turn history: 0 = default (last 7 messages in full), 1 = only
        the last 3 in full, 2 = only the newest exchange in full with shorter
        snippets. Older messages always keep their role, timestamp, tool name
        and referenceable ids, so nothing becomes unaddressable.
        """
        summary_block = (
            xml_tag("history_summary", xml_escape(self.history_summary))
            if self.history_summary else ""
        )
        head_n = max(min(self.head_count, len(self.items)), 0)
        head_items = self.items[:head_n]
        body_items = self.items[head_n:]
        blocks: List[str] = []
        if summary_block:
            blocks.append(summary_block)
        if head_items:
            head_lines = [
                f"{_who(m)}{_ts(m)}: {_minify_message(m.text, snippet_len=300)}"
                for m in head_items
            ]
            blocks.append(xml_tag("opening_exchange", "\n".join(head_lines)))
        if not body_items:
            return "\n".join(blocks)
        recent_full = {0: _RECENT_FULL, 1: 3}.get(decay_level, 2)
        snippet_len = 120 if decay_level < 2 else 60
        total = len(body_items)
        cutoff = max(total - recent_full, 0)
        lines: List[str] = []
        for idx, m in enumerate(body_items):
            if idx < cutoff:
                # Minified: keep role, timestamp, and any referenceable IDs
                lines.append(f"{_who(m)}{_ts(m)}: {_minify_message(m.text, snippet_len=snippet_len)}")
            else:
                # Full render for recent messages
                suffix = f" | mentions: {xml_escape(m.mentions)}" if m.mentions else ""
                lines.append(f"{_who(m)}{_ts(m)}: {xml_escape(m.text.strip())}{suffix}")
        blocks.append(xml_tag(self.tag_name, "\n".join(lines)))
        return "\n".join(blocks)


def _who(m: "MessageItem") -> str:
    if m.role == "user":
        return "User"
    if m.role == "event":
        return "Event"
    return "Assistant"


def _ts(m: "MessageItem") -> str:
    return f" ({m.timestamp})" if m.timestamp else ""


def _minify_message(text: str, snippet_len: int = 120) -> str:
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
    snippet = text[:snippet_len].rstrip()
    if len(text) > snippet_len:
        snippet += "…"
    parts = [xml_escape(snippet)]
    # The rolling-compaction builder appends an independently bounded exact
    # observation fragment. Its scope can contain more than _RECENT_FULL
    # messages, so ordinary minification would otherwise cut the very facts
    # the compactor must summarize. This marker is never added to normal live
    # history, and its aggregate budget is enforced before this renderer.
    marker_index = text.find(_COMPACTION_OBSERVATION_MARKER)
    if marker_index >= 0:
        observation_fragment = text[marker_index:].strip()
        if observation_fragment not in snippet:
            parts.append(xml_escape(observation_fragment))
    if tool_part and tool_part not in snippet:
        parts.append(xml_escape(tool_part))
    for id_str in ids:
        if id_str not in snippet:
            parts.append(xml_escape(id_str))
    return " | ".join(parts)

