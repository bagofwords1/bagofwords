from typing import ClassVar, List, Optional
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class StepItem(BaseModel):
    id: str
    title: str
    slug: Optional[str] = None
    row_count: Optional[int] = None
    columns: Optional[List[str]] = None


class StepsSection(ContextSection):
    """Lists the report's loadable steps for the coder prompt.

    Advertises what `load_step(id_or_name)` can pull in. Resolution is not
    limited to this list — any default step in the report is loadable by id
    or name — but listing the common ones helps the model reach for reuse.
    """

    tag_name: ClassVar[str] = "available_steps"

    items: List[StepItem] = []

    def render(self) -> str:
        if not self.items:
            return xml_tag(self.tag_name, "No loadable steps in this report")
        parts: List[str] = []
        for s in self.items:
            attrs = {"id": s.id, "title": s.title}
            if s.slug:
                attrs["slug"] = s.slug
            if s.row_count is not None:
                attrs["rows"] = str(s.row_count)
            inner = ""
            if s.columns:
                inner = xml_tag("columns", xml_escape(", ".join(s.columns[:50])))
            parts.append(xml_tag("step", inner, attrs))
        return xml_tag(self.tag_name, "\n".join(parts), {"count": str(len(self.items))})
