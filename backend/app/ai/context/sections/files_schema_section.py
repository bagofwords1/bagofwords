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

    files: List[FileItem] = []

    def render(self) -> str:
        file_nodes: List[str] = []
        for f in self.files or []:
            inner_parts: List[str] = []
            if f.prompt_schema:
                inner_parts.append(xml_tag("schema", xml_escape(f.prompt_schema)))
            inner = "\n".join(inner_parts)
            file_nodes.append(
                xml_tag(
                    "file",
                    inner,
                    {
                        "id": f.id or "",
                        "filename": f.filename,
                        "path": f.path or "",
                        "content_type": f.content_type or "",
                    },
                )
            )
        return xml_tag(self.tag_name, "\n\n".join(file_nodes))


