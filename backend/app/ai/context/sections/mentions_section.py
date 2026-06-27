from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from .base import ContextSection, xml_tag, xml_escape


class FileMentionItem(BaseModel):
    id: str
    filename: Optional[str] = None
    content_type: Optional[str] = None
    created_at: Optional[str] = None


class DataSourceMentionItem(BaseModel):
    id: str
    name: Optional[str] = None


class TableMentionItem(BaseModel):
    id: str
    data_source_name: Optional[str] = None
    table_name: Optional[str] = None
    columns_preview: Optional[List[str]] = None


class EntityMentionItem(BaseModel):
    id: str
    title: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None
    columns: Optional[List[str]] = None
    sample_rows: Optional[List[Dict[str, Any]]] = None
    status: Optional[str] = None
    entity_type: Optional[str] = None


class InstructionMentionItem(BaseModel):
    id: str
    title: Optional[str] = None
    kind: Optional[str] = None  # 'instruction' | 'skill'
    text: Optional[str] = None


class MentionsSection(ContextSection):
    tag_name = "mentions"

    files: List[FileMentionItem] = []
    data_sources: List[DataSourceMentionItem] = []
    tables: List[TableMentionItem] = []
    entities: List[EntityMentionItem] = []
    # Explicitly @-mentioned instructions/skills. These are FORCE-INCLUDED into
    # the prompt context (full text), regardless of load_mode / agent scoping —
    # mirroring how a mentioned file is force-included.
    instructions: List[InstructionMentionItem] = []

    def render(self) -> str:
        parts: List[str] = []

        # Files
        if self.files:
            file_tags: List[str] = []
            for f in self.files:
                inner = []
                if f.filename:
                    inner.append(xml_tag("filename", xml_escape(f.filename)))
                if f.content_type:
                    inner.append(xml_tag("content_type", xml_escape(f.content_type)))
                if f.created_at:
                    inner.append(xml_tag("created_at", xml_escape(str(f.created_at))))
                file_tags.append(xml_tag("file", "\n".join(inner), {"id": f.id}))
            parts.append(xml_tag("files", "\n".join(file_tags)))

        # Data sources
        if self.data_sources:
            ds_tags: List[str] = []
            for ds in self.data_sources:
                ds_tags.append(xml_tag("data_source", xml_tag("name", xml_escape(ds.name or "")), {"id": ds.id}))
            parts.append(xml_tag("data_sources", "\n".join(ds_tags)))

        # Tables
        if self.tables:
            tbl_tags: List[str] = []
            for t in self.tables:
                inner = []
                fqn = f"{t.data_source_name}.{t.table_name}" if t.data_source_name else (t.table_name or "")
                inner.append(xml_tag("fqn", xml_escape(fqn)))
                if t.columns_preview:
                    cols = ", ".join(t.columns_preview)
                    inner.append(xml_tag("columns", xml_escape(cols)))
                tbl_tags.append(xml_tag("table", "\n".join(inner), {"id": t.id}))
            parts.append(xml_tag("tables", "\n".join(tbl_tags)))

        # Entities
        if self.entities:
            ent_tags: List[str] = []
            for e in self.entities:
                inner = []
                if e.title:
                    inner.append(xml_tag("title", xml_escape(e.title)))
                if e.description:
                    # Trim description a bit to keep context compact
                    desc_short = (e.description or "")[:300]
                    inner.append(xml_tag("description", xml_escape(desc_short)))
                if e.code:
                    code_short = (e.code or "")[:800]
                    inner.append(xml_tag("code", xml_escape(code_short)))
                if e.columns:
                    inner.append(xml_tag("columns", xml_escape(", ".join(e.columns))))
                if e.sample_rows:
                    row_tags: List[str] = []
                    for row in (e.sample_rows or [])[:2]:
                        pairs = []
                        for k, v in list(row.items())[:6]:
                            s = str(v)
                            s = s[:100] + ("..." if len(s) > 100 else "")
                            pairs.append(f"{k}={s}")
                        row_tags.append(xml_tag("row", xml_escape(", ".join(pairs))))
                    inner.append(xml_tag("sample_rows", "\n".join(row_tags)))
                # Optional metadata
                if e.entity_type:
                    inner.append(xml_tag("entity_type", xml_escape(e.entity_type)))
                if e.status:
                    inner.append(xml_tag("status", xml_escape(e.status)))
                ent_tags.append(xml_tag("entity", "\n".join(inner), {"id": e.id}))
            parts.append(xml_tag("entities", "\n".join(ent_tags)))

        # Instructions / skills — force-included full content. Rendered under an
        # explicit header so the model treats them as active instructions for
        # this turn (the user explicitly @-mentioned them).
        if self.instructions:
            ins_tags: List[str] = []
            for ins in self.instructions:
                inner = []
                if ins.title:
                    inner.append(xml_tag("title", xml_escape(ins.title)))
                if ins.kind:
                    inner.append(xml_tag("kind", xml_escape(ins.kind)))
                if ins.text:
                    inner.append(xml_tag("content", xml_escape(ins.text)))
                attrs = {"id": ins.id}
                if ins.kind:
                    attrs["kind"] = ins.kind
                ins_tags.append(xml_tag("instruction", "\n".join(inner), attrs))
            body = "=== MENTIONED INSTRUCTIONS ===\n" + "\n".join(ins_tags)
            parts.append(xml_tag("instructions", body))

        if not parts:
            return xml_tag(self.tag_name, "No mentions for this turn")
        return xml_tag(self.tag_name, "\n\n".join(parts))


