import json
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


class CreateDataTool(Tool):
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
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "stage": "data_model_type_determined",
                    "data_model_type": "table",
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
                limit = int(getattr(data, "schema_limit", 10) or 10)
                schemas_excerpt = ctx.render_combined(top_k_per_ds=max(1, limit), index_limit=0, include_index=False)
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
        platform = (getattr(context_view, "meta", {}) or {}).get("external_platform") if context_view else None

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

        coder = Coder(model=runtime_ctx.get("model"), organization_settings=organization_settings, context_hub=context_hub)
        streamer = StreamingCodeExecutor(organization_settings=organization_settings, logger=None, context_hub=context_hub)

        # Combine schemas with files for additional grounding
        schemas = (schemas_excerpt or "") + ("\n\n" + files_context if files_context else "")

        code_errors = []
        generated_code = None
        exec_df = None
        output_log = ""

        # Validation function reused from Coder (permissive for now)
        async def _validator_fn(code, data_model_unused):
            return await coder.validate_code(code, data_model_unused)

        async for e in streamer.generate_and_execute_stream(
            data_model={},
            prompt=data.interpreted_prompt or data.user_prompt,
            schemas=schemas,
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_context_builder=(getattr(context_hub, "code_builder", None) if context_hub else None),
            code_generator_fn=coder.generate_code,
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

        current_step_id = runtime_ctx.get("current_step_id")
        observation = {
            "summary": f"Created data '{data.title}' successfully.",
            "data_preview": data_preview,
            "stats": info,
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
                    "code": generated_code,
                    "data": formatted,
                    "data_preview": data_preview,
                    "stats": info,
                    "execution_log": output_log,
                    "errors": code_errors,
                },
                "observation": observation,
            },
        )


