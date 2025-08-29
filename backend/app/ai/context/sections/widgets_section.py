from typing import ClassVar, List, Optional, Dict, Any
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class WidgetObservation(BaseModel):
    widget_id: str
    widget_title: str
    widget_type: str
    step_id: str
    step_title: str
    row_count: int = 0
    column_names: List[str] = []
    data_model: Optional[Dict[str, Any]] = None
    stats: Dict[str, Any] = {}
    data_preview: Optional[str] = None



class WidgetsSection(ContextSection):
    tag_name: ClassVar[str] = "widgets"

    items: List[WidgetObservation] = []

    def render(self) -> str:
        parts: List[str] = []
        for it in self.items or []:
            inner = []
            inner.append(xml_tag("type", xml_escape(it.widget_type)))
            inner.append(xml_tag("step", xml_escape(it.step_title), {"id": it.step_id}))
            inner.append(xml_tag("rows", str(it.row_count)))
            if it.column_names:
                inner.append(xml_tag("columns", ", ".join(xml_escape(c) for c in it.column_names)))
            if it.data_model is not None:
                inner.append(xml_tag("data_model", xml_escape(str(it.data_model))))
            if it.stats:
                inner.append(xml_tag("stats", xml_escape(str(it.stats))))
            if it.data_preview:
                inner.append(xml_tag("data_preview", xml_escape(it.data_preview)))
            parts.append(xml_tag("widget", "\n".join(inner), {"id": it.widget_id, "title": it.widget_title}))
        return xml_tag(self.tag_name, "\n\n".join(parts))


