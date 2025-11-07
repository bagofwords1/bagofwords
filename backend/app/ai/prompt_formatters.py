from pydantic import BaseModel
from typing import Optional
from app.ai.schemas.codegen import CodeGenContext


async def build_codegen_context(
    *,
    runtime_ctx: dict,
    user_prompt: str,
    interpreted_prompt: str | None,
    schemas_excerpt: str,
    tables_by_source: list | None = None,
) -> CodeGenContext:
    """
    Build a CodeGenContext from runtime_ctx (ContextHub/ContextView) with safe fallbacks.
    """
    context_view = runtime_ctx.get("context_view") if isinstance(runtime_ctx, dict) else None
    context_hub = runtime_ctx.get("context_hub") if isinstance(runtime_ctx, dict) else None

    # Defaults
    instructions_context = ""
    mentions_context = "<mentions>No mentions for this turn</mentions>"
    entities_context = ""
    messages_context = ""
    resources_context = ""
    files_context = ""
    platform = None
    history_summary = ""
    past_observations = []
    last_observation = None
    data_sources_context = ""

    try:
        # Static sections
        inst_obj = getattr(getattr(context_view, "static", None), "instructions", None) if context_view else None
        if inst_obj:
            try:
                instructions_context = inst_obj.render()
            except Exception:
                instructions_context = ""
        mentions_obj = getattr(getattr(context_view, "static", None), "mentions", None) if context_view else None
        if mentions_obj:
            try:
                mentions_context = mentions_obj.render()
            except Exception:
                mentions_context = mentions_context
        resources_obj = getattr(getattr(context_view, "static", None), "resources", None) if context_view else None
        if resources_obj:
            try:
                resources_context = resources_obj.render()
            except Exception:
                resources_context = ""
        files_obj = getattr(getattr(context_view, "static", None), "files", None) if context_view else None
        if files_obj:
            try:
                files_context = files_obj.render()
            except Exception:
                files_context = ""

        # Warm sections
        messages_obj = getattr(getattr(context_view, "warm", None), "messages", None) if context_view else None
        if messages_obj:
            try:
                messages_context = messages_obj.render()
            except Exception:
                messages_context = ""
        entities_obj = getattr(getattr(context_view, "warm", None), "entities", None) if context_view else None
        if entities_obj:
            try:
                entities_context = entities_obj.render()
            except Exception:
                entities_context = ""

        # Platform meta
        try:
            platform = (getattr(context_view, "meta", {}) or {}).get("external_platform") if context_view else None
        except Exception:
            platform = None

        # Observations/history via ContextHub
        try:
            if context_hub and getattr(context_hub, "observation_builder", None):
                past_observations = context_hub.observation_builder.tool_observations or []
                last_observation = context_hub.observation_builder.get_latest_observation()
        except Exception:
            past_observations = []
            last_observation = None
        try:
            if context_hub and hasattr(context_hub, "get_history_summary"):
                history_summary = await context_hub.get_history_summary()
        except Exception:
            history_summary = ""
    except Exception:
        pass

    # Render data sources/clients descriptions if available in runtime context
    try:
        ds_clients = runtime_ctx.get("ds_clients") if isinstance(runtime_ctx, dict) else None
        if isinstance(ds_clients, dict) and ds_clients:
            lines = []
            for name, client in ds_clients.items():
                try:
                    desc = getattr(client, "description", None)
                    if callable(desc):
                        # Some clients expose description as @property; getattr will yield value
                        desc = desc  # already resolved
                    lines.append(f"data_source_name: {name}\ndescription: {desc}")
                except Exception:
                    lines.append(f"data_source_name: {name}\ndescription: ")
            data_sources_context = "\n".join(lines)
    except Exception:
        data_sources_context = ""

    # Normalize tables_by_source to a list[dict] for Pydantic
    norm_tables_by_source = None
    if tables_by_source:
        try:
            norm: list[dict] = []
            for item in tables_by_source:
                if hasattr(item, "model_dump"):
                    norm.append(item.model_dump())
                elif hasattr(item, "dict"):
                    norm.append(item.dict())
                elif isinstance(item, dict):
                    norm.append(item)
            norm_tables_by_source = norm if norm else None
        except Exception:
            norm_tables_by_source = None

    return CodeGenContext(
        user_prompt=user_prompt or (interpreted_prompt or ""),
        interpreted_prompt=interpreted_prompt or None,
        schemas_excerpt=schemas_excerpt or "",
        data_sources_context=data_sources_context,
        instructions_context=instructions_context,
        mentions_context=mentions_context,
        entities_context=entities_context,
        messages_context=messages_context,
        resources_context=resources_context,
        files_context=files_context,
        platform=platform,
        history_summary=history_summary,
        past_observations=past_observations,
        last_observation=last_observation,
        tables_by_source=norm_tables_by_source,
    )


class TableColumn(BaseModel):
    name: str
    dtype: str | None
    is_active: bool = True


class ForeignKey(BaseModel):
    column: TableColumn
    references_name: str
    references_column: TableColumn


