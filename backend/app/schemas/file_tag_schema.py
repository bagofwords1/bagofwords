from pydantic import BaseModel
from datetime import datetime
class FileTagBase(BaseModel):
    key: str
    value: str

class FileTagCreate(FileTagBase):
    file_id: str

class FileTagSchema(FileTagBase):
    id: str
    file_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True