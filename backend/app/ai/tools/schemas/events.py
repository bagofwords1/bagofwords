from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel


class ToolStartEvent(BaseModel):
    type: Literal["tool.start"]
    payload: Dict[str, Any] = {}


class ToolProgressEvent(BaseModel):
    type: Literal["tool.progress"]
    payload: Dict[str, Any] = {}


class ToolPartialEvent(BaseModel):
    type: Literal["tool.partial"]
    payload: Dict[str, Any] = {}


class ToolStdoutEvent(BaseModel):
    type: Literal["tool.stdout"]
    payload: Union[str, Dict[str, Any]]


class ToolEndEvent(BaseModel):
    type: Literal["tool.end"]
    payload: Dict[str, Any] = {}


class ToolErrorEvent(BaseModel):
    type: Literal["tool.error"]
    payload: Dict[str, Any] = {}


# Union type for all tool events
ToolEvent = Union[
    ToolStartEvent,
    ToolProgressEvent,
    ToolPartialEvent,
    ToolStdoutEvent,
    ToolEndEvent,
    ToolErrorEvent,
]