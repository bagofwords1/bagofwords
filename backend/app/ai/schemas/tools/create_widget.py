from typing import Optional, Dict, Any
from pydantic import BaseModel


class CreateWidgetInput(BaseModel):
    widget_title: Optional[str] = None
    goal: Dict[str, Any]
    preferences: Optional[Dict[str, Any]] = None
    constraints: Optional[Dict[str, Any]] = None


class CreateWidgetOutput(BaseModel):
    widget_id: Optional[str] = None
    step_id: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None