class Table(BaseModel):
    name: str
    columns: list[TableColumn] | None
    pks: list[TableColumn] | None
    fks: list[ForeignKey] | None
    is_active: bool = True

    metadata_json: Optional[dict] = None
    # Optional structural metrics
    centrality_score: Optional[float] = None
    richness: Optional[float] = None
    degree_in: Optional[int] = None
    degree_out: Optional[int] = None
    entity_like: Optional[bool] = None
    # Optional usage/feedback stats
    usage_count: Optional[int] = None
    success_count: Optional[int] = None
    failure_count: Optional[int] = None
    weighted_usage_count: Optional[float] = None
    pos_feedback_count: Optional[int] = None
    neg_feedback_count: Optional[int] = None
    last_used_at: Optional[str] = None
    last_feedback_at: Optional[str] = None
    success_rate: Optional[float] = None
    score: Optional[float] = None


class ServiceFormatter:
    def __init__(self, tables: list[Table]) -> None:
        self.tables = tables
        self.table_str = self.format_tables(tables)
    
    def format_tables(self, tables: list[Table]) -> str:
        table_strs = []
        for table in tables:
            table_strs.append(self.format_table(table))
        return "\n\n".join(table_strs)
    
    def format_table(self, table: Table) -> str:
        table_strs = []
        table_title = f"table: {table.name}"
        table_strs.append(table_title)
        # Optional compact metadata block
        if table.metadata_json:
            try:
                # Prefer a concise single-line summary with key Tableau identifiers
                tmeta = table.metadata_json.get("tableau", {}) if isinstance(table.metadata_json, dict) else {}
                kv = []
                for k in ["datasourceLuid", "projectName", "name"]:
                    v = tmeta.get(k)
                    if v is not None:
                        kv.append(f"{k}={v}")
                if kv:
                    table_strs.append(f"meta: {'; '.join(kv)}")
            except Exception:
                # Best-effort only; never fail formatting due to metadata
                pass
        for col in table.columns or []:
            table_strs.append(f"column: {col.name} type: {col.dtype or 'any'}")
        
        return "\n".join(table_strs)


class TableFormatter:

    table_sep: str = "\n\n"

    def __init__(self, tables: list[Table]) -> None:
        self.tables = tables
        self.table_str = self.format_tables(tables)

    def format_table(self, table: Table) -> str:
        """Get table format."""
        table_fmt = []
        table_name = table.name
        for col in table.columns or []:
            table_fmt.append(f"    {col.name} {col.dtype or 'any'}")
        if table.pks:
            table_fmt.append(
                f"    primary key ({', '.join(pk.name for pk in table.pks)})"
            )
        for fk in table.fks or []:
            table_fmt.append(
                f"    foreign key ({fk.column.name}) references {fk.references_name}({fk.references_column.name})"  # noqa: E501
            )
        # Append compact metrics block if available
        metrics_lines = []
        has_struct = any(v is not None for v in [table.centrality_score, table.richness, table.degree_in, table.degree_out, table.entity_like])
        has_stats = any(v is not None for v in [table.usage_count, table.success_count, table.failure_count, table.pos_feedback_count, table.neg_feedback_count, table.score])
        if has_struct or has_stats:
            metrics_lines.append("    -- metrics --")
        if has_stats:
            if table.score is not None:
                metrics_lines.append(f"    score: {round(table.score, 3)}")
            if table.usage_count is not None or table.success_count is not None or table.failure_count is not None:
                metrics_lines.append(
                    f"    usage: {table.usage_count or 0}, success: {table.success_count or 0}, failure: {table.failure_count or 0}"
                )
            if table.success_rate is not None:
                metrics_lines.append(f"    success_rate: {round(table.success_rate, 3)}")
            if table.pos_feedback_count is not None or table.neg_feedback_count is not None:
                metrics_lines.append(
                    f"    feedback: +{table.pos_feedback_count or 0} / -{table.neg_feedback_count or 0}"
                )
            if table.last_used_at:
                metrics_lines.append(f"    last_used: {table.last_used_at}")
        if has_struct:
            cs = f"{round(table.centrality_score, 3)}" if table.centrality_score is not None else "?"
            rch = f"{round(table.richness, 3)}" if table.richness is not None else "?"
            di = table.degree_in if table.degree_in is not None else "?"
            do = table.degree_out if table.degree_out is not None else "?"
            el = table.entity_like if table.entity_like is not None else "?"
            metrics_lines.append(f"    structural: centrality={cs}, richness={rch}, degree_in={di}, degree_out={do}, entity_like={el}")
        if metrics_lines:
            table_fmt.extend(metrics_lines)
        if table_fmt:
            all_cols = ",\n".join(table_fmt)
            create_tbl = f"CREATE TABLE {table_name} (\n{all_cols}\n)"
        else:
            create_tbl = f"CREATE TABLE {table_name}"
        return create_tbl

    def format_tables(self, tables: list[Table]) -> str:
        return self.table_sep.join(self.format_table(table) for table in tables)
