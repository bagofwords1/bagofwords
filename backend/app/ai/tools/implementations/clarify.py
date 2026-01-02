from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ClarifyInput, ClarifyOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
)


class ClarifyTool(Tool):
    """Clarify tool - signals that the planner needs user clarification.

    The actual questions are output by the planner in assistant_message.
    This tool just marks analysis_complete=True to stop the loop and wait for user response.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="clarify",
            description=(
                "ACTION: Signal that you need clarification from the user. "
                "Output your questions in assistant_message, then call this tool to pause and wait for response."
            ),
            category="action",
            version="1.0.0",
            input_schema=ClarifyInput.model_json_schema(),
            output_schema=ClarifyOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=10,
            idempotent=True,
            required_permissions=[],
            tags=["clarification", "questions", "user-interaction"],
            examples=[
                {
                    "input": {
                        "context": "User requested revenue analysis but didn't specify time period"
                    },
                    "description": "Signal need for clarification (questions go in assistant_message)"
                },
                {
                    "input": {},
                    "description": "Simple clarification signal with no context"
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
            payload={"context": data.context}
        )

        # Simple observation - just mark that we're waiting for user response
        # The actual questions are in the planner's assistant_message
        summary = "Waiting for user clarification"
        if data.context:
            summary = f"Waiting for user clarification: {data.context}"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "status": "awaiting_response"
                },
                "observation": {
                    "summary": summary,
                    "artifacts": [],
                    "analysis_complete": True,
                    # No final_answer - the planner's assistant_message has the questions
                },
            }
        )
