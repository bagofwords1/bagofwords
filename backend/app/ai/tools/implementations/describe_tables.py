import re
from typing import AsyncIterator, Dict, Any, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    DescribeTablesInput,
    DescribeTablesOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)


class DescribeTablesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="describe_tables",
            description=(
                "Describe specific tables to get more information about them. Returns tables, columns, usage metrics, etc. Use this to ensure you have the right tables and columns for your analysis."
            ),
            category="action",
            version="1.0.0",
            input_schema=DescribeTablesInput.model_json_schema(),
            output_schema=DescribeTablesOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["schema", "tables", "columns", "topk", "index"],
            observation_policy="on_trigger",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return DescribeTablesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return DescribeTablesOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = DescribeTablesInput(**tool_input)

        # Emit start
        yield ToolStartEvent(type="tool.start", payload={"query": data.query})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "collecting_index"})

        context_hub = runtime_ctx.get("context_hub")
        context_view = runtime_ctx.get("context_view")
        errors: list[str] = []

        # Resolve queries into exact names vs regex patterns
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_patterns"})
        queries = data.query if isinstance(data.query, list) else [data.query]
        table_names: list[str] = []
        name_patterns: list[str] = []
        special = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
        for q in queries:
            if not isinstance(q, str):
                continue
            try:
                if special.search(q or ""):
                    # Treat as regex pattern
                    name_patterns.append(q)
                else:
                    # Treat as exact name match
                    table_names.append(q)
            except Exception:
                name_patterns.append(q)

        # Broaden simple names to allow optional schema prefix and case-insensitive match
        # e.g., "staff" should match "public.staff" and case variants
        try:
            broadened = []
            for tn in table_names:
                esc = re.escape(tn)
                broadened.append(f"(?i)(?:^|\\.){esc}$")
            if broadened:
                name_patterns.extend(broadened)
        except Exception:
            pass

        # Emit debug of resolved patterns (helps UI debugging instead of breakpoints)
        try:
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "stage": "debug.resolved_queries",
                    "table_names": table_names,
                    "name_patterns": name_patterns,
                },
            )
        except Exception:
            pass

        # Build filtered schema context via the same builder used by the agent
        schemas_excerpt = ""
        searched_sources = 0
        matched_tables_total = 0
        truncated = False
        try:
            if not context_hub or not getattr(context_hub, "schema_builder", None):
                # Fallback to whatever is in the current context view
                _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
                schemas_excerpt = _schemas_section_obj.render() if _schemas_section_obj else ""
            else:
                builder = context_hub.schema_builder
                # Build without top_k slicing so we can compute truncation accurately
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_excerpt"})
                ctx = await builder.build(
                    include_inactive=False,
                    with_stats=True,
                    data_source_ids=data.data_source_ids,
                    table_names=table_names or None,
                    name_patterns=name_patterns or None,
                    active_only=True,
                )
                # Compute counts before render limits
                try:
                    searched_sources = len(getattr(ctx, "data_sources", []) or [])
                    matched_tables_total = sum(len(getattr(ds, "tables", []) or []) for ds in getattr(ctx, "data_sources", []) or [])
                except Exception:
                    searched_sources = 0
                    matched_tables_total = 0

                # Render combined excerpt using a per-source sample cap
                top_k = max(1, int(data.limit or 20))
                schemas_excerpt = ctx.render_combined(top_k_per_ds=top_k, index_limit=200)

                # Determine truncation if any data source has more tables than top_k
                truncated = any(
                    (len(getattr(ds, "tables", []) or []) > top_k) for ds in getattr(ctx, "data_sources", []) or []
                )
        except Exception as e:
            errors.append(str(e))
            schemas_excerpt = schemas_excerpt or ""
            searched_sources = searched_sources or 0
            matched_tables_total = matched_tables_total or 0
            truncated = truncated or False

        # Finalize
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "finalizing"})

        output = DescribeTablesOutput(
            schemas_excerpt=schemas_excerpt,
            truncated=truncated,
            searched_sources=searched_sources,
            searched_tables_est=matched_tables_total,
            errors=errors,
        ).model_dump()

        # Include the original query explicitly for UI display after reloads
        try:
            output["search_query"] = data.query
        except Exception:
            pass

        observation = {
            "summary": f"Described {matched_tables_total} tables across {searched_sources} data sources.",
            "analysis_complete": False,
            "final_answer": None,
            "schemas_excerpt": schemas_excerpt,
        }

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )


