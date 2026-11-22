import re
from typing import AsyncIterator, Dict, Any, Type, List

from pydantic import BaseModel
from sqlalchemy import select, and_

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
from app.models.instruction_reference import InstructionReference
from app.models.datasource_table import DataSourceTable
from app.data_sources.routines import is_routine


def _specificity_tiers(cols, terms) -> Dict[int, int]:
    """{id(column): size of the narrowest term it matched} (smaller = more
    specific), so ranking by usage stays inside a specificity tier."""
    from app.ai.context.column_ranking import column_term_matchers
    matchers = column_term_matchers(terms)
    per_col = {}
    counts = [0] * len(matchers)
    for c in cols:
        name = str(getattr(c, "name", "") or "")
        desc = str(getattr(c, "description", "") or "")
        which = [k for k, (_, m) in enumerate(matchers) if m(name, desc)]
        per_col[id(c)] = which
        for k in which:
            counts[k] += 1
    return {cid: (min(counts[k] for k in which) if which else 0) for cid, which in per_col.items()}


class DescribeTablesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="describe_tables",
            description=(
                "Describe specific tables to get more information about them. Returns tables, columns, usage metrics, etc. "
                "Use this to ensure you have the right tables and columns for your analysis. "
                "Also returns instructions and business rules associated with the tables. "
                "Tables with instructions>0 in the schema have associated rules — use this tool to see them. "
                "When a data source has multiple connections with identically-named tables, use connection_ids to scope the search to a specific connection and avoid duplicates. "
                "Also describes stored procedures and functions (<routine> entries): returns their source code, "
                "so you can apply the business logic they encode in your own SELECT. "
                "Wide tables show only their top columns (by usage, instructions and keys) and fold the rest into a "
                "<more_columns> summary such as 'svc.request.* (412)'. Those columns still exist and can be queried: "
                "find them with `columns` (names or patterns, matched against column names and descriptions). "
                "`query` + `columns` returns only the matching columns of those tables; `columns` alone searches every "
                "table in scope and returns each table that has a match (use it to ask 'which table has a user id column?')."
            ),
            category="research",
            version="1.0.0",
            input_schema=DescribeTablesInput.model_json_schema(),
            output_schema=DescribeTablesOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["schema", "tables", "columns", "topk", "index", "procedures", "functions"],
            observation_policy="on_trigger",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return DescribeTablesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return DescribeTablesOutput

    # Cap on how many thin tables we sample per describe_tables call — bounds the
    # extra searches a schema-on-read source can trigger from one inspection.
    _MAX_THIN_SAMPLES = 12

    async def _sample_thin_tables(self, ctx: Any, runtime_ctx: Dict[str, Any]) -> None:
        """Fill columns for matched tables that were indexed with none (thin,
        schema-on-read). Best-effort: any failure leaves the table as-is. No-op
        for normal sources — their tables already carry columns."""
        try:
            db = runtime_ctx.get("db")
            organization = runtime_ctx.get("organization")
            context_hub = runtime_ctx.get("context_hub")
            if db is None or context_hub is None:
                return
            ds_objs = {str(getattr(d, "id", "")): d
                       for d in (getattr(context_hub, "data_sources", []) or [])}
            user = getattr(context_hub, "user", None)
            from app.services.data_source_service import DataSourceService
            dss = DataSourceService()
            clients_cache: Dict[str, Dict[str, Any]] = {}
            sampled = 0
            for ds in (getattr(ctx, "data_sources", []) or []):
                if sampled >= self._MAX_THIN_SAMPLES:
                    break
                ds_id = str(getattr(getattr(ds, "info", None), "id", "") or "")
                ds_obj = ds_objs.get(ds_id)
                if ds_obj is None:
                    continue
                for t in (getattr(ds, "tables", []) or []):
                    if sampled >= self._MAX_THIN_SAMPLES:
                        break
                    if (getattr(t, "columns", None) or []):
                        continue  # already has columns — nothing to sample
                    if is_routine(t):
                        continue  # a procedure with no parameters, not a thin table
                    if getattr(t, "excluded_columns", None):
                        # Empty because the agent manager hid columns, not
                        # because the source is schema-on-read: sampling would
                        # put them straight back.
                        continue
                    if ds_id not in clients_cache:
                        try:
                            clients_cache[ds_id] = await dss.construct_clients(db, ds_obj, user) or {}
                        except Exception:
                            clients_cache[ds_id] = {}
                    clients = {k: v for k, v in clients_cache[ds_id].items() if ":" in str(k)}
                    conn_name = getattr(t, "connection_name", None)
                    client = clients.get(f"{ds_obj.name}:{conn_name}") if conn_name else None
                    if client is None and len(clients) == 1:
                        client = next(iter(clients.values()))
                    if client is None or not hasattr(client, "aget_schema"):
                        continue
                    try:
                        sampled_tbl = await client.aget_schema(getattr(t, "name", None))
                    except Exception:
                        continue
                    cols = getattr(sampled_tbl, "columns", None) or [] if sampled_tbl else []
                    if cols:
                        t.columns = cols
                        sampled += 1
        except Exception:
            # Never let on-demand sampling break table inspection.
            return

    async def _apply_column_search(self, ctx: Any, terms: List[str], *, keep_unmatched: bool,
                                   per_ds_limit: int, runtime_ctx: Dict[str, Any]) -> None:
        """Narrow every table in ``ctx`` to the columns matching ``terms``.

        ``keep_unmatched``: with a table query, a named table with no match
        stays (as "0 of N") so the agent learns the column is not there; a
        scope-wide search keeps only tables that have a match. Columns the
        agent manager excluded never reach this point (the builder drops them)
        and are filtered again here, in case a late fill re-added them.
        Matching columns are ordered by usage (one batched stats read for the
        returned tables)."""
        from app.ai.context.column_ranking import MAX_MATCHED_COLUMNS, rank_table_columns, search_columns
        from app.ai.prompt_formatters import filter_columns

        returned = []
        for ds in (getattr(ctx, "data_sources", []) or []):
            kept = []
            for t in (getattr(ds, "tables", []) or []):
                if is_routine(t):
                    if keep_unmatched:
                        kept.append(t)
                    continue
                visible = filter_columns(getattr(t, "columns", None) or [], getattr(t, "excluded_columns", None))
                hits = search_columns(visible, terms)
                if not hits and not keep_unmatched:
                    continue
                t.columns = hits
                t.column_match = {"terms": list(terms), "matched": len(hits), "total": len(visible)}
                t.column_budget = MAX_MATCHED_COLUMNS
                t.columns_ranked = False
                kept.append(t)
            ds.tables = kept
            returned.extend(kept[:per_ds_limit])
        ids = [str(t.id) for t in returned if getattr(t, "id", None) and (t.columns or [])]
        if not ids:
            return
        try:
            from app.services.column_usage_service import ColumnUsageService
            db = runtime_ctx.get("db")
            lock = runtime_ctx.get("tool_db_lock")
            if db is None:
                return
            if lock is not None:
                async with lock:
                    stats = await ColumnUsageService().load_stats(db, ids)
            else:
                stats = await ColumnUsageService().load_stats(db, ids)
        except Exception:
            stats = {}
        for t in returned:
            if t.columns:
                # Usage order within each specificity tier (search_columns put
                # the narrowest matches first; keep that the primary key).
                specificity = _specificity_tiers(t.columns, terms)
                rank_table_columns(t, stats.get(str(getattr(t, "id", "") or "")), None)
                t.columns = sorted(t.columns, key=lambda c: specificity.get(id(c), 0))

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = DescribeTablesInput(**tool_input)

        # Same-args dedupe: an identical describe this run already returned the
        # tables + their business rules — re-fetching just burns a turn (models
        # otherwise loop on the schema's static instructions>0 markers).
        _seen: dict = runtime_ctx.setdefault("_describe_tables_seen", {})
        query_terms = [q for q in (data.query if isinstance(data.query, list) else [data.query]) if isinstance(q, str) and q.strip()]
        column_terms = [c.strip() for c in (data.columns if isinstance(data.columns, list) else [data.columns])
                        if isinstance(c, str) and c.strip()]
        _qkey = "|".join(sorted(str(q).strip().lower() for q in query_terms))
        if column_terms:
            _qkey += "#cols:" + "|".join(sorted(c.lower() for c in column_terms))
        _qkey += "#ds:" + ",".join(sorted(data.data_source_ids or [])) + "#conn:" + ",".join(sorted(data.connection_ids or []))
        if _qkey and _qkey in _seen:
            yield ToolStartEvent(type="tool.start", payload={"query": data.query, "columns": data.columns})
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"success": True, "message": "already described this run"},
                "observation": {"summary": (
                    "You already described these tables this run — their columns and "
                    "business rules were returned above. Proceed to data work "
                    "(create_data / inspect_data); do not describe them again."
                ), "artifacts": []},
            })
            return
        _seen[_qkey] = True

        # Emit start
        yield ToolStartEvent(type="tool.start", payload={"query": data.query, "columns": data.columns})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "collecting_index"})

        context_hub = runtime_ctx.get("context_hub")
        context_view = runtime_ctx.get("context_view")
        errors: list[str] = []

        # Resolve queries into name patterns (always escaped literal + optional raw regex)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_patterns"})
        queries = query_terms
        name_patterns: list[str] = []
        special = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
        for q in queries:
            if not isinstance(q, str):
                continue
            # Always add escaped literal version (handles names with special chars like parens)
            esc = re.escape(q)
            name_patterns.append(f"(?i)(?:^|[./]){esc}$")
            # Separator-tolerant variant: match the query as the leading segment of
            # a delimited/pattern name so a plain query finds collapsed or namespaced
            # tables — e.g. "security" -> "security-*" (Elasticsearch patterns),
            # "web" -> "web::access_combined" (Splunk index::sourcetype). Bounded to a
            # separator so it won't match unrelated names like "securityaudit". The
            # leading anchor also allows ':' so a bare sourcetype matches a Splunk
            # "index::sourcetype" name (e.g. "json_app" -> "app::json_app").
            name_patterns.append(f"(?i)(?:^|[./:]){esc}(?:[-:._*/].*)?$")

            # Also add as raw regex if it contains special chars (for intentional patterns like .*Opportunities.*)
            if special.search(q or ""):
                try:
                    re.compile(q)  # validate it's a valid regex
                    name_patterns.append(f"(?i){q}")
                except re.error:
                    pass  # invalid regex, skip

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
                ctx = None
            else:
                builder = context_hub.schema_builder
                # Build without top_k slicing so we can compute truncation accurately
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_excerpt"})
                ctx = await builder.build(
                    with_stats=True,
                    data_source_ids=data.data_source_ids,
                    connection_ids=data.connection_ids,
                    name_patterns=name_patterns or None,
                )
                # Sample-on-demand for THIN tables (schema-on-read sources like
                # Splunk index a cheap catalog and leave low-volume sourcetypes
                # with no columns). When the agent inspects such a table, fetch its
                # fields live via the client's get_schema so the excerpt carries
                # real columns instead of "0 columns" (which the agent misreads as
                # empty). Best-effort and no-op for normal sources (their tables
                # always have columns); never breaks the tool.
                await self._sample_thin_tables(ctx, runtime_ctx)

                # Column search: keep only matching columns; without
                # a table query, keep only the tables that have a match.
                if column_terms:
                    yield ToolProgressEvent(type="tool.progress", payload={"stage": "searching_columns"})
                    await self._apply_column_search(ctx, column_terms, keep_unmatched=bool(query_terms),
                                                    per_ds_limit=max(1, int(data.limit or 10)), runtime_ctx=runtime_ctx)

                # Compute counts before render limits
                try:
                    searched_sources = len(getattr(ctx, "data_sources", []) or [])
                    matched_tables_total = sum(len(getattr(ds, "tables", []) or []) for ds in getattr(ctx, "data_sources", []) or [])
                except Exception:
                    searched_sources = 0
                    matched_tables_total = 0

                # Render combined excerpt using a per-source sample cap
                top_k = max(1, int(data.limit or 20))
                schemas_excerpt = ctx.render_combined(top_k_per_ds=top_k, index_limit=200, include_routine_definitions=True)

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

        # Lightweight preview for UI: gather top tables across data sources
        try:
            top_tables: list[dict[str, Any]] = []
            if locals().get("ctx") is not None:
                per_ds_cap = max(1, int(getattr(data, "limit", 10) or 10))
                for ds in (getattr(ctx, "data_sources", []) or []):
                    ds_info = getattr(ds, "info", None)
                    ds_id = getattr(ds_info, "id", None)
                    ds_name = getattr(ds_info, "name", None)
                    ds_type = getattr(ds_info, "type", None)
                    for t in (getattr(ds, "tables", []) or [])[:per_ds_cap]:
                        # Columns preview
                        # The same bounded column list the model saw: a column
                        # search's matches, or a wide table's top columns.
                        col_items: list[dict[str, Any]] = []
                        cols_total = None
                        cols_matched = None
                        try:
                            from app.ai.context.column_ranking import curate_columns
                            _cur = curate_columns(t)
                            match = getattr(t, "column_match", None)
                            cols_total = (match or {}).get("total", _cur.total)
                            cols_matched = (match or {}).get("matched") if match else None
                            for c in _cur.shown:
                                col_items.append({
                                    "name": getattr(c, "name", None),
                                    "dtype": getattr(c, "dtype", None),
                                    "description": getattr(c, "description", None),
                                    "metadata": getattr(c, "metadata", None),
                                    "usage_count": getattr(c, "usage_count", None),
                                })
                        except Exception:
                            col_items = []
                        # Usage preview
                        usage = {
                            "usage_count": getattr(t, "usage_count", None),
                            "success_count": getattr(t, "success_count", None),
                            "failure_count": getattr(t, "failure_count", None),
                            "success_rate": getattr(t, "success_rate", None),
                            "last_used_at": getattr(t, "last_used_at", None),
                            "score": getattr(t, "score", None),
                        }
                        if not any(v is not None for v in usage.values()):
                            usage = None
                        top_tables.append({
                            "data_source_id": ds_id,
                            "data_source_name": ds_name,
                            "data_source_type": getattr(t, "connection_type", None) or ds_type,
                            "connection_name": getattr(t, "connection_name", None),
                            "connection_type": getattr(t, "connection_type", None),
                            "schema": None,
                            "name": getattr(t, "name", None),
                            "full_name": None,
                            "description": getattr(t, "description", None),
                            "metadata": getattr(t, "metadata_json", None),
                            "columns": col_items,
                            "usage": usage,
                            "columns_total": cols_total,
                            "columns_matched": cols_matched,
                        })
            output["top_tables"] = top_tables
        except Exception:
            output["top_tables"] = []

        # Include the original query explicitly for UI display after reloads
        try:
            output["search_query"] = data.query
            output["search_columns"] = data.columns
        except Exception:
            pass

        # Load related instructions via table references (only intelligent load_mode)
        related_instructions: List[dict[str, Any]] = []
        try:
            db = runtime_ctx.get("db")
            organization = runtime_ctx.get("organization")
            if db and organization and ctx is not None:
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_instructions"})

                # Collect matched table names and their data source IDs
                matched_table_info: List[tuple[str, str]] = []  # (table_name, ds_id)
                for ds in (getattr(ctx, "data_sources", []) or []):
                    ds_info = getattr(ds, "info", None)
                    ds_id = getattr(ds_info, "id", None)
                    if ds_id:
                        for t in (getattr(ds, "tables", []) or []):
                            t_name = getattr(t, "name", None)
                            if t_name:
                                matched_table_info.append((t_name, str(ds_id)))

                if matched_table_info:
                    # Query DataSourceTable to get IDs for the matched tables
                    table_names_list = [name for name, _ in matched_table_info]
                    ds_ids_list = list(set(ds_id for _, ds_id in matched_table_info))

                    table_query = select(DataSourceTable.id).where(
                        and_(
                            DataSourceTable.name.in_(table_names_list),
                            DataSourceTable.datasource_id.in_(ds_ids_list),
                            DataSourceTable.deleted_at.is_(None),
                        )
                    )
                    table_result = await db.execute(table_query)
                    table_ids = [str(row[0]) for row in table_result.fetchall()]

                    if table_ids:
                        # Query InstructionReference for these table IDs
                        ref_query = select(InstructionReference.instruction_id).where(
                            and_(
                                InstructionReference.object_type == "datasource_table",
                                InstructionReference.object_id.in_(table_ids),
                                InstructionReference.deleted_at.is_(None),
                            )
                        )
                        ref_result = await db.execute(ref_query)
                        instruction_ids = list(set(str(row[0]) for row in ref_result.fetchall()))

                        if instruction_ids:
                            # Load instructions via InstructionContextBuilder (only intelligent)
                            from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

                            # Reuse context_hub's builder (has data_source_ids + build context)
                            # or create a new one as fallback
                            if context_hub and getattr(context_hub, 'instruction_builder', None):
                                instruction_builder = context_hub.instruction_builder
                            else:
                                instruction_builder = InstructionContextBuilder(
                                    db=db,
                                    organization=organization,
                                )
                            instruction_items = await instruction_builder.load_instructions_by_ids(
                                instruction_ids=instruction_ids,
                                load_mode_filter="intelligent",
                            )

                            # Convert to output format
                            for item in instruction_items:
                                related_instructions.append({
                                    "id": item.id,
                                    "title": item.title,
                                    "category": item.category,
                                    "text": item.text,
                                    "load_mode": item.load_mode,
                                })
        except Exception as e:
            errors.append(f"Failed to load related instructions: {str(e)}")

        output["related_instructions"] = related_instructions

        # Build instructions excerpt for observation (same format as context builder)
        instructions_excerpt = ""
        if related_instructions:
            from app.ai.context.sections.instructions_section import InstructionsSection, InstructionItem as InstructionSectionItem
            instruction_items = [
                InstructionSectionItem(
                    id=inst["id"],
                    category=inst.get("category"),
                    text=inst.get("text") or "",
                    load_mode=inst.get("load_mode"),
                    load_reason="table_reference",
                )
                for inst in related_instructions
            ]
            instructions_section = InstructionsSection(items=instruction_items)
            instructions_excerpt = instructions_section.render()

        summary = f"Described {matched_tables_total} tables across {searched_sources} data sources."
        if column_terms:
            summary = (f"Column search for {', '.join(column_terms)}: {matched_tables_total} tables with matching "
                       f"columns across {searched_sources} data sources.")
        observation = {
            "summary": summary,
            "analysis_complete": False,
            "final_answer": None,
            "schemas_excerpt": schemas_excerpt,
            "instructions_excerpt": instructions_excerpt,
        }

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )


