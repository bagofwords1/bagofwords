import asyncio
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.llm import LLM
from app.ai.tools.schemas import (
    CreateDataModelInput, CreateDataModelOutput, DataModel, DataModelColumn,
    ToolEvent, ToolStartEvent, ToolProgressEvent, ToolEndEvent,
)


class CreateDataModelTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_data_model",
            description="Generate a normalized data model from a high-level goal and available schemas.",
            category="action",
            version="1.0.0",
            input_schema=CreateDataModelInput.model_json_schema(),
            output_schema=CreateDataModelOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=[],
            tags=["data-model", "generation", "widget"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateDataModelInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDataModelOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateDataModelInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"widget_title": data.widget_title})
        # Emit an early progress event to guarantee first emission
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Build a compact prompt using schemas/context  
        llm = LLM(runtime_ctx.get("model"))
        context_view = runtime_ctx.get("context_view")
        schemas_excerpt = getattr(context_view.static, "schemas", "") if context_view else ""
        # Enhanced prompt for streaming JSON similar to planner
        prompt = f"""
You are a data modeling assistant.
Given the user's goal and available schemas, produce a normalized JSON data_model that will be streamed progressively.

<schemas>
{schemas_excerpt}
</schemas>

<user_prompt>
{data.prompt}
</user_prompt>

Return a JSON object with the following structure:
{{
    "type": "table|bar_chart|line_chart|pie_chart|area_chart|count|heatmap|map|candlestick|treemap|radar_chart|scatter_plot",
    "columns": [
        {{
            "generated_column_name": "column_name",
            "source": "table.column",
            "description": "Description ending with period.",
            "source_data_source_id": "extract-from-schema-context"
        }}
    ],
    "series": [
        {{
            "name": "Series Name",
            "key": "key_column",
            "value": "value_column"
        }}
    ]
}}

CRITICAL: 
- Only use columns that exist in the provided schemas
- ALWAYS extract the data source ID from the <data_source_id> tags in the schema context above
- Every column MUST have the same source_data_source_id value from the schema context
- If multiple data sources exist, use the appropriate data_source_id for each column based on which schema it comes from
"""

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_data_model"})

        # Stream the LLM response and parse progressively (similar to planner.py)
        from partialjson.json_parser import JSONParser
        import json
        import re

        parser = JSONParser()
        buffer = ""
        current_data_model = {
            "type": None,
            "columns": [],
            "filters": [],
            "group_by": [],
            "sort": [],
            "limit": 100,
            "series": []
        }
        
        # Emit an early progress event before the LLM call starts
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_call_start"})
        
        async for chunk in llm.inference_stream(prompt):
            buffer += chunk
            
            
            try:
                parsed = parser.parse(buffer)
                if not parsed or not isinstance(parsed, dict):
                    continue

                # Stream type updates
                if "type" in parsed and parsed["type"] != current_data_model["type"]:
                    current_data_model["type"] = parsed["type"]
                    yield ToolProgressEvent(
                        type="tool.progress",
                        payload={
                            "stage": "data_model_type_determined",
                            "data_model_type": current_data_model["type"]
                        }
                    )

                # Stream column updates (similar to planner.py column streaming)
                if "columns" in parsed and isinstance(parsed["columns"], list):
                    for column in parsed["columns"]:
                        if not isinstance(column, dict):
                            continue

                        # Validate column completeness using DataModelColumn schema
                        try:
                            # Try to validate using the actual schema
                            DataModelColumn(**column)
                            is_complete = not any(
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

                # Stream series updates (for charts)
                if "series" in parsed and isinstance(parsed["series"], list):
                    chart_type = current_data_model.get("type")
                    
                    # Define required keys per chart type (similar to planner.py)
                    type_specific_keys = {
                        "bar_chart": ["name", "key", "value"],
                        "line_chart": ["name", "key", "value"], 
                        "pie_chart": ["name", "key", "value"],
                        "area_chart": ["name", "key", "value"],
                        "candlestick": ["name", "key", "open", "close", "low", "high"],
                        "heatmap": ["name", "x", "y", "value"],
                        "scatter_plot": ["name", "x", "y"],
                        "map": ["name", "key", "value"],
                        "treemap": ["name", "id", "parentId", "value"],
                        "radar_chart": ["name", "dimensions"]
                    }

                    required_keys = type_specific_keys.get(chart_type, ["name", "key", "value"])
                    
                    series_complete = all(
                        isinstance(series_item, dict) and
                        all(key in series_item for key in required_keys)
                        for series_item in parsed["series"]
                    )

                    if series_complete and parsed["series"] != current_data_model["series"]:
                        current_data_model["series"] = parsed["series"]
                        yield ToolProgressEvent(
                            type="tool.progress",
                            payload={
                                "stage": "series_configured",
                                "series": current_data_model["series"],
                                "chart_type": chart_type
                            }
                        )

                # Update other fields
                for field in ["filters", "group_by", "sort", "limit"]:
                    if field in parsed:
                        current_data_model[field] = parsed[field]

            except Exception as e:
                # Continue streaming even if parsing fails
                continue

        # Validate final data model using schema before signaling widget creation
        try:
            validated_data_model = DataModel(**current_data_model)
            final_data_model = validated_data_model.model_dump()
        except ValidationError as e:
            # If validation fails, use current_data_model as-is and let ToolRunner handle it
            final_data_model = current_data_model
        
        # Signal widget creation needed
        yield ToolProgressEvent(
            type="tool.progress",
            payload={
                "stage": "widget_creation_needed",
                "widget_title": data.widget_title,
                "data_model": final_data_model
            }
        )

        # Final result
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "data_model": final_data_model,
                    "widget_title": data.widget_title,
                },
                "observation": {
                    "summary": f"Generated data model for '{data.widget_title}'",
                    "data_model": final_data_model,
                    "widget_title": data.widget_title,
                },
            },
        )


