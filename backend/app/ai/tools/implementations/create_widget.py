import asyncio
from typing import AsyncIterator, Dict, Any, Type, Optional
from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    CreateWidgetInput, CreateWidgetOutput,
    CreateDataModelInput, DataModel, DataModelColumn, CreateAndExecuteCodeInput,
    ToolEvent, ToolStartEvent, ToolProgressEvent, ToolStdoutEvent, ToolEndEvent,
)
from app.ai.llm import LLM
from partialjson.json_parser import JSONParser
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor


class CreateWidgetTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_widget",
            description="End-to-end: create data model from prompt, generate code, execute, and return widget data.",
            category="action",
            version="1.0.0",
            input_schema=CreateWidgetInput.model_json_schema(),
            output_schema=CreateWidgetOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=180,
            idempotent=False,
            required_permissions=[],
            tags=["widget", "data-model", "code", "execution"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateWidgetInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateWidgetOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateWidgetInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"widget_title": data.widget_title})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Context
        organization_settings = runtime_ctx.get("settings")
        context_view = runtime_ctx.get("context_view")
        schemas_section = getattr(context_view.static, "schemas", None) if context_view else None
        schemas_excerpt = schemas_section.render() if schemas_section else ""

        # Phase 1: Generate Data Model (streamed parsing like create_data_model)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_data_model"})
        llm = LLM(runtime_ctx.get("model"))
        header = f"""
You are a data modeling assistant.
Given the user's goal and available schemas, produce a normalized JSON data_model that will be streamed progressively.

<schemas>
{schemas_excerpt}
</schemas>

<user_prompt>
{data.user_prompt}
</user_prompt>

<interpreted_prompt>
{data.interpreted_prompt}
</interpreted_prompt>

Return a JSON object with the following structure:
"""
        skeleton = """
{
    "type": "table|bar_chart|line_chart|pie_chart|area_chart|count|heatmap|map|candlestick|treemap|radar_chart|scatter_plot",
    "columns": [
        {
            "generated_column_name": "column_name",
            "source": "table.column",
            "description": "Description ending with period.",
            "source_data_source_id": "extract-from-schema-context"
        }
    ],
    "series": [
        {
            "name": "Series Name",
            "key": "key_column",
            "value": "value_column"
        }
    ],
    "filters": [],
    "group_by": [],
    "sort": [],
    "limit": None
}
"""
        critical = """
CRITICAL:
- Only use columns that exist in the provided schemas
- ALWAYS extract the data source ID from the <data_source_id> tags in the schema context above
- Every column MUST have the same source_data_source_id value from the schema context
- If multiple data sources exist, use the appropriate data_source_id for each column based on which schema it comes from
"""
        prompt = header + "\n" + skeleton + "\n" + critical

        parser = JSONParser()
        buffer = ""
        current_data_model: Dict[str, Any] = {
            "type": None,
            "columns": [],
            "filters": [],
            "group_by": [],
            "sort": [],
            "limit": None,
            "series": []
        }

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_call_start"})
        import re
        import json
        async for chunk in llm.inference_stream(prompt):
            buffer += chunk
            try:
                parsed = parser.parse(buffer)
                if not parsed or not isinstance(parsed, dict):
                    continue

                if "type" in parsed and parsed["type"] != current_data_model["type"]:
                    current_data_model["type"] = parsed["type"]
                    yield ToolProgressEvent(type="tool.progress", payload={"stage": "data_model_type_determined", "data_model_type": parsed["type"]})

                if "columns" in parsed and isinstance(parsed["columns"], list):
                    for column in parsed["columns"]:
                        if not isinstance(column, dict):
                            continue
                        # Validate column completeness using DataModelColumn schema (EXACT like create_data_model)
                        try:
                            DataModelColumn(**column)
                            # Enforce UUID format for source_data_source_id (treat mismatch as incomplete rather than raising)
                            uuid_ok = re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", (column.get("source_data_source_id") or "")) is not None
                            is_complete = uuid_ok and not any(
                                existing['generated_column_name'] == column['generated_column_name']
                                for existing in current_data_model["columns"]
                            )
                        except ValidationError:
                            is_complete = False

                        if is_complete:
                            current_data_model["columns"].append(column)
                            yield ToolProgressEvent(
                                type="tool.progress",
                                payload={
                                    "stage": "column_added",
                                    "column": column,
                                    "total_columns": len(current_data_model["columns"]) 
                                }
                            )

                if "series" in parsed and isinstance(parsed["series"], list) and parsed["series"] != current_data_model["series"]:
                    current_data_model["series"] = parsed["series"]
                    yield ToolProgressEvent(type="tool.progress", payload={"stage": "series_configured", "series": parsed["series"], "chart_type": current_data_model.get("type")})

                for field in ["filters", "group_by", "sort", "limit"]:
                    if field in parsed:
                        if field == "sort" and isinstance(parsed["sort"], list):
                            normalized_sort = []
                            for item in parsed["sort"]:
                                if isinstance(item, dict):
                                    # Map common alias "column" -> required key "field"
                                    if "field" not in item and "column" in item:
                                        item = {**item, "field": item.get("column")}
                                normalized_sort.append(item)
                            current_data_model["sort"] = normalized_sort
                        elif field == "limit":
                            # Keep default 100 if model emitted null/None
                            if parsed["limit"] is not None:
                                current_data_model["limit"] = parsed["limit"]
                        else:
                            current_data_model[field] = parsed[field]
            except Exception as e:
                continue
        # Finalize model (best-effort validation via Pydantic model)
        try:
            dm = DataModel(**current_data_model)
            final_data_model = dm.model_dump()
        except Exception as e:
            final_data_model = current_data_model
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "widget_creation_needed", "widget_title": data.widget_title, "data_model": final_data_model})

        # Phase 2/3: Code generation and execution with internal retries
        # Resolve builders from context_hub when available
        context_hub = runtime_ctx.get("context_hub")
        instruction_builder = runtime_ctx.get("instruction_context_builder") or (getattr(context_hub, "instruction_builder", None) if context_hub else None)
        code_context_builder = runtime_ctx.get("code_context_builder") or (getattr(context_hub, "code_builder", None) if context_hub else None)

        coder = Coder(model=runtime_ctx.get("model"), organization_settings=organization_settings, context_hub=context_hub)
        streamer = StreamingCodeExecutor(organization_settings=organization_settings, logger=None, context_hub=context_hub)

        context_view = runtime_ctx.get("context_view")
        schemas_section = getattr(context_view.static, "schemas", None) if context_view else None
        schemas = schemas_section.render() if schemas_section else ""
        messages_section = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = messages_section.render() if messages_section else ""

        # Stream generation + execution with retries
        # Stream and capture final results
        exec_df = None
        generated_code = None
        code_errors = []
        output_log = ""

        async for e in streamer.generate_and_execute_stream(
            data_model=final_data_model,
            prompt=data.interpreted_prompt or data.user_prompt,
            schemas=schemas,
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_context_builder=code_context_builder,
            code_generator_fn=coder.data_model_to_code,
            validator_fn=None,
            max_retries=2,
            sigkill_event=runtime_ctx.get("sigkill_event"),
        ):
            if e["type"] == "progress":
                yield ToolProgressEvent(type="tool.progress", payload=e["payload"])
            elif e["type"] == "stdout":
                yield ToolStdoutEvent(type="tool.stdout", payload=e["payload"]) 
            elif e["type"] == "done":
                generated_code = e["payload"].get("code")
                code_errors = e["payload"].get("errors") or []
                output_log = e["payload"].get("execution_log") or ""
                exec_df = e["payload"].get("df")

        # Ensure variables exist even if done wasn't reached

        if generated_code is None or exec_df is None:
            # Failure case
            current_step_id = runtime_ctx.get("current_step_id")
            error_observation = {
                "summary": "Create widget failed",
                "error": {"type": "execution_failure", "message": "execution failed"},
            }
            if current_step_id:
                error_observation["step_id"] = current_step_id
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "data_model": final_data_model,
                        "code": generated_code or "",
                        "widget_data": {},
                        "data_preview": {},
                        "stats": {},
                        "execution_log": output_log,
                        "errors": code_errors,
                    },
                    "observation": error_observation,
                },
            )
            return

        # Success path: format widget data and preview (privacy aware)
        widget_data = streamer.format_df_for_widget(exec_df)
        info = widget_data.get("info", {})
        allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value if organization_settings else True
        if allow_llm_see_data:
            data_preview = {
                "columns": widget_data.get("columns", []),
                "rows": widget_data.get("rows", [])[:5],
            }
        else:
            data_preview = {
                "columns": [{"field": c.get("field")} for c in widget_data.get("columns", [])],
                "row_count": len(widget_data.get("rows", [])),
                "stats": info,
            }

        current_step_id = runtime_ctx.get("current_step_id")
        observation = {
            "summary": f"Created widget '{data.widget_title}' successfully.",
            "data_model": final_data_model,
            "data_preview": data_preview,
            "stats": info,
        }
        if current_step_id:
            observation["step_id"] = current_step_id

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "data_model": final_data_model,
                    "code": generated_code,
                    "widget_data": widget_data,
                    "data_preview": data_preview,
                    "stats": info,
                    "execution_log": output_log,
                    "errors": code_errors,
                },
                "observation": observation,
            },
        )


