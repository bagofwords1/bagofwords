from typing import ClassVar, List, Optional
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class FilesSchemaContext(ContextSection):
    tag_name: ClassVar[str] = "files"

    class FileItem(BaseModel):
        id: Optional[str] = None
        filename: str
        path: Optional[str] = None
        content_type: Optional[str] = None
        # A compact, human-readable schema/metadata summary derived from File.prompt_schema()
        prompt_schema: Optional[str] = None
        # Render tier: "full" inlines prompt_schema; "index" renders only the
        # compact index_summary line (content stays behind read_file/inspect_data).
        detail: str = "full"
        # One-line structural summary used by the index tier.
        index_summary: Optional[str] = None
        # Provenance: "upload" (user attached to this report) or "agent"
        # (snapshotted from a data source's file library).
        origin: str = "upload"

    files: List[FileItem] = []

    def render(self) -> str:
        file_nodes: List[str] = []
        has_index_tier = False
        for f in self.files or []:
            attrs = {
                "id": f.id or "",
                "filename": f.filename,
                "path": f.path or "",
                "content_type": f.content_type or "",
            }
            if f.origin and f.origin != "upload":
                attrs["source"] = f.origin
            if f.detail == "index":
                has_index_tier = True
                attrs["detail"] = "index"
                inner = xml_tag("summary", xml_escape(f.index_summary or f.filename))
            else:
                inner_parts: List[str] = []
                if f.prompt_schema:
                    inner_parts.append(xml_tag("schema", xml_escape(f.prompt_schema)))
                inner = "\n".join(inner_parts)
            file_nodes.append(xml_tag("file", inner, attrs))
        body = "\n\n".join(file_nodes)
        if has_index_tier:
            body += (
                "\n" + xml_tag(
                    "note",
                    'Files marked detail="index" show structure only. Before using '
                    "one, read its content on demand: read_file(file_id=...) for "
                    "text/PDF/images, inspect_data for tabular files. Do not guess "
                    "cell values from the index line.",
                )
            )
        return xml_tag(self.tag_name, body)
