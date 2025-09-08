from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class ViewSchema(BaseModel):
    component: Optional[str] = None
    variant: Optional[str] = None
    theme: Optional[str] = None
    style: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "allow"


