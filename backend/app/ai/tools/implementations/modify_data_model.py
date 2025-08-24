import asyncio
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ModifyDataModelInput, ModifyDataModelOutput, DataModel,
    ToolEvent, ToolStartEvent, ToolProgressEvent, ToolEndEvent,
)


class ModifyDataModelTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="modify_data_model",
            description="Apply a patch/diff to the current step's data model and return the updated model.",
            category="action",
            version="1.0.0",
            input_schema=ModifyDataModelInput.model_json_schema(),
            output_schema=ModifyDataModelOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["data-model", "modify"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ModifyDataModelInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ModifyDataModelOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ModifyDataModelInput(**tool_input)
        modifications = data.model_dump(exclude_none=True)
        modification_keys = []
        for key in ["data_model", "remove_columns", "add_columns", "transform_columns"]:
            if key in modifications:
                modification_keys.append(key)
        yield ToolStartEvent(type="tool.start", payload={"modifications": modification_keys})

        current_step = runtime_ctx.get("current_step")
        if not current_step or not getattr(current_step, "data_model", None):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"data_model": {}},
                    "observation": {
                        "summary": "No current step or data_model found to modify",
                        "error": {"type": "missing_state", "message": "current_step.data_model is required"},
                    },
                },
            )
            return

        # Apply modifications using Agent v1 logic semantics
        updated = self._apply_data_model_diff(current_step.data_model, data.model_dump(exclude_none=True))

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "updated_data_model"})

        # Let ToolRunner handle output validation generically
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"data_model": updated},
                "observation": {
                    "summary": "Updated data model with modifications",
                    "data_model": updated,
                },
            },
        )

    def _apply_data_model_diff(self, previous_data_model: Dict[str, Any], diff_data_model: Dict[str, Any]) -> Dict[str, Any]:
        # Minimal port of Agent._apply_data_model_diff semantics
        updated_data_model = dict(previous_data_model or {})

        # Handle data_model wrapper if present
        patch = diff_data_model.get("data_model", diff_data_model)

        # Update chart/table type
        if "type" in patch:
            updated_data_model["type"] = patch["type"]

        # Update series
        if "series" in patch:
            updated_data_model["series"] = patch["series"]

        # Handle existing column modifications
        if "add_columns" in patch:
            updated_data_model.setdefault("columns", [])
            for column_to_add in patch["add_columns"]:
                exists = any(col.get("generated_column_name") == column_to_add.get("generated_column_name") for col in updated_data_model["columns"])
                if not exists:
                    updated_data_model["columns"].append(column_to_add)

        if "remove_columns" in patch:
            updated_data_model.setdefault("columns", [])
            to_remove = {c.get("generated_column_name") for c in patch["remove_columns"]}
            updated_data_model["columns"] = [col for col in updated_data_model["columns"] if col.get("generated_column_name") not in to_remove]

        if "transform_columns" in patch:
            updated_data_model.setdefault("columns", [])
            transforms = {c.get("generated_column_name"): c for c in patch["transform_columns"]}
            updated_data_model["columns"] = [transforms.get(col.get("generated_column_name"), col) for col in updated_data_model["columns"]]

        return updated_data_model


