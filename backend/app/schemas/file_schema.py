from pydantic import BaseModel
from datetime import datetime

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