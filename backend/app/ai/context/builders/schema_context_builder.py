"""
Schema Context Builder - builds TablesSchemaContext object for schemas
"""
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table as PromptTable, TableColumn as PromptTableColumn, ForeignKey as PromptForeignKey
from app.models.table_stats import TableStats
from app.models.organization import Organization
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
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

    async def build(self, include_inactive: bool = False, with_stats: bool = True, top_k: Optional[int] = None) -> TablesSchemaContext:
        """Return TablesSchemaContext built from report's data sources, with optional stats and top_k filtering."""

        ds_sections: List[TablesSchemaContext.DataSource] = []

        for ds in self.data_sources:
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
                    pks = getattr(base, 'pks', []) if base is not None else []
                    fks = getattr(base, 'fks', []) if base is not None else []
                    metadata_json = getattr(base, 'metadata_json', None) if base is not None else None
                    normalized.append({
                        "name": name,
                        "columns": columns,
                        "pks": pks,
                        "fks": fks,
                        "metadata_json": metadata_json,
                        "centrality_score": getattr(base, 'centrality_score', None) if base is not None else None,
                        "richness": getattr(base, 'richness', None) if base is not None else None,
                        "degree_in": getattr(base, 'degree_in', None) if base is not None else None,
                        "degree_out": getattr(base, 'degree_out', None) if base is not None else None,
                        "entity_like": getattr(base, 'entity_like', None) if base is not None else None,
                        "is_active": True,
                    })
            else:
                for t in ds_tables:
                    if not include_inactive and not getattr(t, 'is_active', False):
                        continue
                    columns = [{"name": col.get("name"), "dtype": col.get("dtype", "unknown")} for col in (getattr(t, 'columns', []) or [])]
                    normalized.append({
                        "name": getattr(t, 'name', ''),
                        "columns": columns,
                        "pks": getattr(t, 'pks', []) or [],
                        "fks": getattr(t, 'fks', []) or [],
                        "metadata_json": getattr(t, 'metadata_json', None),
                        "centrality_score": getattr(t, 'centrality_score', None),
                        "richness": getattr(t, 'richness', None),
                        "degree_in": getattr(t, 'degree_in', None),
                        "degree_out": getattr(t, 'degree_out', None),
                        "entity_like": getattr(t, 'entity_like', None),
                        "is_active": bool(getattr(t, 'is_active', False)),
                    })

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
                    is_active=bool(item.get("is_active", True)),
                    centrality_score=item.get("centrality_score"),
                    richness=item.get("richness"),
                    degree_in=item.get("degree_in"),
                    degree_out=item.get("degree_out"),
                    entity_like=item.get("entity_like"),
                    metadata_json=item.get("metadata_json"),
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

            if with_stats:
                scored.sort(key=lambda x: x[0], reverse=True)
                tables = [t for (_, t) in scored]
                if top_k is not None and top_k > 0:
                    tables = tables[:top_k]

            ds_sections.append(
                TablesSchemaContext.DataSource(
                    info=DataSourceSummarySchema(id=str(ds.id), name=ds.name, type=ds.type, context=getattr(ds, 'context', None)),
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