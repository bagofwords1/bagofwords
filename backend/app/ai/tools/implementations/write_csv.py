import csv
import time
import logging
import uuid
from typing import AsyncIterator, Dict, Any, Type, List
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.write_csv import WriteCsvInput, WriteCsvOutput
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolStdoutEvent,
    ToolEndEvent,
)
from app.ee.audit.tool_audit import log_tool_audit
from app.dependencies import async_session_maker

logger = logging.getLogger(__name__)


class WriteCsvTool(Tool):
    """
    Generate or transform data via Coder-generated Python code and save as CSV.
    Follows the same pattern as InspectDataTool but persists the output
    as a File record instead of returning logs.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="write_csv",
            description="""
            Purpose:
Generate or transform tabular data using custom Python/pandas code,
then save the result as a CSV file that can be loaded by create_data for visualization.

Use when:
    - The user asks to create/generate a table of data (e.g. "create a table of X, Y, Z")
    - You received raw/unstructured data from execute_mcp and need to clean/reshape it
    - You need to parse, filter, extract, merge, or convert data into a tabular format
    - You need to produce a dataset that doesn't exist in any connected data source

Do not use when:
    - Data is already in a clean tabular format (execute_mcp auto-materializes tabular data)
    - You need to query a SQL database (use create_data instead)
            """,
            category="action",
            version="1.0.0",
            input_schema=WriteCsvInput.model_json_schema(),
            output_schema=WriteCsvOutput.model_json_schema(),
            tags=["transform", "csv", "write_csv", "generate", "table"],
            timeout_seconds=120,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WriteCsvInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WriteCsvOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = WriteCsvInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")

        yield ToolStartEvent(type="tool.start", payload={"title": "Writing CSV"})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init_write_csv"})

        context_hub = runtime_ctx.get("context_hub")

        # 1. Resolve tables (same pattern as inspect_data)
        resolved_tables: List[Dict[str, Any]] = []
        if data.tables_by_source and context_hub and getattr(context_hub, "schema_builder", None):
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_tables"})
            from app.ai.tools.implementations.create_data import CreateDataTool
            resolved_tables, _ = await CreateDataTool._resolve_active_tables(
                data.tables_by_source,
                context_hub.schema_builder,
            )

        # 2. Build context
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "building_context"})
        from app.ai.prompt_formatters import build_codegen_context

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

        # Generate output directly in uploads/files
        import os
        output_filename = os.path.join("uploads", "files", f"__write_csv_output_{uuid.uuid4().hex}.csv")

        # Augment the prompt to instruct the coder to save output as CSV
        csv_prompt = (
            f"{data.user_prompt}\n\n"
            "IMPORTANT: The final result must be a pandas DataFrame stored in a variable called `df`. "
            "Print a preview of the first 5 rows with print(df.head()). "
            f"Then save to CSV: df.to_csv('{output_filename}', index=False). "
            "Print the shape: print(f'Shape: {df.shape}')"
        )

        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=csv_prompt,
            interpreted_prompt=csv_prompt,
            schemas_excerpt=schemas_excerpt,
            tables_by_source=resolved_tables if resolved_tables else None,
        )

        # 3. Setup Coder and Executor
        from app.ai.agents.coder.coder import Coder
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        from app.ai.schemas.codegen import CodeGenRequest

        coder = Coder(
            model=runtime_ctx.get("model"),
            organization_settings=organization_settings,
            context_hub=context_hub,
            usage_session_maker=async_session_maker,
        )

        streamer = StreamingCodeExecutor(
            organization_settings=organization_settings,
            logger=None,
            context_hub=context_hub,
        )

        async def _generator_fn(**kwargs):
            return await coder.generate_inspection_code(**kwargs)

        # 4. Execute
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "code_execution"})

        output_log = ""
        generated_code = ""
        success = False
        execution_error = None
        execution_start = time.monotonic()

        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=1),
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_generator_fn=_generator_fn,
            sigkill_event=runtime_ctx.get("sigkill_event"),
        ):
            if e["type"] == "stdout":
                yield ToolStdoutEvent(type="tool.stdout", payload=e["payload"])
                payload = e["payload"]
                if isinstance(payload, str):
                    output_log += payload + "\n"
                else:
                    output_log += (payload.get("message") or "") + "\n"
            elif e["type"] == "progress":
                yield ToolProgressEvent(type="tool.progress", payload=e["payload"])
            elif e["type"] == "done":
                success = True
                generated_code = e["payload"].get("code") or ""
                if e["payload"].get("errors"):
                    success = False
                    execution_error = str(e["payload"]["errors"])
                full_log = e["payload"].get("execution_log")
                if full_log and len(full_log) > len(output_log):
                    output_log = full_log

        execution_duration_ms = int((time.monotonic() - execution_start) * 1000)

        if not success:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error_message": execution_error,
                        "code": generated_code,
                        "execution_log": output_log[:3000],
                    },
                    "observation": {
                        "summary": f"write_csv failed: {execution_error}",
                        "code": generated_code,
                        "success": False,
                    },
                },
            )
            return

        # 5. Find the output CSV and create a File record
        csv_path = output_filename
        if not os.path.exists(csv_path):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error_message": "Code executed but no output CSV was produced.",
                        "code": generated_code,
                        "execution_log": output_log[:3000],
                    },
                    "observation": {
                        "summary": "No output CSV produced",
                        "code": generated_code,
                        "success": False,
                    },
                },
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving_file"})

        import pandas as pd
        from uuid import uuid4
        from app.models.file import File
        from app.services.file_preview import _preview_csv
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        from app.ai.tools.implementations.create_data import build_view_from_data_model, _infer_palette_theme

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")

        # Rename to final name
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "formatting_output"})
        unique_name = f"{uuid4()}_write_csv_output.csv"
        dest_path = os.path.join("uploads", "files", unique_name)
        os.rename(csv_path, dest_path)

        # Read full CSV for widget data
        full_df = pd.read_csv(dest_path)
        total_rows = len(full_df)

        # Format data for visualization (same structure as create_data)
        streamer = StreamingCodeExecutor(
            organization_settings=organization_settings,
            logger=None,
            context_hub=runtime_ctx.get("context_hub"),
        )
        formatted = streamer.format_df_for_widget(full_df)
        info = formatted.get("info", {})
        data_preview = full_df.head(5).to_string() if not full_df.empty else ""

        # Generate preview
        preview = None
        try:
            preview = _preview_csv(dest_path, "write_csv_output.csv")
        except Exception:
            pass

        file = File(
            filename="write_csv_output.csv",
            path=dest_path,
            content_type="text/csv",
            preview=preview,
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
        )
        db.add(file)
        await db.flush()

        # Link to report
        if report:
            from app.models.report_file_association import report_file_association
            from sqlalchemy import insert
            await db.execute(
                insert(report_file_association).values(
                    report_id=str(report.id),
                    file_id=str(file.id),
                )
            )

        await db.flush()

        # Build data_model and view for visualization
        query_title = data.title or "Generated CSV"
        final_dm = {"type": "table", "series": []}
        palette_theme = _infer_palette_theme(runtime_ctx) or "default"
        available_columns = [c.get("field") for c in formatted.get("columns", []) if c.get("field")]
        view_schema = build_view_from_data_model(final_dm, title=query_title, palette_theme=palette_theme, available_columns=available_columns)
        view_payload = view_schema.model_dump(exclude_none=True) if view_schema else {"version": "v2", "view": {"type": "table"}}

        # Emit data_model_type_determined so orchestrator creates Query/Step/Visualization
        yield ToolProgressEvent(
            type="tool.progress",
            payload={
                "stage": "data_model_type_determined",
                "data_model_type": "table",
                "query_title": query_title,
                "timing": False,
            },
        )

        # Audit
        await log_tool_audit(
            runtime_ctx,
            action="tool.csv_written",
            resource_type="report",
            resource_id=str(report.id) if report else None,
            details={
                "tool": "write_csv",
                "file_id": str(file.id),
                "row_count": total_rows,
            },
        )

        current_step_id = runtime_ctx.get("current_step_id")

        observation = {
            "summary": f"Wrote CSV: {total_rows} rows, {len(full_df.columns)} columns",
            "file_id": str(file.id),
            "row_count": total_rows,
            "columns": list(full_df.columns),
            "data_preview": data_preview,
            "stats": info,
            "data_model": final_dm,
            "view": view_payload,
            "success": True,
            "analysis_complete": False,
            "final_answer": None,
        }
        if current_step_id:
            observation["step_id"] = current_step_id

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "file_id": str(file.id),
                    "file_name": file.filename,
                    "code": generated_code,
                    "data": formatted,
                    "data_preview": data_preview,
                    "stats": info,
                    "data_model": final_dm,
                    "view": view_payload,
                    "execution_log": output_log[:3000],
                },
                "observation": observation,
            },
        )
