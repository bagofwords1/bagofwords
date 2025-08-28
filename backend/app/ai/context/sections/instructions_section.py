from typing import ClassVar, List, Optional
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class InstructionItem(BaseModel):
    id: str
    category: Optional[str] = None
    text: str


class InstructionsSection(ContextSection):
    tag_name: ClassVar[str] = "instructions"

    items: List[InstructionItem] = []

    def render(self) -> str:
        if not self.items:
            return ""
        parts: List[str] = []
        for inst in self.items:
            parts.append(
                xml_tag(
                    "instruction",
                    xml_escape(inst.text.strip()),
                    {"id": inst.id, "category": inst.category or ""},
                )
            )
        return xml_tag(self.tag_name, "\n".join(parts))


