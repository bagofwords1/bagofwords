import asyncio
from typing import AsyncIterator, Dict, Any, Type, Optional
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import AnswerQuestionInput, AnswerQuestionOutput
from app.ai.tools.schemas.events import ToolEvent, ToolStartEvent, ToolPartialEvent, ToolEndEvent


class AnswerQuestionTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="answer_question",
            description="Answer questions directly from available context with concise responses",
            category="both",  # Can be used in both research and action modes
            version="1.0.0",
            input_schema=AnswerQuestionInput.model_json_schema(),
            output_schema=AnswerQuestionOutput.model_json_schema(),
            max_retries=1,  # Simple question answering doesn't need many retries
            timeout_seconds=15,
            idempotent=True,  # Safe to retry
            tags=["question", "context", "research"],
            examples=[
                {
                    "input": {"question": "What is the main purpose of this system?"},
                    "description": "General question about system purpose"
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return AnswerQuestionInput

    @property  
    def output_model(self) -> Type[BaseModel]:
        return AnswerQuestionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        # Validate input via schema (lightweight)
        data = AnswerQuestionInput(**tool_input)
        
        yield ToolStartEvent(
            type="tool.start", 
            payload={"question": data.question}
        )

        # In a real impl, call LLM or use context. Here, mock with a brief delay.
        await asyncio.sleep(0)
        yield ToolPartialEvent(
            type="tool.partial", 
            payload={"draft": f"Answering: {data.question}"}
        )

        await asyncio.sleep(0)
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"answer": "This is a placeholder answer.", "citations": []},
                "observation": {"summary": "Answered user question.", "artifacts": []},
            }
        )