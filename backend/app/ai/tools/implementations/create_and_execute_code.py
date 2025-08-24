import asyncio
from typing import AsyncIterator, Dict, Any, Type, Optional
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    CreateAndExecuteCodeInput, CreateAndExecuteCodeOutput,
    ToolEvent, ToolStartEvent, ToolProgressEvent, ToolStdoutEvent, ToolEndEvent,
)
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import CodeExecutionManager


class CreateAndExecuteCodeTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_and_execute_code",
            description="Generate code from the current step's data model and execute it, returning formatted results. The tool has a dependency on the create_data_model tool.",
            category="action",
            version="1.0.0",
            input_schema=CreateAndExecuteCodeInput.model_json_schema(),
            output_schema=CreateAndExecuteCodeOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=120,
            idempotent=False,
            required_permissions=[],
            tags=["code", "execution", "data-model"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateAndExecuteCodeInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateAndExecuteCodeOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateAndExecuteCodeInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={})

        # Pull state from runtime context
        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        head_completion = runtime_ctx.get("head_completion")
        organization_settings = runtime_ctx.get("settings")
        current_step = runtime_ctx.get("current_step")
        code_context_builder = runtime_ctx.get("code_context_builder") if runtime_ctx else None

        if not current_step or not getattr(current_step, "data_model", None):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "code": "",
                        "data_preview": {},
                        "stats": {},
                        "execution_log": None,
                    },
                    "observation": {
                        "summary": "No current step or data_model found to generate code",
                        "error": {"type": "missing_state", "message": "current_step.data_model is required"},
                    },
                },
            )
            return

        # 1) Generate code from data model
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_code"})
        coder = Coder(model=runtime_ctx.get("model"), organization_settings=organization_settings, instruction_context_builder=runtime_ctx.get("instruction_context_builder"))
        
        # Get context data for code generation
        context_view = runtime_ctx.get("context_view")
        schemas = getattr(context_view, "schemas_excerpt", "") if context_view else ""
        memories_context = getattr(context_view, "memories_context", "") if context_view else ""
        messages_context = getattr(context_view, "messages_context", "") if context_view else ""
        
        code = await coder.data_model_to_code(
            data_model=current_step.data_model,
            prompt=data.prompt,
            schemas=schemas,
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_and_error_messages=[],
            memories=memories_context,
            previous_messages=messages_context,
            retries=0,
            prev_data_model_code_pair=None,
            sigkill_event=None,
            code_context_builder=code_context_builder,
        )
        
        # Optional validation hook (mirrors Agent v1 behavior)
        validation_result: Optional[Dict[str, Any]] = None
        if organization_settings and organization_settings.get_config("validator").value:
            try:
                validation_result = await coder.validate_code(code, current_step.data_model)
            except Exception:
                validation_result = {"valid": True}

        # 2) Execute code
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "executing_code"})
        mgr = CodeExecutionManager(
            logger=None,
            project_manager=runtime_ctx.get("project_manager"),
            db=db,
            report=report,
            head_completion=head_completion,
            widget=runtime_ctx.get("current_widget"),
            step=current_step,
            organization_settings=organization_settings,
        )

        # Use the manager to execute provided code directly
        try:
            # Get clients and files from runtime context (mirror agent.py pattern)
            clients = runtime_ctx.get("ds_clients", {})
            files = runtime_ctx.get("excel_files", [])
            
            yield ToolStdoutEvent(type="tool.stdout", payload=f"Generated code:\n{code}")
            yield ToolStdoutEvent(type="tool.stdout", payload=f"Available clients: {list(clients.keys())}")
            yield ToolStdoutEvent(type="tool.stdout", payload=f"Available files: {len(files)} files")
            df, output_log = mgr._execute_code(code, clients, files)
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            yield ToolStdoutEvent(type="tool.stdout", payload=f"Execution error: {str(e)}")
            yield ToolStdoutEvent(type="tool.stdout", payload=f"Full traceback:\n{error_details}")
            
            # Get step_id from runtime context for error tracking too
            current_step_id = runtime_ctx.get("current_step_id")
            error_observation = {
                "summary": "Code execution failed",
                "error": {"type": "runtime_error", "message": str(e), "traceback": error_details},
            }
            if current_step_id:
                error_observation["step_id"] = current_step_id
                
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "code": code,
                        "data_preview": {},
                        "stats": {},
                        "execution_log": None,
                    },
                    "observation": error_observation,
                },
            )
            return

        # Format full data for widget using CodeExecutionManager
        widget_data = mgr.format_df_for_widget(df)
        info = widget_data.get("info", {})
        
        # Privacy check for observation and preview
        allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value if organization_settings else True
        
        if allow_llm_see_data:
            # Include data sample (first 5 rows) 
            data_preview = {
                "columns": widget_data.get("columns", []),
                "rows": widget_data.get("rows", [])[:5],
            }
        else:
            # Include only metadata (privacy-safe)
            data_preview = {
                "columns": [{"field": col["field"]} for col in widget_data.get("columns", [])],
                "row_count": len(widget_data.get("rows", [])),
                "stats": info,
            }

        # Get step_id from runtime context for proper tracking
        current_step_id = runtime_ctx.get("current_step_id")
        
        observation = {
            "summary": f"Generated and executed code successfully. {len(widget_data.get('rows', []))} rows returned.",
            "data_preview": data_preview,  # Privacy-safe for LLM
            "stats": info,
        }
        
        # Include step_id if available for tracking
        if current_step_id:
            observation["step_id"] = current_step_id

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "code": code,
                    "widget_data": widget_data,  # Full data for AgentV2 to persist
                    "data_preview": data_preview,  # Privacy-safe preview for UI
                    "stats": info,
                    "execution_log": output_log,
                },
                "observation": observation,
            },
        )


