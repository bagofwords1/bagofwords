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
    """Clarify tool — ask the user one or more questions before proceeding.

    All questions are rendered as an interactive form in the UI (chip-pickers
    for enumerated options, text inputs for free-form answers). The user fills
    in every answer and submits them in a single reply, which re-enters the
    agent loop with the answers as context.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="clarify",
            description=(
                "Ask the user one or more clarifying questions before proceeding. "
                "Each question is rendered as an interactive form row: "
                "a chip-picker when `options` is supplied, a text field otherwise. "
                "Use `options` for enumerable choices; include 'Other…' when the list may not cover every case. "
                "Set `multi_select: true` on a question when several options may apply at once "
                "(select-all-that-apply); leave it false for mutually exclusive choices. "
                "The agent loop pauses until the user submits all answers."
            ),
            category="action",
            version="2.1.0",
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
                        "questions": [
                            {
                                "text": "Which date range should I use?",
                                "options": [
                                    "Last 7 days",
                                    "Last 30 days",
                                    "Last 90 days",
                                    "Year to date",
                                    "Other…",
                                ],
                            },
                            {
                                "text": "Which metric should I focus on?",
                                "options": ["Revenue", "Orders", "Sessions", "Conversion rate", "Other…"],
                            },
                        ],
                        "context": "report scope is ambiguous — need date range and primary KPI before querying",
                    },
                    "description": "ask the user to pick a date range and metric before building a report",
                },
                {
                    "input": {
                        "questions": [
                            {
                                "text": "What should the chart title be?",
                            }
                        ],
                        "context": "user did not specify a title",
                    },
                    "description": "ask a single free-form question",
                },
                {
                    "input": {
                        "questions": [
                            {
                                "text": "Which metrics should the dashboard include?",
                                "options": ["Revenue", "Orders", "Sessions", "Conversion rate", "Other…"],
                                "multi_select": True,
                            }
                        ],
                        "context": "dashboard scope is open-ended — several KPIs may apply",
                    },
                    "description": "select-all-that-apply question via multi_select",
                },
            ],
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
            payload={"questions": [q.model_dump() for q in data.questions], "context": data.context},
        )

        n = len(data.questions)
        summary = f"Awaiting user answers to {n} question{'s' if n != 1 else ''}"
        if data.context:
            summary += f": {data.context}"

        # Build a plain-text representation for the final_answer (shown if the
        # component is not rendered, e.g. in non-UI contexts).
        final_answer = "\n\n".join(
            q.text + (
                "\n" + " / ".join(q.options) if q.options else ""
            )
            for q in data.questions
        )

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"status": "awaiting_response"},
                "observation": {
                    "summary": summary,
                    "artifacts": [],
                    "analysis_complete": True,
                    "final_answer": final_answer,
                },
            },
        )
