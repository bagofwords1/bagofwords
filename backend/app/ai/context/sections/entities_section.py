from typing import ClassVar, List, Optional, Any
import json
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class EntityItem(BaseModel):
    id: str
    type: str  # 'model' | 'metric' (or other)
    title: str
    description: Optional[str] = None
    ds_names: Optional[List[str]] = None
    code: Optional[str] = None
    data: Optional[Any] = None
    data_model: Optional[Any] = None


class EntitiesSection(ContextSection):
    tag_name: ClassVar[str] = "catalog_entities"

    items: List[EntityItem] = []

    def render(self) -> str:
        if not self.items:
            return ""
        parts: List[str] = []
        for ent in self.items:
            # Truncate description to keep prompt compact
            desc = (ent.description or "")[:160]
            attrs = {
                "id": ent.id,
                "type": ent.type,
                "ds": ",".join(ent.ds_names or []),
            }
            # Inner body: title+desc plus optional rich blocks (code, data_model, data)
            inner_segments: List[str] = []
            inner_segments.append(xml_escape(f"{ent.title} — {desc}".strip()))
            # Code (trim to keep token cost bounded)
            if ent.code:
                inner_segments.append(
                    xml_tag("code", xml_escape(str(ent.code)[:2000]))
                )
            # Data model (JSON, trimmed)
            if ent.data_model is not None:
                try:
                    dm = json.dumps(ent.data_model, ensure_ascii=False)
                except Exception:
                    dm = str(ent.data_model)
                inner_segments.append(
                    xml_tag("data_model", xml_escape(dm[:2000]))
                )
            # Data (JSON, trimmed)
            if ent.data is not None:
                try:
                    data_json = json.dumps(ent.data, ensure_ascii=False)
                except Exception:
                    data_json = str(ent.data)
                inner_segments.append(
                    xml_tag("data", xml_escape(data_json[:2000]))
                )

            parts.append(
                xml_tag(
                    "entity",
                    "\n".join(inner_segments),
                    attrs,
                )
            )
        return xml_tag(self.tag_name, "\n".join(parts))


