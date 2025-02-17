from pydantic import BaseModel
from typing import List
from .step_schema import StepSchema
from typing import Optional

class WidgetBase(BaseModel):
    x: Optional[int] = None
    y: Optional[int] = None
    height: Optional[int] = None
    width: Optional[int] = None
    status: Optional[str] = None

    class Config:
        from_attributes = True

class WidgetCreate(WidgetBase):
    new_message: str
    messages: Optional[List[dict]] = []

    class Config:
        from_attributes = True

class WidgetUpdate(WidgetBase):
    id: str


class WidgetSchema(WidgetBase):
    id: str
    title: str
    slug: str
    last_step: Optional[StepSchema] = None
    #steps: Optional[List[StepSchema]]

    class Config:
        from_attributes = True
