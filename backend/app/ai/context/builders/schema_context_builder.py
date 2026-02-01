"""
Schema Context Builder - builds TablesSchemaContext object for schemas
"""
from typing import List, Optional, Dict, Any
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, func, and_
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table as PromptTable, TableColumn as PromptTableColumn, ForeignKey as PromptForeignKey
from app.models.table_stats import TableStats
from app.models.organization import Organization
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.instruction_reference import InstructionReference
from app.models.user_data_source_overlay import UserDataSourceTable, UserDataSourceColumn


class SchemaContextBuilder:
    """
    Builds database schema context for agent execution as a structured object.
    """
    
    def __init__(self, db: AsyncSession, data_sources: List[DataSource], organization: Organization, report: Report, user=None):
        self.db = db
        self.organization = organization
        self.report = report
        self.data_sources = data_sources
        self.user = user

    async def build(
        self,
        with_stats: bool = True,
        top_k: Optional[int] = None,
        *,
        data_source_ids: Optional[List[str]] = None,
        table_names: Optional[List[str]] = None,
        name_patterns: Optional[List[str]] = None,
        active_only: bool = True,
        sort: str = "score",  # "score" | "usage" | "centrality" | "alpha"
    ) -> TablesSchemaContext:
        """Return TablesSchemaContext with optional filtering and sorting.

        Args:
            with_stats: Include usage statistics for tables.
            top_k: Limit number of tables returned.
            data_source_ids: Filter to specific data sources.
            table_names: Filter to specific table names (exact match).
            name_patterns: Filter tables by regex patterns.
            active_only: If True (default), only return active tables. If False, include inactive.
            sort: Sort order for tables.
        """
        ds_sections: List[TablesSchemaContext.DataSource] = []

        ds_filter = set(str(x) for x in (data_source_ids or [])) if data_source_ids else None
        for ds in self.data_sources:
            if ds_filter and str(ds.id) not in ds_filter:
                continue
            # Build stats map (table name lowercase -> TableStats)
            stats_map: Dict[str, TableStats] = {}
            if with_stats:
                res = await self.db.execute(
                    select(TableStats).where(
                        TableStats.report_id == None,
                        TableStats.data_source_id == str(ds.id),
                    )
                )
                for s in res.scalars().all():
                    stats_map[(s.table_fqn or '').lower()] = s

            # Canonical (org-level) source
            ds_tables_result = await self.db.execute(
                select(DataSourceTable).where(DataSourceTable.datasource_id == str(ds.id))
            )
            ds_tables = ds_tables_result.scalars().all()
            canonical_by_name: Dict[str, DataSourceTable] = {getattr(t, 'name', ''): t for t in ds_tables}

            # Choose source: overlay for user_required with user, else canonical
            use_overlay = (getattr(ds, 'auth_policy', 'system_only') == 'user_required') and (self.user is not None)

            # Normalize into a common shape for downstream rendering
            # Each entry: { name, columns: [{name,dtype}], pks: [{name,dtype}], fks: [fk], metadata_json, metrics, is_active }
            normalized: List[Dict[str, Any]] = []

            if use_overlay:
                overlays_q = await self.db.execute(
                    select(UserDataSourceTable).where(
                        UserDataSourceTable.data_source_id == str(ds.id),
                        UserDataSourceTable.user_id == str(self.user.id),
                        UserDataSourceTable.is_accessible == True,
                    )
                )
                overlay_tables = overlays_q.scalars().all()
                overlay_ids = [str(ot.id) for ot in overlay_tables]
                cols_q = await self.db.execute(
                    select(UserDataSourceColumn).where(
                        UserDataSourceColumn.user_data_source_table_id.in_(overlay_ids)
                    )
                )
                cols = cols_q.scalars().all()
                cols_by_table: Dict[str, list[UserDataSourceColumn]] = {}
                for c in cols:
                    cols_by_table.setdefault(str(c.user_data_source_table_id), []).append(c)

                for ot in overlay_tables:
                    name = getattr(ot, 'table_name', '') or ''
                    overlay_cols = cols_by_table.get(str(ot.id), [])
                    columns = [{"name": getattr(c, 'column_name', ''), "dtype": getattr(c, 'data_type', None)} for c in overlay_cols]
                    base = canonical_by_name.get(name)
                    # Respect canonical table's is_active status (default False if not found)
                    canonical_is_active = bool(getattr(base, 'is_active', False)) if base is not None else False
                    # Skip inactive tables when active_only is True
                    if active_only and not canonical_is_active:
                        continue
                    pks = getattr(base, 'pks', []) if base is not None else []
                    fks = getattr(base, 'fks', []) if base is not None else []
                    metadata_json = getattr(base, 'metadata_json', None) if base is not None else None
                    normalized.append({
                        "name": name,
                        "table_id": str(base.id) if base is not None else None,
                        "columns": columns,
                        "pks": pks,
                        "fks": fks,
                        "metadata_json": metadata_json,
                        "centrality_score": getattr(base, 'centrality_score', None) if base is not None else None,
                        "richness": getattr(base, 'richness', None) if base is not None else None,
                        "degree_in": getattr(base, 'degree_in', None) if base is not None else None,
                        "degree_out": getattr(base, 'degree_out', None) if base is not None else None,
                        "entity_like": getattr(base, 'entity_like', None) if base is not None else None,
                        "is_active": canonical_is_active,
                    })
            else:
                for t in ds_tables:
                    table_is_active = bool(getattr(t, 'is_active', False))
                    # Skip inactive tables when active_only is True
                    if active_only and not table_is_active:
                        continue
                    columns = [{"name": col.get("name"), "dtype": col.get("dtype", "unknown")} for col in (getattr(t, 'columns', []) or [])]
                    normalized.append({
                        "name": getattr(t, 'name', ''),
                        "table_id": str(t.id) if getattr(t, 'id', None) else None,
                        "columns": columns,
                        "pks": getattr(t, 'pks', []) or [],
                        "fks": getattr(t, 'fks', []) or [],
                        "metadata_json": getattr(t, 'metadata_json', None),
                        "centrality_score": getattr(t, 'centrality_score', None),
                        "richness": getattr(t, 'richness', None),
                        "degree_in": getattr(t, 'degree_in', None),
                        "degree_out": getattr(t, 'degree_out', None),
                        "entity_like": getattr(t, 'entity_like', None),
                        "is_active": table_is_active,
                    })

            # Batch-query instruction reference counts for all tables in this data source
            instruction_ref_counts: Dict[str, int] = {}
            table_ids_for_ref = [item["table_id"] for item in normalized if item.get("table_id")]
            if table_ids_for_ref:
                try:
                    ref_count_result = await self.db.execute(
                        select(
                            InstructionReference.object_id,
                            func.count(InstructionReference.id)
                        ).where(
                            and_(
                                InstructionReference.object_type == 'datasource_table',
                                InstructionReference.object_id.in_(table_ids_for_ref),
                                InstructionReference.deleted_at.is_(None),
                            )
                        ).group_by(InstructionReference.object_id)
                    )
                    for object_id, count in ref_count_result.all():
                        instruction_ref_counts[str(object_id)] = count
                except Exception:
                    pass  # Non-critical - continue without counts

            # Common rendering and scoring
            scored: List[tuple[float, PromptTable]] = []
            tables: List[PromptTable] = []
            for item in normalized:
                columns = [
                    PromptTableColumn(name=c.get("name"), dtype=c.get("dtype"))
                    for c in (item.get("columns") or [])
                ]
                pks = [
                    PromptTableColumn(name=pk.get("name"), dtype=pk.get("dtype"))
                    for pk in (item.get("pks") or [])
                ]
                fks = [
                    PromptForeignKey(
                        column=PromptTableColumn(name=fk.get('column', {}).get('name'), dtype=fk.get('column', {}).get('dtype')),
                        references_name=fk.get('references_name'),
                        references_column=PromptTableColumn(name=fk.get('references_column', {}).get('name'), dtype=fk.get('references_column', {}).get('dtype')),
                    )
                    for fk in (item.get("fks") or [])
                ]

                tbl = PromptTable(
                    name=item.get("name", ""),
                    columns=columns,
                    pks=pks,
                    fks=fks,
                    is_active=bool(item.get("is_active", False)),  # Default False for safety
                    centrality_score=item.get("centrality_score"),
                    richness=item.get("richness"),
                    degree_in=item.get("degree_in"),
                    degree_out=item.get("degree_out"),
                    entity_like=item.get("entity_like"),
                    metadata_json=item.get("metadata_json"),
                    referenced_instructions_count=instruction_ref_counts.get(item.get("table_id", ""), None) or None,
                )

                if with_stats:
                    key = (item.get("name", "") or '').lower()
                    s = stats_map.get(key)
                    if s:
                        usage_count = int(s.usage_count or 0)
                        success_count = int(s.success_count or 0)
                        failure_count = int(s.failure_count or 0)
                        weighted_usage_count = float(s.weighted_usage_count or 0.0)
                        pos_feedback_count = int(s.pos_feedback_count or 0)
                        neg_feedback_count = int(s.neg_feedback_count or 0)
                        last_used_at = s.last_used_at.isoformat() if s.last_used_at else None
                        last_feedback_at = s.last_feedback_at.isoformat() if s.last_feedback_at else None
                        success_rate = (success_count / max(1, usage_count)) if usage_count > 0 else 0.0
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        if s.last_used_at:
                            age_days = max(0.0, (now - s.last_used_at.replace(tzinfo=timezone.utc)).total_seconds() / 86400.0)
                        else:
                            age_days = 365.0
                        recency = pow(2.718281828, -age_days / 14.0)
                        usage_signal = (weighted_usage_count)**0.5
                        feedback_signal = (float(s.weighted_pos_feedback or 0.0) - float(s.weighted_neg_feedback or 0.0))
                        structural_signal = (float(item.get("centrality_score") or 0.0) + float(item.get("richness") or 0.0) + (0.5 if item.get("entity_like") else 0.0))
                        score = 0.35 * (usage_signal * recency) + 0.25 * success_rate + 0.2 * feedback_signal + 0.2 * structural_signal - 0.2 * (failure_count**0.5)
                        tbl.usage_count = usage_count
                        tbl.success_count = success_count
                        tbl.failure_count = failure_count
                        tbl.weighted_usage_count = weighted_usage_count
                        tbl.pos_feedback_count = pos_feedback_count
                        tbl.neg_feedback_count = neg_feedback_count
                        tbl.last_used_at = last_used_at
                        tbl.last_feedback_at = last_feedback_at
                        tbl.success_rate = round(success_rate, 4)
                        tbl.score = float(round(score, 6))
                        scored.append((tbl.score or 0.0, tbl))
                    else:
                        structural_signal = (float(item.get("centrality_score") or 0.0) + float(item.get("richness") or 0.0) + (0.5 if item.get("entity_like") else 0.0))
                        score = 0.1 * structural_signal
                        tbl.score = float(round(score, 6))
                        scored.append((tbl.score or 0.0, tbl))
                else:
                    tables.append(tbl)

            # Default ordering by composite score when stats are present
            if with_stats:
                scored.sort(key=lambda x: x[0], reverse=True)
                tables = [t for (_, t) in scored]

            # Apply alternate sorts if requested
            try:
                if sort == "alpha":
                    tables.sort(key=lambda t: (t.name or '').lower())
                elif sort == "usage":
                    tables.sort(key=lambda t: (getattr(t, 'weighted_usage_count', 0.0) or 0.0, getattr(t, 'usage_count', 0) or 0), reverse=True)
                elif sort == "centrality":
                    def _cent(t):
                        di = float(getattr(t, 'degree_in', 0.0) or 0.0)
                        do = float(getattr(t, 'degree_out', 0.0) or 0.0)
                        cs = float(getattr(t, 'centrality_score', 0.0) or 0.0)
                        return di + do + cs
                    tables.sort(key=_cent, reverse=True)
            except Exception:
                pass

            # Apply table-level filters (name matching only - active filtering already done above)
            if table_names or name_patterns:
                name_set = set((table_names or []))
                patterns = []
                for p in (name_patterns or []):
                    try:
                        patterns.append(re.compile(p))
                    except Exception:
                        continue
                def _match(n: str) -> bool:
                    if name_set and n in name_set:
                        return True
                    for rp in patterns:
                        try:
                            if rp.search(n or ''):
                                return True
                        except Exception:
                            continue
                    return (not name_set) and (not patterns)
                filtered = []
                for t in tables:
                    if not _match(getattr(t, 'name', '')):
                        continue
                    filtered.append(t)
                tables = filtered

            # Apply top_k cap last
            if top_k is not None and top_k > 0:
                tables = tables[:top_k]

            ds_sections.append(
                TablesSchemaContext.DataSource(
                    info=DataSourceSummarySchema(
                        id=str(ds.id),
                        name=ds.name,
                        # Support both Pydantic schemas (ds.type) and ORM objects (ds.connections[0].type)
                        type=getattr(ds, 'type', None) or (ds.connections[0].type if getattr(ds, 'connections', None) else None),
                        # Prefer the richer human-written description when available; fallback to context
                        context=(getattr(ds, 'description', None) or getattr(ds, 'context', None)),
                    ),
                    tables=tables,
                )
            )

        return TablesSchemaContext(data_sources=ds_sections)

    # Backward-compatibility helpers (temporary; will be removed after full migration)
    async def get_data_source_count(self) -> int:
        data_sources = getattr(self.report, 'data_sources', []) or []
        return len(data_sources)

    async def get_file_count(self) -> int:
        files = getattr(self.report, 'files', []) or []
        return len(files)