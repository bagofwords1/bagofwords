import asyncio
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ClarifyInput, ClarifyOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent, 
    ToolProgressEvent,
    ToolEndEvent,
)


class ClarifyTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="clarify",
            description="Ask clarifying questions when user request is ambiguous or needs more specific information. Use also when user is using a metric/measure that is not well defined.",
            category="action",  # Research tool - gathers information
            version="1.0.0",
            input_schema=ClarifyInput.model_json_schema(),
            output_schema=ClarifyOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=10,  # Simple clarification doesn't need long timeout
            idempotent=True,  # Safe to retry
            required_permissions=[],  # No special permissions needed
            tags=["clarification", "questions", "user-interaction", "research"],
            examples=[
                {
                    "input": {
                        "questions": [
                            "What specific time range do you want to analyze?",
                            "Do you need the data grouped by month, quarter, or year?"
                        ],
                        "context": "User requested revenue analysis but didn't specify time period or grouping"
                    },
                    "description": "Ask for clarification on data analysis requirements"
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ClarifyInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ClarifyOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ClarifyInput(**tool_input)
        
        yield ToolStartEvent(
            type="tool.start", 
            payload={"questions_count": len(data.questions)}
        )

        # Brief processing delay
        await asyncio.sleep(0)
        yield ToolProgressEvent(
            type="tool.progress", 
            payload={"stage": "preparing_clarification_questions"}
        )

        # Format questions for user presentation
        await asyncio.sleep(0)
        
        # Create a user-friendly question format
        formatted_questions = []
        for i, question in enumerate(data.questions, 1):
            formatted_questions.append(f"{i}. {question}")

        question_text = "\n".join(formatted_questions)
        
        # Create the final message
        context_prefix = f"\n\n**Context:** {data.context}\n\n" if data.context else "\n\n"
        final_message = f"I need some clarification to better help you:{context_prefix}**Questions:**\n{question_text}\n\nPlease provide more details so I can assist you effectively."

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "questions_asked": data.questions,
                    "status": "awaiting_response"
                },
                "observation": {
                    "summary": f"Asked {len(data.questions)} clarifying questions",
                    "artifacts": [],
                    "analysis_complete": True,
                    "final_answer": final_message
                },
            }
        )
