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
    """Clarify tool — pause and ask the user a question.

    The user-facing text lives in ``question`` (required tool input). Emits
    ``final_answer`` in the observation so the existing terminal-tool path in
    ``agent_v2`` routes it into the block's content and the completion message.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="clarify",
            description=(
                "Pause and ask the user a clarifying question. "
                "Put the full user-facing message in `question` (markdown OK). "
                "The agent loop stops after this tool runs and waits for the user's reply."
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
                        "question": (
                            "Which definition of \"active user\" should I use?\n"
                            "- logged in within the last 30 days\n"
                            "- performed any tracked action within the last 30 days\n"
                            "- has an active subscription\n"
                            "- or specify your own."
                        ),
                        "context": "user asked about active users; not defined in instructions",
                    },
                    "description": "ask the user to pick a definition before computing",
                },
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
            payload={"question": data.question, "context": data.context},
        )

        summary = "Awaiting user clarification"
        if data.context:
            summary = f"Awaiting user clarification: {data.context}"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"status": "awaiting_response"},
                "observation": {
                    "summary": summary,
                    "artifacts": [],
                    "analysis_complete": True,
                    # final_answer carries the question text → agent_v2's
                    # terminal-tool branch updates the completion message
                    # and re-upserts the block with this as its content.
                    "final_answer": data.question,
                },
            }
        )
