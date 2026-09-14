from typing import ClassVar, List, Optional, Dict, Any
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class QueryVisualizationSummary(BaseModel):
    id: str
    title: str = ""
    status: Optional[str] = None
    view: Optional[Dict[str, Any]] = None


class QueryObservation(BaseModel):
    query_id: str
    query_title: str
    default_step_id: Optional[str] = None
    default_step_title: Optional[str] = None
    row_count: int = 0
    column_names: List[str] = []
    data_model: Optional[Dict[str, Any]] = None
    stats: Dict[str, Any] = {}
    data_preview: Optional[str] = None
    visualizations: List[QueryVisualizationSummary] = []
    # Declared ParamSpec dicts. Rendered so the planner can see that a query
    # takes VALUES — and which names — without spending a read_query call.
    # Mirrors how <entities> advertises its params for describe_entity.
    parameters: Optional[List[Dict[str, Any]]] = None


class QueriesSection(ContextSection):
    tag_name: ClassVar[str] = "queries"

    items: List[QueryObservation] = []

    def render(self) -> str:
        parts: List[str] = []
        for it in self.items or []:
            inner: List[str] = []
            if it.default_step_title:
                inner.append(xml_tag("step", xml_escape(it.default_step_title), {"id": it.default_step_id or ""}))
            inner.append(xml_tag("rows", str(it.row_count)))
            if it.column_names:
                inner.append(xml_tag("columns", ", ".join(xml_escape(c) for c in it.column_names)))
            if it.data_model is not None:
                inner.append(xml_tag("data_model", xml_escape(str(it.data_model))))
            if it.stats:
                inner.append(xml_tag("stats", xml_escape(str(it.stats))))
            if it.data_preview:
                inner.append(xml_tag("data_preview", xml_escape(it.data_preview)))
            if it.parameters:
                param_parts: List[str] = []
                for p in it.parameters:
                    if not isinstance(p, dict) or not p.get("name"):
                        continue
                    attrs = {
                        "name": str(p.get("name")),
                        "type": str(p.get("type") or "string"),
                        "source": str(p.get("source") or "input"),
                    }
                    if p.get("required"):
                        attrs["required"] = "true"
                    if p.get("default") is not None:
                        attrs["default"] = xml_escape(str(p.get("default")))
                    param_parts.append(xml_tag("param", xml_escape(str(p.get("label") or "")), attrs))
                if param_parts:
                    inner.append(xml_tag("parameters", "\n".join(param_parts)))
            # Visualizations
            if it.visualizations:
                viz_parts: List[str] = []
                for v in it.visualizations:
                    v_inner: List[str] = []
                    if v.status:
                        v_inner.append(xml_tag("status", xml_escape(v.status)))
                    if v.view is not None:
                        v_inner.append(xml_tag("view", xml_escape(str(v.view))))
                    viz_parts.append(xml_tag("visualization", "\n".join(v_inner), {"id": v.id, "title": v.title}))
                inner.append(xml_tag("visualizations", "\n\n".join(viz_parts)))
            parts.append(xml_tag("query", "\n".join(inner), {"id": it.query_id, "title": it.query_title}))
        return xml_tag(self.tag_name, "\n\n".join(parts))


