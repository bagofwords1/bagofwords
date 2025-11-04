import asyncio
import json
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
    # Helper: Build concise schemas excerpt (AgentV2-style) with optional keyword filtering
    @staticmethod
    async def _build_schemas_excerpt(context_hub, context_view, user_text: str, top_k: int = 10) -> str:
        try:
            import re
            if context_hub and getattr(context_hub, "schema_builder", None):
                tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]{3,}", user_text or "")]
                seen = set()
                keywords = []
                for t in tokens:
                    if t in seen:
                        continue
                    seen.add(t)
                    keywords.append(t)
                    if len(keywords) >= 3:
                        break
                name_patterns = [f"(?i){re.escape(k)}" for k in keywords] if keywords else None

                ctx = await context_hub.schema_builder.build(
                    include_inactive=False,
                    with_stats=True,
                    name_patterns=name_patterns,
                    active_only=True,
                )
                return ctx.render_combined(top_k_per_ds=top_k, index_limit=0, include_index=False)
            # Fallback to compact static renderers
            _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
            return _schemas_section_obj.render("gist") if _schemas_section_obj else ""
        except Exception:
            _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
            return _schemas_section_obj.render() if _schemas_section_obj else ""

    # Lean, provider-agnostic error summarization for observations
    @staticmethod
    def _summarize_errors(errors, attempts: int | None = None, succeeded: bool = False) -> dict:
        # errors: List[Tuple[str, str]] -> (code_text, error_text)
        last_text = (errors[-1][1] if errors else "") or ""
        last_line = last_text.strip().splitlines()[0][:300]
        attempts = attempts if attempts is not None else (len(errors or []) + (1 if succeeded else 0))
        payload = {
            "retry_summary": {
                "attempts": int(attempts),
                "succeeded": bool(succeeded),
                "error_count": int(len(errors or [])),
                "last_error_message": last_line,
            }
        }
        if not succeeded and last_line:
            payload["error"] = {"message": last_line}
        return payload
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
        # Schemas (prefer explicit tables_by_source if provided, else AgentV2-style keyword filtering)
        context_hub = runtime_ctx.get("context_hub")
        schemas_excerpt = ""
        try:
            if data.tables_by_source and context_hub and getattr(context_hub, "schema_builder", None):
                import re
                # Collect source scope and patterns
                data_source_ids = []
                name_patterns = []
                # Detect if a string contains regex special characters
                # Classic safe class: [.*+?^${}()|\[\]\\]
                special = re.compile(r"[.*+?^${}()|\[\]\\]")
                for group in (data.tables_by_source or []):
                    if group.data_source_id:
                        data_source_ids.append(group.data_source_id)
                    for q in (group.tables or []):
                        if not isinstance(q, str):
                            continue
                        try:
                            if special.search(q or ""):
                                name_patterns.append(q)
                            else:
                                esc = re.escape(q)
                                name_patterns.append(f"(?i)(?:^|\\.){esc}$")
                        except Exception:
                            continue
                # Deduplicate ds scope
                ds_scope = list({str(x) for x in data_source_ids}) or None
                # Build filtered schema
                ctx = await context_hub.schema_builder.build(
                    include_inactive=False,
                    with_stats=True,
                    data_source_ids=ds_scope,
                    name_patterns=name_patterns or None,
                    active_only=True,
                )
                limit = int(getattr(data, "schema_limit", 10) or 10)
                schemas_excerpt = ctx.render_combined(top_k_per_ds=max(1, limit), index_limit=0, include_index=False)
            else:
                raw_text = (data.interpreted_prompt or data.user_prompt or "")
                schemas_excerpt = await self._build_schemas_excerpt(context_hub, context_view, raw_text, top_k=10)
        except Exception as e:
            raw_text = (data.interpreted_prompt or data.user_prompt or "")
            schemas_excerpt = await self._build_schemas_excerpt(context_hub, context_view, raw_text, top_k=10)
        # Resources
        _resources_section_obj = getattr(context_view.static, "resources", None) if context_view else None
        resources_context = _resources_section_obj.render() if _resources_section_obj else ""
        # Files
        _files_section_obj = getattr(context_view.static, "files", None) if context_view else None
        files_context = _files_section_obj.render() if _files_section_obj else ""
        # Instructions
        _instructions_section_obj = getattr(context_view.static, "instructions", None) if context_view else None
        instructions_context = _instructions_section_obj.render() if _instructions_section_obj else ""
        # Messages
        _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        # Mentions
        _mentions_section_obj = getattr(context_view.static, "mentions", None) if context_view else None
        mentions_context = _mentions_section_obj.render() if _mentions_section_obj else "<mentions>No mentions for this turn</mentions>"
        # Entities (warm)
        _entities_section_obj = getattr(context_view.warm, "entities", None) if context_view else None
        entities_context = _entities_section_obj.render() if _entities_section_obj else ""
        # Platform
        platform = (getattr(context_view, "meta", {}) or {}).get("external_platform") if context_view else None
        # Observations and history
        context_hub = runtime_ctx.get("context_hub")
        past_observations = []
        last_observation = None
        if context_hub and getattr(context_hub, "observation_builder", None):
            try:
                past_observations = context_hub.observation_builder.tool_observations or []
                last_observation = context_hub.observation_builder.get_latest_observation()
            except Exception:
                past_observations = []
                last_observation = None
        history_summary = ""
        if context_hub and hasattr(context_hub, "get_history_summary"):
            try:
                history_summary = await context_hub.get_history_summary()
            except Exception:
                history_summary = ""



        # Phase 1: Generate Data Model (streamed parsing like create_data_model)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_data_model"})
        llm = LLM(runtime_ctx.get("model"))

        header = f"""
You are a data modeling assistant.
Given the user's goal and available schemas, produce a normalized JSON data_model that will be streamed progressively.

INPUT ENVELOPE
<user_prompt>{data.user_prompt}</user_prompt>
<context>
  <platform>{platform}</platform>
  {instructions_context}
  {schemas_excerpt}
  {files_context}
  {mentions_context}
  {entities_context}
  {resources_context if resources_context else 'No metadata resources available'}
  {history_summary}
  {messages_context if messages_context else 'No detailed conversation history available'}
  <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>
  <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>
</context>

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
 - Prefer using data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.
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
        async for chunk in llm.inference_stream(prompt):
            # Guard against empty SSE heartbeats
            if not chunk:
                continue
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
        files_section = getattr(context_view.static, "files", None) if context_view else None
        schemas = (schemas_excerpt or "") + ("\n\n" + files_section.render() if files_section else "")
        messages_section = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = messages_section.render() if messages_section else ""

        # Stream generation + execution with retries
        # Stream and capture final results
        exec_df = None
        generated_code = None
        code_errors = []
        output_log = ""

        # Define validator before use to avoid scope errors
        async def _validator_fn(code, data_model):
            validator_coder = Coder(model=runtime_ctx.get("model"), organization_settings=organization_settings, context_hub=context_hub)
            return await validator_coder.validate_code(code, data_model)

        async for e in streamer.generate_and_execute_stream(
            data_model=final_data_model,
            prompt=data.interpreted_prompt or data.user_prompt,
            schemas=schemas,
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_context_builder=code_context_builder,
            code_generator_fn=coder.data_model_to_code,
            validator_fn=_validator_fn,
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
                "error": {"type": "execution_failure", "message": "execution failed (validation or execution error)"},
            }
            # Enrich with deterministic retry summary and last error message
            try:
                error_observation.update(self._summarize_errors(code_errors, succeeded=False))
            except Exception:
                pass
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
            # Allow planner reflection and further steps in next loop
            "analysis_complete": False,
            "final_answer": None
        }
        # If there were internal retries/errors but eventual success, include summary
        try:
            if code_errors:
                observation.update(self._summarize_errors(code_errors, succeeded=True))
        except Exception:
            pass
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


        