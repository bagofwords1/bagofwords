from typing import Any, Dict, Optional
from pydantic import BaseModel


class ToolInvocation(BaseModel):
    name: str
    input: Dict[str, Any]
    context: Dict[str, Any] = {}


class ToolResult(BaseModel):
    ok: bool = True
    output: Dict[str, Any] = {}
    observation: Optional[Dict[str, Any]] = None


# Tool-specific schemas (mock, scalable)

from app.ai.schemas.tools.answer_question import AnswerQuestionInput, AnswerQuestionOutput
from app.ai.schemas.tools.create_widget import CreateWidgetInput, CreateWidgetOutput

