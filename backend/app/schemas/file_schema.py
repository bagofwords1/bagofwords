from pydantic import BaseModel, Field
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
    # "upload" | "connector" | "artifact" — see File.source_kind.
    source_kind: str | None = None

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
    # False when a dashboard owns the file, so a delete would be refused. The
    # listing holds only the user's own files; the delete endpoints check
    # again — this only drives the UI.
    can_delete: bool = False


class FilesDeleteRequest(BaseModel):
    file_ids: list[str] = Field(..., min_length=1, max_length=2000)


class FileDeleteFailure(BaseModel):
    id: str
    error_code: str
    reason: str


class FilesDeleteResult(BaseModel):
    deleted: list[str]
    failed: list[FileDeleteFailure]
