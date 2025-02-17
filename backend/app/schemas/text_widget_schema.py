from pydantic import BaseModel
from typing import List
from typing import Optional

class TextWidgetBase(BaseModel):
    x: Optional[int] = None
    y: Optional[int] = None
    height: Optional[int] = None
    width: Optional[int] = None
    status: Optional[str] = None
    content: Optional[str] = None

    class Config:
        from_attributes = True

class TextWidgetCreate(TextWidgetBase):
    content: str

    class Config:
        from_attributes = True

class TextWidgetUpdate(TextWidgetBase):
    pass

class TextWidgetSchema(TextWidgetBase):
    id: str
    content: str

    class Config:
        from_attributes = True
