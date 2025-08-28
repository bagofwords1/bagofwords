from typing import ClassVar
from app.ai.context.sections.base import ContextSection, xml_tag


class ResourcesSection(ContextSection):
    tag_name: ClassVar[str] = "resources"

    # For now keep as pre-rendered text until we model resources deeply
    content: str = ""

    def render(self) -> str:
        return xml_tag(self.tag_name, self.content or "")


