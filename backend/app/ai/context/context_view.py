"""
ContextView - Read-only consumer-facing view over the current context snapshot.

Lightweight wrapper that groups static vs warm sections for planner/tools.
"""
from pydantic import BaseModel


class StaticSections(BaseModel):
    schemas: str = ""
    instructions: str = ""
    resources: str = ""
    code: str = ""


class WarmSections(BaseModel):
    messages: str = ""
    observations: str = ""
    widgets: str = ""


class ContextView(BaseModel):
    static: StaticSections
    warm: WarmSections
    meta: dict = {}

