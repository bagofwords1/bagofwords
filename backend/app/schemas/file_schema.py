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


class FileSchemaWithMetadata(FileSchema):
    schemas: list[SheetSchema]
    tags: list[FileTagSchema]
