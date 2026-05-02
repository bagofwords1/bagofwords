from pydantic import BaseModel
from datetime import datetime
from app.schemas.file_tag_schema import FileTagSchema
from app.schemas.sheet_schema_schema_ import SheetSchema

class FileBase(BaseModel):
    pass

class FileCreate(FileBase):
    pass

class FileSchema(FileBase):
    id: str
    filename: str
    content_type: str
    path: str
    created_at: datetime

    class Config:
        from_attributes = True


class FileSchemaWithCompletionId(FileSchema):
    """File schema that includes completion_id from the report_file_association."""
    completion_id: str | None = None
    # True when the file is attached to the report only because it was
    # auto-snapshotted from one of the report's data sources. The chat
    # prompt box uses this to hide inherited files from per-turn chips.
    from_data_source: bool = False


class FileSchemaWithMetadata(FileSchema):
    schemas: list[SheetSchema]
    tags: list[FileTagSchema]
