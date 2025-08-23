import asyncio
from typing import AsyncIterator, Dict, Any

from app.ai.tools.base import Tool
from app.ai.schemas.tools.create_widget import CreateWidgetInput, CreateWidgetOutput


class CreateWidgetTool(Tool):
    name = "create_widget"
    description = "Create a widget from a high-level goal; derives the data model."
    input_model = CreateWidgetInput
    output_model = CreateWidgetOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        data = CreateWidgetInput(**tool_input)
        yield {"type": "tool.start", "payload": {"widget_title": data.widget_title}}

        # synthesize data model (mock)
        await asyncio.sleep(0)
        yield {"type": "tool.progress", "payload": {"stage": "synthesize_data_model"}}
        await asyncio.sleep(0)
        yield {"type": "tool.partial", "payload": {"draft_data_model": {"type": (data.preferences or {}).get("chart_type", "table")}}}

        # validate (mock)
        await asyncio.sleep(0)
        yield {"type": "tool.progress", "payload": {"stage": "validate_data_model"}}

        # execute (mock)
        await asyncio.sleep(0)
        yield {"type": "tool.stdout", "payload": "Generating and executing code..."}
        await asyncio.sleep(0)
        yield {"type": "tool.partial", "payload": {"data_preview": {"columns": ["month", "total_revenue"], "rows_sample": [["2023-01", 12345.67]]}}}

        # end
        await asyncio.sleep(0)
        yield {
            "type": "tool.end",
            "payload": {
                "output": {"widget_id": None, "step_id": None, "stats": {"total_rows": 1, "total_columns": 2}},
                "observation": {"summary": f"Created {data.widget_title}", "artifacts": []},
            },
        }

