import asyncio
from typing import AsyncIterator, Dict, Any, Type, List

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    CreateDashboardInput,
    CreateDashboardOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.agents.dashboard_designer.dashboard_designer import DashboardDesigner


class CreateDashboardTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_dashboard",
            description="Design a narrative dashboard/report layout from available widgets and context. Should always be the last tool called in the chain (after all data analysis is complete).",
            category="action",
            version="1.0.0",
            input_schema=CreateDashboardInput.model_json_schema(),
            output_schema=CreateDashboardOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=True,
            required_permissions=[],
            tags=["dashboard", "report", "layout"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateDashboardInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDashboardOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateDashboardInput(**tool_input)

        # Start event
        yield ToolStartEvent(type="tool.start", payload={"report_title": data.report_title or ""})
        # Early progress to guarantee first emission
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Pull runtime context (new context implementation)
        model = runtime_ctx.get("model")
        instruction_context_builder = runtime_ctx.get("instruction_context_builder")
        all_widgets: List[Any] = runtime_ctx.get("widgets") or []
        steps = runtime_ctx.get("steps")
        previous_messages = data.previous_messages or runtime_ctx.get("previous_messages") or ""

        # Filter widgets if requested
        if data.use_all_widgets or not data.widget_ids:
            widgets = all_widgets
        else:
            allowed_ids = set(data.widget_ids)
            widgets = [w for w in all_widgets if str(getattr(w, "id", "")) in allowed_ids]
       
        # Mark all widgets as published
        db = runtime_ctx.get("db")
        for w in widgets:
            w.status = "published"
            db.add(w)
        await db.commit()

        designer = DashboardDesigner(model, instruction_context_builder)

        final_layout = {"prefix": "", "blocks": [], "end_message": ""}

        # Stream updates from designer and map to tool.progress events
        async for update in designer.execute(
            prompt=data.prompt,
            widgets=widgets,
            steps=steps,
            previous_messages=previous_messages,
        ):
            if not isinstance(update, dict):
                continue
            if "prefix" in update and update["prefix"] is not None and update["prefix"] != final_layout["prefix"]:
                final_layout["prefix"] = update["prefix"]
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "layout_prefix", "prefix": final_layout["prefix"]})

            if "blocks" in update and isinstance(update["blocks"], list) and update["blocks"] != final_layout["blocks"]:
                final_layout["blocks"] = update["blocks"]
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "layout_blocks_update", "blocks": final_layout["blocks"]})

            if "end_message" in update and update["end_message"] is not None and update["end_message"] != final_layout["end_message"]:
                final_layout["end_message"] = update["end_message"]
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "layout_end_message", "end_message": final_layout["end_message"]})

        # Final result
        output = CreateDashboardOutput(layout=final_layout, report_title=data.report_title)
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {
                    "summary": f"Designed dashboard{(' ' + data.report_title) if data.report_title else ''}",
                    "layout": final_layout,
                },
            },
        )


