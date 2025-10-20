from typing import ClassVar, List, Optional
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


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
        lines: List[str] = []
        for m in self.items:
            who = "User" if m.role == "user" else "Assistant"
            ts = f" ({m.timestamp})" if m.timestamp else ""
            suffix = f" | mentions: {xml_escape(m.mentions)}" if m.mentions else ""
            lines.append(f"{who}{ts}: {xml_escape(m.text.strip())}{suffix}")
        return xml_tag(self.tag_name, "\n".join(lines))


