import asyncio
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import CreateWidgetInput, CreateWidgetOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent, 
    ToolProgressEvent,
    ToolPartialEvent,
    ToolStdoutEvent,
    ToolEndEvent,
)


class CreateWidgetTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_widget",
            description="Create a data visualization widget from high-level goals and derive the underlying data model",
            category="action",  # Action-only tool - modifies system state
            version="1.0.0",
            input_schema=CreateWidgetInput.model_json_schema(),
            output_schema=CreateWidgetOutput.model_json_schema(),
            max_retries=2,
            timeout_seconds=60,  # Longer timeout for complex operations
            idempotent=False,  # Creates new resources
            required_permissions=["widget:create"],
            tags=["widget", "visualization", "data-model", "creation"],
            examples=[
                {
                    "input": {
                        "widget_title": "Revenue Dashboard", 
                        "goal": {"type": "dashboard", "metrics": ["revenue", "growth"]}
                    },
                    "description": "Create a revenue tracking dashboard"
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateWidgetInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateWidgetOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateWidgetInput(**tool_input)
        
        yield ToolStartEvent(
            type="tool.start", 
            payload={"widget_title": data.widget_title}
        )

        # synthesize data model (mock)
        await asyncio.sleep(0)
        yield ToolProgressEvent(
            type="tool.progress", 
            payload={"stage": "synthesize_data_model"}
        )
        
        await asyncio.sleep(0)
        yield ToolPartialEvent(
            type="tool.partial", 
            payload={"draft_data_model": {"type": (data.preferences or {}).get("chart_type", "table")}}
        )

        # validate (mock)
        await asyncio.sleep(0)
        yield ToolProgressEvent(
            type="tool.progress", 
            payload={"stage": "validate_data_model"}
        )

        # execute (mock)
        await asyncio.sleep(0)
        yield ToolStdoutEvent(
            type="tool.stdout", 
            payload="Generating and executing code..."
        )
        
        await asyncio.sleep(0)
        yield ToolPartialEvent(
            type="tool.partial", 
            payload={"data_preview": {"columns": ["month", "total_revenue"], "rows_sample": [["2023-01", 12345.67]]}}
        )

        # end
        await asyncio.sleep(0)
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"widget_id": None, "step_id": None, "stats": {"total_rows": 1, "total_columns": 2}},
                "observation": {"summary": f"Created {data.widget_title}", "artifacts": []},
            }
        )