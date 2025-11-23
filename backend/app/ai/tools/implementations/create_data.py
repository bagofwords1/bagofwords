import json
import asyncio
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    CreateDataInput,
    CreateDataOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolStdoutEvent,
    ToolEndEvent,
)
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.llm import LLM
from app.dependencies import async_session_maker
from app.ai.tools.schemas import DataModel
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.ai.prompt_formatters import build_codegen_context


ALLOWED_VIZ_TYPES = {
    "table","bar_chart","line_chart","pie_chart","area_chart","count",
    "heatmap","map","candlestick","treemap","radar_chart","scatter_plot",
}


class CreateDataTool(Tool):
    # --- Visualization inference (post-execution) ---------------------------------------------
    @staticmethod
    def _build_viz_profile(formatted: Dict[str, Any], allow_llm_see_data: bool) -> Dict[str, Any]:
        info = formatted.get("info", {}) if isinstance(formatted, dict) else {}
        column_info = info.get("column_info") or {}
        cols = []
        for name, meta in (column_info.items() if isinstance(column_info, dict) else []):
            cols.append({
                "name": name,
                "dtype": meta.get("dtype"),
                "non_null_count": meta.get("non_null_count"),
                "unique_count": meta.get("unique_count"),
                "null_count": meta.get("null_count"),
                "min": meta.get("min"),
                "max": meta.get("max"),
            })
        profile: Dict[str, Any] = {
            "row_count": info.get("total_rows"),
            "column_count": info.get("total_columns"),
            "columns": cols,
        }
        if allow_llm_see_data:
            # Add a tiny head sample for better inference (privacy-aware)
            profile["head_rows"] = (formatted.get("rows") or [])[:5]
        return profile

    async def _infer_visualization_model(
        self,
        runtime_ctx: Dict[str, Any],
        user_prompt: str,
        messages_context: str,
        formatted: Dict[str, Any],
        allow_llm_see_data: bool,
    ) -> Dict[str, Any]:
        """Ask a small LLM pass to pick visualization type and series from schema/stats (+sample).

        Returns a minimal DataModel dict validated against schema: at least { type, series? }.
        Fallback to {"type": "table", "series": []} on failure.
        """
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        profile = self._build_viz_profile(formatted, allow_llm_see_data)

        allowed_types = list(ALLOWED_VIZ_TYPES)

        prompt = (
            "You are a visualization planner. Based on the data profile, choose the best visualization "
            "type and construct a minimal series spec. Use ONLY the provided column names. Return JSON only.\n\n"
            "Context messages (recent):\n" + (messages_context or "") + "\n\n"
            "User prompt:\n" + (user_prompt or "") + "\n\n"
            "Data profile (JSON):\n" + json.dumps(profile, ensure_ascii=False) + "\n\n"
            "Return a compact JSON object with keys: type, series, view.\n"
            "- type must be one of: " + ", ".join(allowed_types) + "\n"
            "- series must match the chart type contract:\n"
            "  * bar/line/area/pie/map: [{name, key, value}]\n"
            "  * scatter: [{name, x, y}] (+ size optional)\n"
            "  * heatmap: [{name, x, y, value}]\n"
            "  * candlestick: [{name, open, close, low, high, key}]\n"
            "  * treemap: [{name, id, parentId, value}]\n"
            "  * radar_chart: [{name?, dimensions: [{axis, value}...]}]\n"
            "- For table, return series: []\n"
            "- Do not invent columns.\n"
        )

        try:
            raw = llm.inference(prompt, usage_scope="create_data.viz_infer")
        except Exception:
            raw = None

        candidate = {"type": "table", "series": []}
        view_options: Dict[str, Any] | None = None
        if raw:
            try:
                candidate_json = json.loads(raw)
            except Exception:
                candidate_json = None
            if isinstance(candidate_json, dict):
                try:
                    dm = DataModel(**{k: v for k, v in candidate_json.items() if k in {"type", "series", "group_by", "sort", "limit"}})
                    candidate = dm.model_dump()
                except Exception:
                    candidate = {"type": "table", "series": []}
                # Extract optional view mappings (limit/sort/colors) from candidate_json.view
                try:
                    view = candidate_json.get("view") if isinstance(candidate_json, dict) else None
                    if isinstance(view, dict):
                        # limit
                        if view.get("limit") is not None and candidate.get("limit") is None:
                            candidate["limit"] = view.get("limit")
                        # sort { by, order }
                        sort = view.get("sort")
                        if isinstance(sort, dict) and not candidate.get("sort"):
                            by = sort.get("by") or sort.get("field")
                            order = str(sort.get("order") or "asc").lower()
                            if by:
                                candidate["sort"] = [{"field": by, "direction": ("desc" if order.startswith("d") else "asc")}]
                        # colors → view.options.colors
                        colors = None
                        if isinstance(view.get("colors"), list):
                            colors = view.get("colors")
                        elif isinstance(view.get("color"), str):
                            colors = [view.get("color")]
                        if colors:
                            view_options = {"colors": colors}
                except Exception:
                    pass

        # Normalize: ensure series exists for non-table types
        if candidate.get("type") != "table" and not candidate.get("series"):
            candidate["series"] = []

        # Emit a progress event for UI when series/type are inferred
        try:
            chart_type = candidate.get("type")
            if chart_type and chart_type != "table":
                await asyncio.sleep(0)  # keep cooperative
                payload = {
                    "stage": "series_configured",
                    "series": candidate.get("series") or [],
                    "chart_type": chart_type,
                }
                if view_options:
                    payload["view"] = {"type": chart_type, "options": view_options}
                yield_event = ToolProgressEvent(
                    type="tool.progress",
                    payload=payload,
                )
                # Use synchronous yield pattern by returning a marker to the caller
                return {"data_model": candidate, "progress_event": yield_event, "view_options": view_options}
        except Exception:
            pass
        return {"data_model": candidate, "progress_event": None, "view_options": view_options}
    @staticmethod
    async def _build_schemas_excerpt(context_hub, context_view, user_text: str, top_k: int = 10) -> str:
        """Best-effort schema excerpt similar to CreateWidgetTool, with keyword fallback."""
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
            _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
            return _schemas_section_obj.render("gist") if _schemas_section_obj else ""
        except Exception:
            _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
            return _schemas_section_obj.render() if _schemas_section_obj else ""

    @staticmethod
    def _summarize_errors(errors) -> dict:
        last_text = (errors[-1][1] if errors else "") or ""
        last_line = last_text.strip().splitlines()[0][:300]
        payload = {
            "retry_summary": {
                "attempts": int(len(errors or [])),
                "succeeded": False,
                "error_count": int(len(errors or [])),
                "last_error_message": last_line,
            }
        }
        if last_line:
            payload["error"] = {"message": last_line}
        return payload

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_data",
            description="Generate code from prompt and execute to return tabular data.",
            category="action",
            version="1.0.0",
            input_schema=CreateDataInput.model_json_schema(),
            output_schema=CreateDataOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=180,
            idempotent=False,
            required_permissions=[],
            tags=["data", "code", "execution"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateDataInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDataOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateDataInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Context and views
        organization_settings = runtime_ctx.get("settings")
        context_view = runtime_ctx.get("context_view")
        context_hub = runtime_ctx.get("context_hub")

        # Early: signal intended artifact type and request step creation before code-gen
        try:
            # Single signal: declare type and pass the intended query title
            allowed_types = ALLOWED_VIZ_TYPES
            requested_type = None
            try:
                requested_type = str((tool_input or {}).get("visualization_type") or "").strip()
            except Exception:
                requested_type = None
            viz_type = requested_type if requested_type in allowed_types else "table"
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "stage": "data_model_type_determined",
                    "data_model_type": viz_type,
                    "query_title": data.title,
                },
            )
        except Exception:
            # Best-effort only; if creation fails now, later stages may still create
            pass

        # Build filtered schemas excerpt using tables_by_source (regex-aware), or fallback to keywords
        schemas_excerpt = ""
        try:
            if data.tables_by_source and context_hub and getattr(context_hub, "schema_builder", None):
                import re
                special = re.compile(r"[.*+?^${}()|\[\]\\]")
                data_source_ids = []
                name_patterns = []
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
                ds_scope = list({str(x) for x in data_source_ids}) or None
                ctx = await context_hub.schema_builder.build(
                    include_inactive=False,
                    with_stats=True,
                    data_source_ids=ds_scope,
                    name_patterns=name_patterns or None,
                    active_only=True,
                )
                # Fixed default size for per-datasource schema excerpt
                schemas_excerpt = ctx.render_combined(top_k_per_ds=10, index_limit=0, include_index=False)
            else:
                raw_text = (data.interpreted_prompt or data.user_prompt or "")
                schemas_excerpt = await self._build_schemas_excerpt(context_hub, context_view, raw_text, top_k=10)
        except Exception:
            raw_text = (data.interpreted_prompt or data.user_prompt or "")
            schemas_excerpt = await self._build_schemas_excerpt(context_hub, context_view, raw_text, top_k=10)

        # Static and warm sections for prompt grounding
        _resources_section_obj = getattr(context_view.static, "resources", None) if context_view else None
        resources_context = _resources_section_obj.render() if _resources_section_obj else ""
        _files_section_obj = getattr(context_view.static, "files", None) if context_view else None
        files_context = _files_section_obj.render() if _files_section_obj else ""
        _instructions_section_obj = getattr(context_view.static, "instructions", None) if context_view else None
        instructions_context = _instructions_section_obj.render() if _instructions_section_obj else ""
        _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        _mentions_section_obj = getattr(context_view.static, "mentions", None) if context_view else None
        mentions_context = _mentions_section_obj.render() if _mentions_section_obj else "<mentions>No mentions for this turn</mentions>"
        _entities_section_obj = getattr(context_view.warm, "entities", None) if context_view else None
        entities_context = _entities_section_obj.render() if _entities_section_obj else ""

        # Past observations and history summary
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

        # Code generation and execution with retries
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_code"})

        coder = Coder(
            model=runtime_ctx.get("model"),
            organization_settings=organization_settings,
            context_hub=context_hub,
            usage_session_maker=async_session_maker,
        )
        streamer = StreamingCodeExecutor(organization_settings=organization_settings, logger=None, context_hub=context_hub)

        # Build typed context via helper
        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=(data.user_prompt or data.interpreted_prompt or ""),
            interpreted_prompt=(data.interpreted_prompt or None),
            schemas_excerpt=(schemas_excerpt or ""),
            tables_by_source=(data.tables_by_source or None),
        )

        # Combine schemas with files for additional grounding (keep previous semantics)
        schemas = (codegen_context.schemas_excerpt or "") + ("\n\n" + codegen_context.files_context if codegen_context.files_context else "")

        code_errors = []
        generated_code = None
        exec_df = None
        output_log = ""

        # Validation function reused from Coder (permissive for now)
        async def _validator_fn(code, data_model_unused):
            return await coder.validate_code(code, data_model_unused)

        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=2),
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_context_builder=None,
            code_generator_fn=coder.generate_code,
            validator_fn=_validator_fn,
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

        if generated_code is None or exec_df is None:
            current_step_id = runtime_ctx.get("current_step_id")
            error_observation = {
                "summary": "Create data failed",
                "error": {"type": "execution_failure", "message": "execution failed (validation or execution error)"},
            }
            try:
                error_observation.update(self._summarize_errors(code_errors))
            except Exception:
                pass
            if current_step_id:
                error_observation["step_id"] = current_step_id
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "code": generated_code or "",
                        "data": {},
                        "data_preview": {},
                        "stats": {},
                        "execution_log": output_log,
                        "errors": code_errors,
                    },
                    "observation": error_observation,
                },
            )
            return

        # Success path: format data and privacy-aware preview
        formatted = streamer.format_df_for_widget(exec_df)
        info = formatted.get("info", {})
        allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value if organization_settings else True
        if allow_llm_see_data:
            data_preview = {
                "columns": formatted.get("columns", []),
                "rows": formatted.get("rows", [])[:5],
            }
        else:
            data_preview = {
                "columns": [{"field": c.get("field")} for c in formatted.get("columns", [])],
                "row_count": len(formatted.get("rows", [])),
                "stats": info,
            }

        # Optional: infer minimal visualization model (type + series) using the existing DataModel schema
        inferred_dm = None
        try:
            requested_type = None
            try:
                requested_type = str((tool_input or {}).get("visualization_type") or "").strip()
            except Exception:
                requested_type = None
            effective_type = requested_type if requested_type else "table"
            if effective_type != "table":
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "inferring_visualization"})
                inference = await self._infer_visualization_model(
                    runtime_ctx=runtime_ctx,
                    user_prompt=(data.user_prompt or data.interpreted_prompt or ""),
                    messages_context=codegen_context.messages_context,
                    formatted=formatted,
                    allow_llm_see_data=allow_llm_see_data,
                )
                inferred_dm = (inference or {}).get("data_model")
                progress_event = (inference or {}).get("progress_event")
                inferred_view_opts = (inference or {}).get("view_options")
                if progress_event is not None:
                    # emit the series_configured progress for UI if a non-table chart was chosen
                    yield progress_event
        except Exception:
            inferred_dm = None
            inferred_view_opts = None

        current_step_id = runtime_ctx.get("current_step_id")
        # Always provide a minimal data_model in observation/output
        try:
            fallback_type = effective_type if 'effective_type' in locals() and effective_type else "table"
        except Exception:
            fallback_type = "table"
        # Force the final type to the early/user-requested type; only take series/grouping from inference
        final_dm = {"type": fallback_type, "series": []}
        if isinstance(inferred_dm, dict):
            for key in ("series", "group_by", "sort", "limit"):
                if inferred_dm.get(key) is not None:
                    final_dm[key] = inferred_dm.get(key)
        # Prepare view options (e.g., colors) to send to AgentV2 for persistence
        view_options = None
        try:
            if isinstance(inferred_view_opts, dict) and inferred_view_opts:
                view_options = inferred_view_opts
        except Exception:
            view_options = None

        observation = {
            "summary": f"Created data '{data.title}' successfully.",
            "data_preview": data_preview,
            "stats": info,
            "analysis_complete": False,
            "final_answer": None,
        }
        observation["data_model"] = final_dm
        if view_options:
            observation["view"] = {"type": final_dm.get("type"), "options": view_options}
        if current_step_id:
            observation["step_id"] = current_step_id
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "code": generated_code,
                    "data": formatted,
                    "data_preview": data_preview,
                    "stats": info,
                    "execution_log": output_log,
                    "errors": code_errors,
                    "data_model": final_dm,
                    "view_options": view_options,
                },
                "observation": observation,
            },
        )


