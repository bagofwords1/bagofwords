from typing import ClassVar
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class CodeSection(ContextSection):
    tag_name: ClassVar[str] = "code"

    content: str = ""

    def render(self) -> str:
        return xml_tag(self.tag_name, xml_escape(self.content or ""))


