from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.ai.context.sections.files_schema_section import FilesSchemaContext
from app.models.organization import Organization
from app.models.report import Report


class FilesContextBuilder:
    def __init__(self, db: AsyncSession, organization: Organization, report: Report):
        self.db = db
        self.organization = organization
        self.report = report

    async def build(self) -> FilesSchemaContext:
        files = getattr(self.report, 'files', []) or []
        items: List[FilesSchemaContext.FileItem] = []
        for f in files:
            try:
                prompt_schema = f.prompt_schema()
            except Exception:
                prompt_schema = None
            items.append(
                FilesSchemaContext.FileItem(
                    id=str(getattr(f, 'id', None)) if getattr(f, 'id', None) else None,
                    filename=getattr(f, 'filename', getattr(f, 'name', 'unknown')),
                    path=getattr(f, 'path', None),
                    content_type=getattr(f, 'content_type', None),
                    prompt_schema=prompt_schema,
                )
            )
        return FilesSchemaContext(files=items)


