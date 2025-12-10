from typing import ClassVar, List, Optional, Any, Dict
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
    tag_name: ClassVar[str] = "entities"

    items: List[EntityItem] = []
    allow_llm_see_data: bool = True  # Controls sample vs profile rendering

    def _build_profile(self, data: Any) -> Dict[str, Any]:
        """Build a data profile from entity data (stats only, no sample rows)."""
        if not data or not isinstance(data, dict):
            return {"row_count": 0, "column_count": 0, "columns": []}

        info = data.get("info", {}) if isinstance(data, dict) else {}
        column_info = info.get("column_info") or {}
        rows = data.get("rows", [])
        columns = data.get("columns", [])

        cols: List[Dict[str, Any]] = []
        for name, meta in (column_info.items() if isinstance(column_info, dict) else []):
            cols.append({
                "name": name,
                "dtype": meta.get("dtype"),
                "unique_count": meta.get("unique_count"),
                "null_count": meta.get("null_count"),
            })

        # If column_info is empty but we have columns list, build from that
        if not cols and columns:
            for col in columns:
                col_name = col.get("field") if isinstance(col, dict) else str(col)
                cols.append({"name": col_name, "dtype": None})

        return {
            "row_count": info.get("total_rows") or len(rows),
            "column_count": info.get("total_columns") or len(cols),
            "columns": cols,
        }

    def _render_sample(self, data: Any, max_rows: int = 5) -> str:
        """Render sample rows from entity data."""
        if not data or not isinstance(data, dict):
            return ""
        rows = data.get("rows", [])
        if not rows:
            return ""
        sample_rows = rows[:max_rows]
        try:
            sample_json = json.dumps(sample_rows, ensure_ascii=False)
        except Exception:
            sample_json = str(sample_rows)
        return xml_tag("sample", xml_escape(sample_json[:3000]), {"rows": str(len(sample_rows))})

    def _render_profile(self, data: Any) -> str:
        """Render data profile (stats only, no sample rows)."""
        profile = self._build_profile(data)
        if profile.get("row_count", 0) == 0 and profile.get("column_count", 0) == 0:
            return ""
        
        cols_xml: List[str] = []
        for col in profile.get("columns", [])[:20]:  # Cap columns to avoid bloat
            attrs = {"name": col.get("name", "")}
            if col.get("dtype"):
                attrs["dtype"] = str(col["dtype"])
            if col.get("unique_count") is not None:
                attrs["unique"] = str(col["unique_count"])
            if col.get("null_count") is not None:
                attrs["nulls"] = str(col["null_count"])
            attrs_str = "".join(f' {k}="{xml_escape(str(v))}"' for k, v in attrs.items())
            cols_xml.append(f"<col{attrs_str}/>")
        
        inner = "\n".join(cols_xml)
        return xml_tag(
            "profile",
            inner,
            {"row_count": str(profile.get("row_count", 0)), "column_count": str(profile.get("column_count", 0))},
        )

    def render(self) -> str:
        if not self.items:
            return xml_tag(self.tag_name, "No entities matched")
        
        parts: List[str] = []
        for ent in self.items:
            # Full description for matched entities
            desc = (ent.description or "")[:300]
            attrs = {
                "id": ent.id,
                "type": ent.type,
                "title": ent.title,
                "ds": ",".join(ent.ds_names or []),
            }
            
            inner_segments: List[str] = []
            
            # Description
            if desc:
                inner_segments.append(xml_tag("description", xml_escape(desc)))
            
            # Note: Code is NOT included in prompt - use describe_entity tool to get code
            
            # Sample or Profile based on settings
            if ent.data is not None:
                if self.allow_llm_see_data:
                    sample_xml = self._render_sample(ent.data)
                    if sample_xml:
                        inner_segments.append(sample_xml)
                else:
                    profile_xml = self._render_profile(ent.data)
                    if profile_xml:
                        inner_segments.append(profile_xml)

            parts.append(
                xml_tag(
                    "entity",
                    "\n".join(inner_segments),
                    attrs,
                )
            )
        
        return xml_tag(self.tag_name, "\n".join(parts), {"count": str(len(self.items))})


