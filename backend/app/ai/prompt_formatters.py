from pydantic import BaseModel
from typing import Optional


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
