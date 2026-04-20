import json
import time
from typing import AsyncIterator, Dict, Any, Type, List, Optional
from pydantic import BaseModel

from app.ee.audit.tool_audit import log_tool_audit, _truncate_queries
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.inspect_data import InspectDataInput, InspectDataOutput
from app.ai.tools.schemas import (
    ToolEvent, 
    ToolStartEvent,
    ToolProgressEvent, 
    ToolStdoutEvent, 
    ToolEndEvent
)
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenRequest
from app.ai.prompt_formatters import build_codegen_context
from app.dependencies import async_session_maker

class InspectDataTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="inspect_data",
            description="""
            Purpose:
Quickly examine the structure and sample content of a dataset to validate assumptions and avoid errors before generating insights.

Use when:
	•	You need to confirm column names, formats, data types, or sample values.
	•	You want to check a small amount of data to decide the correct next step.
	•	A previous create_data attempt failed and you need to diagnose the issue.
Don't use on images

            """,
            category="research",
            version="1.0.0",
            input_schema=InspectDataInput.model_json_schema(),
            output_schema=InspectDataOutput.model_json_schema(),
            tags=["data", "debug", "research", "inspection"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return InspectDataInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return InspectDataOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = InspectDataInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")
        
        # Check if LLM is allowed to see data
        allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value if organization_settings else True
        if not allow_llm_see_data:
            await log_tool_audit(
                runtime_ctx,
                action="tool.access_blocked_by_policy",
                resource_type="report",
                resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                details={"tool": "inspect_data", "policy": "allow_llm_see_data"},
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "code": "",
                        "execution_log": "",
                        "error_message": "Data inspection is disabled. The 'Allow LLM to see data' setting is turned off for this organization.",
                        "execution_duration_ms": 0
                    },
                    "observation": {
                        "summary": "inspect_data blocked: allow_llm_see_data is disabled",
                        "details": "The organization setting 'Allow LLM to see data' is turned off.",
                        "code": "",
                        "success": False,
                        "execution_duration_ms": 0
                    }
                }
            )
            return
        
        yield ToolStartEvent(type="tool.start", payload={
            "title": "Inspecting Data",
            "tables_by_source": data.tables_by_source,
        })
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init_inspection"})

        context_hub = runtime_ctx.get("context_hub")

        # 1. Resolve Tables (simplified resolution compared to create_data)
        # We need to know which tables to put in context
        resolved_tables: List[Dict[str, Any]] = []
        if data.tables_by_source and context_hub and getattr(context_hub, "schema_builder", None):
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_tables"})
            # Reusing the static method from CreateDataTool would be ideal if it was shared, 
            # but for now we can import it or implement a lightweight version. 
            # To avoid circular imports or duplication, we rely on the context builder to handle resolution
            # if we pass the raw tables_by_source, BUT build_codegen_context expects resolved tables.
            
            # We'll do a quick resolution pass similar to CreateDataTool
            from app.ai.tools.implementations.create_data import CreateDataTool
            resolved_tables, _ = await CreateDataTool._resolve_active_tables(
                data.tables_by_source,
                context_hub.schema_builder
            )

        # 2. Build Context
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "building_context"})
        
        # Build schemas excerpt for the resolved tables
        schemas_excerpt = ""
        if resolved_tables and context_hub and getattr(context_hub, "schema_builder", None):
            try:
                import re
                all_resolved_names = []
                ds_ids = []
                for group in resolved_tables:
                    if group.get("data_source_id"):
                        ds_ids.append(group["data_source_id"])
                    all_resolved_names.extend(group.get("tables", []))

                ds_scope = list(set(ds_ids)) if ds_ids else None
                name_patterns = [f"(?i)(?:^|\\.){re.escape(n)}$" for n in all_resolved_names] if all_resolved_names else None

                ctx = await context_hub.schema_builder.build(
                    with_stats=True,
                    data_source_ids=ds_scope,
                    name_patterns=name_patterns,
                )
                schemas_excerpt = ctx.render_combined(top_k_per_ds=10, index_limit=0, include_index=False)
            except Exception:
                schemas_excerpt = ""

        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=data.user_prompt,
            interpreted_prompt=data.user_prompt,  # For inspection, use the same prompt
            schemas_excerpt=schemas_excerpt,
            tables_by_source=resolved_tables if resolved_tables else None,
        )

        # 3. Setup Coder and Streamer
        coder = Coder(
            model=runtime_ctx.get("model"),
            organization_settings=organization_settings,
            context_hub=context_hub,
            usage_session_maker=async_session_maker
        )
        
        streamer = StreamingCodeExecutor(
            organization_settings=organization_settings, 
            logger=None, 
            context_hub=context_hub
        )

        # Wrap generate_inspection_code to match the signature expected by streamer
        async def _inspection_generator_fn(**kwargs):
            return await coder.generate_inspection_code(**kwargs)

        # 4. Execute
        
        output_log = ""
        generated_code = ""
        success = False
        execution_error = None
        execution_duration_ms = 0
        executed_queries: List[str] = []
        query_timings: List[dict] = []
        codegen_ms = None
        execution_ms = None
        execution_start = time.monotonic()

        # No retries by default for inspection to keep it fast, unless it crashes hard
        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=1),
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_generator_fn=_inspection_generator_fn,
            sigkill_event=runtime_ctx.get("sigkill_event"),
        ):
            if e["type"] == "stdout":
                yield ToolStdoutEvent(type="tool.stdout", payload=e["payload"])
                # Handle both string and dict payloads from code_execution
                payload = e["payload"]
                if isinstance(payload, str):
                    output_log += payload + "\n"
                else:
                    output_log += (payload.get("message") or "") + "\n"
            elif e["type"] == "progress":
                # Map internal stage names to UI-friendly names
                mapped = dict(e["payload"])
                _stage_map = {
                    "code_generation": "generating_code",
                    "code_generated": "generated_code",
                    "data_query_execution": "executing_code",
                }
                if mapped.get("stage") in _stage_map:
                    mapped["stage"] = _stage_map[mapped["stage"]]
                yield ToolProgressEvent(type="tool.progress", payload=mapped)
            elif e["type"] == "security_violation":
                _vtype = e["payload"].get("violation_type", "unknown")
                _action = "security.unsafe_code_blocked" if _vtype == "unsafe_python" else "security.unsafe_sql_blocked"
                await log_tool_audit(
                    runtime_ctx,
                    action=_action,
                    resource_type="report",
                    resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                    details={
                        "tool": "inspect_data",
                        "violation_type": _vtype,
                        "message": e["payload"].get("message", "")[:300],
                        "code_snippet": e["payload"].get("code_snippet", "")[:300],
                    },
                )
            elif e["type"] == "done":
                execution_duration_ms = int((time.monotonic() - execution_start) * 1000)
                success = True
                # e["payload"] contains 'code', 'execution_log', 'errors', 'df'
                generated_code = e["payload"].get("code") or ""
                executed_queries = e["payload"].get("executed_queries") or []
                query_timings = e["payload"].get("query_timings") or []
                codegen_ms = e["payload"].get("codegen_ms")
                execution_ms = e["payload"].get("execution_ms")
                if e["payload"].get("errors"):
                    success = False
                    execution_error = str(e["payload"]["errors"])
                # We append the full log from payload if our streaming accumulation missed anything
                full_log = e["payload"].get("execution_log")
                if full_log and len(full_log) > len(output_log):
                    output_log = full_log

        # 5. Audit
        _ds_ids = list({g.get("data_source_id") for g in resolved_tables if g.get("data_source_id")})
        _tables = [t for g in resolved_tables for t in g.get("tables", [])]
        if success:
            await log_tool_audit(
                runtime_ctx,
                action="tool.data_queried",
                resource_type="report",
                resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                details={
                    "tool": "inspect_data",
                    "data_source_ids": _ds_ids,
                    "tables_accessed": _tables,
                    "executed_queries": _truncate_queries(executed_queries),
                },
            )
        else:
            await log_tool_audit(
                runtime_ctx,
                action="tool.data_query_failed",
                resource_type="report",
                resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                details={
                    "tool": "inspect_data",
                    "error_type": "execution_failure",
                    "error_message": (execution_error or "")[:300],
                    "data_source_ids": _ds_ids,
                    "tables_requested": _tables,
                },
            )

        # 6. Final Result
        # The value is the LOGS.
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": success,
                    "code": generated_code,
                    "execution_log": output_log,
                    "error_message": execution_error,
                    "execution_duration_ms": execution_duration_ms,
                    "query_timings": query_timings,
                    "codegen_ms": codegen_ms,
                    "execution_ms": execution_ms,
                },
                "observation": {
                    "summary": f"Inspection failed for: {data.user_prompt}" if not success else f"Inspection finished for: {data.user_prompt}",
                    "details": output_log[:3000] if output_log else "No output produced.",
                    "code": generated_code,
                    "success": success,
                    "execution_duration_ms": execution_duration_ms,
                    **({"error": {"type": "execution_failure", "message": execution_error}} if execution_error else {}),
                }
            }
        )

