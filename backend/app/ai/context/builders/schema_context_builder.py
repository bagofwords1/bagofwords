"""
Schema Context Builder - builds TablesSchemaContext object for schemas
"""
from typing import List, Optional, Dict, Any
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, func, and_
from app.ai.context.sections.tables_schema_section import TablesSchemaContext, MCPToolItem
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table as PromptTable, TableColumn as PromptTableColumn, ForeignKey as PromptForeignKey
from app.models.table_stats import TableStats
from app.models.organization import Organization
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.connection_table import ConnectionTable
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
        connection_ids: Optional[List[str]] = None,
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
            connection_ids: Filter to specific connections (UUID strings).
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

            # Canonical (org-level) source - load with connection relationships
            ds_tables_query = (
                select(DataSourceTable)
                .options(
                    selectinload(DataSourceTable.connection_table)
                    .selectinload(ConnectionTable.connection)
                )
                .where(DataSourceTable.datasource_id == str(ds.id))
            )
            # Push active filter into SQL to avoid loading thousands of inactive rows
            if active_only:
                ds_tables_query = ds_tables_query.where(DataSourceTable.is_active == True)
            # Apply connection filter if provided
            if connection_ids:
                conn_id_set = set(str(x) for x in connection_ids)
                ds_tables_query = ds_tables_query.join(
                    ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id
                ).where(ConnectionTable.connection_id.in_(conn_id_set))
            ds_tables_result = await self.db.execute(ds_tables_query)
            ds_tables = ds_tables_result.scalars().all()
            canonical_by_name: Dict[str, DataSourceTable] = {getattr(t, 'name', ''): t for t in ds_tables}

            # Choose source based on the user's CURRENT access to this data source.
            # auth_policy lives on the Connection (not the DataSource), so resolve
            # it from the linked connection — reading it off `ds` would always
            # default to 'system_only' and silently serve the full catalog.
            #   'user'   → this user's per-user overlay (their visible subset)
            #   'system' → owner/admin via service account → full canonical catalog
            #   'none'   → no proven access → no tables (don't leak the catalog)
            effective_auth = await self._resolve_user_access(ds)
            use_overlay = (effective_auth == "user")
            access_denied = (effective_auth == "none")

            # Normalize into a common shape for downstream rendering
            # Each entry: { name, columns: [{name,dtype}], pks: [{name,dtype}], fks: [fk], metadata_json, metrics, is_active }
            normalized: List[Dict[str, Any]] = []
            # Connections whose tables we withhold because the connection is
            # flagged unhealthy (Connection.is_active is False). Collected so the
            # renderer can keep the data source and flag them, instead of the
            # source silently vanishing when one of several connections is down.
            unhealthy_conns: Dict[str, Dict[str, Any]] = {}

            if access_denied:
                # User has no current access — emit the data source with no tables
                # rather than the canonical catalog they can't actually query.
                pass
            elif use_overlay:
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
                    # NOTE: do NOT use getattr(c, 'metadata') — c is a SQLAlchemy
                    # ORM instance whose `.metadata` is the declarative MetaData
                    # registry, not column metadata. It would fail PromptTableColumn
                    # validation and abort the whole schema build. UserDataSourceColumn
                    # carries neither description nor metadata, so emit None.
                    columns = [{"name": getattr(c, 'column_name', ''), "dtype": getattr(c, 'data_type', None), "description": None, "metadata": None} for c in overlay_cols]
                    base = canonical_by_name.get(name)
                    # Respect canonical table's is_active status (default False if not found)
                    canonical_is_active = bool(getattr(base, 'is_active', False)) if base is not None else False
                    # Skip inactive tables when active_only is True
                    if active_only and not canonical_is_active:
                        continue
                    pks = getattr(base, 'pks', []) if base is not None else []
                    fks = getattr(base, 'fks', []) if base is not None else []
                    metadata_json = getattr(base, 'metadata_json', None) if base is not None else None
                    # Extract connection info from the base table
                    conn_id = None
                    conn_name = None
                    conn_type = None
                    conn_is_active = True
                    if base is not None and getattr(base, 'connection_table', None):
                        ct = base.connection_table
                        if getattr(ct, 'connection', None):
                            conn_id = str(ct.connection.id)
                            conn_name = ct.connection.name
                            conn_type = ct.connection.type
                            conn_is_active = bool(getattr(ct.connection, 'is_active', True))
                    # Skip tables whose backing connection is flagged unhealthy.
                    # Connection.is_active is a cached reachability flag; a dead
                    # connection has no client (see construct_clients), so showing
                    # its tables would invite the model to query a source it
                    # cannot reach. Record it so the source is flagged (and kept)
                    # rather than silently emptied.
                    if active_only and not conn_is_active:
                        if conn_id:
                            unhealthy_conns.setdefault(conn_id, {"name": conn_name, "type": conn_type})
                        continue

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
                        "connection_id": conn_id,
                        "connection_name": conn_name,
                        "connection_type": conn_type,
                    })
            else:
                for t in ds_tables:
                    table_is_active = bool(getattr(t, 'is_active', False))
                    # Skip inactive tables when active_only is True
                    if active_only and not table_is_active:
                        continue
                    columns = [{"name": col.get("name"), "dtype": col.get("dtype", "unknown"), "description": col.get("description"), "metadata": col.get("metadata")} for col in (getattr(t, 'columns', []) or [])]

                    # Extract connection info
                    conn_id = None
                    conn_name = None
                    conn_type = None
                    conn_is_active = True
                    if getattr(t, 'connection_table', None):
                        ct = t.connection_table
                        if getattr(ct, 'connection', None):
                            conn_id = str(ct.connection.id)
                            conn_name = ct.connection.name
                            conn_type = ct.connection.type
                            conn_is_active = bool(getattr(ct.connection, 'is_active', True))
                    # Skip tables whose backing connection is flagged unhealthy
                    # (mirrors construct_clients, which builds no client for it).
                    # Record it so the source is flagged (and kept) rather than
                    # silently emptied when a sibling connection is still live.
                    if active_only and not conn_is_active:
                        if conn_id:
                            unhealthy_conns.setdefault(conn_id, {"name": conn_name, "type": conn_type})
                        continue

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
                        "connection_id": conn_id,
                        "connection_name": conn_name,
                        "connection_type": conn_type,
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
                    PromptTableColumn(name=c.get("name"), dtype=c.get("dtype"), description=c.get("description"), metadata=c.get("metadata"))
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
                    connection_id=item.get("connection_id"),
                    connection_name=item.get("connection_name"),
                    connection_type=item.get("connection_type"),
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

            # Pull file-source connections OUT of the table pool: they render as
            # compact scope descriptors, not per-file <table> rows — so they
            # never consume the top_k budget or bloat the prompt.
            file_scopes, tables = self._build_file_scopes(ds, tables)

            # Apply top_k cap last (to the remaining structured tables only)
            if top_k is not None and top_k > 0:
                tables = tables[:top_k]

            # Query MCP tools for this data source's MCP/custom_api connections
            mcp_tools = await self._build_mcp_tools(ds)

            ds_sections.append(
                TablesSchemaContext.DataSource(
                    info=DataSourceSummarySchema(
                        id=str(ds.id),
                        name=ds.name,
                        # Support both Pydantic schemas (ds.type) and ORM objects (ds.connections[0].type)
                        type=getattr(ds, 'type', None) or (ds.connections[0].type if getattr(ds, 'connections', None) else None),
                        # Prefer the richer human-written description when available; fallback to context
                        context=(getattr(ds, 'description', None) or getattr(ds, 'context', None)),
                        # Manager-set publishing lifecycle (published/draft/disabled).
                        publish_status=getattr(ds, 'publish_status', None),
                        # Reliability-loop lifecycle (ok/training/development) — a
                        # published source can still be "training", which sets a
                        # distinct clarify posture in the planner prompt.
                        reliability_status=getattr(ds, 'reliability_status', None),
                    ),
                    tables=tables,
                    mcp_tools=mcp_tools,
                    file_scopes=file_scopes,
                    # Only flag connections as unavailable when at least one other
                    # connection is live — if EVERY connection is down the source
                    # renders nothing and is dropped as before (an all-dead agent
                    # has nothing to offer). This is what keeps "some ok, some not"
                    # showing the ok ones and marking the not-ok ones.
                    unhealthy_connections=(
                        list(unhealthy_conns.values())
                        if (tables or file_scopes) else []
                    ),
                )
            )

        return TablesSchemaContext(data_sources=ds_sections)

    async def _resolve_user_access(self, ds) -> str:
        """Classify self.user's CURRENT access to data source `ds`.

        Returns 'user' (own creds → overlay), 'system' (owner/admin via service
        account → full catalog), or 'none' (no proven access → no tables).

        For non-user_required connections, or when there is no user in context,
        returns 'system' (the canonical catalog is the right thing to serve).
        Fails closed to 'none' for user_required so a stale overlay can't keep
        leaking tables after a user loses access.
        """
        conns = list(getattr(ds, 'connections', None) or [])
        conn = conns[0] if conns else None
        auth_policy = (getattr(conn, 'auth_policy', None) or 'system_only') if conn else 'system_only'
        if auth_policy != 'user_required' or self.user is None or conn is None:
            return 'system'
        try:
            from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
            status = await UserDataSourceCredentialsService().build_user_status_for_connection(
                self.db, conn, self.user, data_source=ds, live_test=False
            )
            return status.effective_auth or 'none'
        except Exception:
            return 'none'

    # File-source connectors and which of them have a native search API.
    _FILE_SOURCE_TYPES = {
        "network_dir", "s3", "sharepoint", "onedrive", "google_drive",
        "outlook_mail", "gmail_mail",
    }
    _NATIVE_SEARCH_TYPES = {
        "sharepoint", "onedrive", "google_drive", "outlook_mail", "gmail_mail",
    }
    # Token-scoped sources: no admin-side path/glob boundary — the user's OAuth
    # account IS the scope. Everything else enforces a path/glob scope.
    _TOKEN_SCOPED_TYPES = {"onedrive", "google_drive", "outlook_mail", "gmail_mail"}

    def _build_file_scopes(self, ds, tables):
        """Turn the data source's file-source connections into compact scope
        descriptors and remove their per-file rows from `tables`.

        Returns (file_scopes, remaining_tables)."""
        import json as _json
        import re as _re
        from collections import defaultdict
        from app.ai.context.sections.tables_schema_section import FileScopeItem

        conns = getattr(ds, 'connections', None) or []
        file_conns = [c for c in conns if getattr(c, 'type', None) in self._FILE_SOURCE_TYPES]
        if not file_conns:
            return [], tables

        file_conn_ids = {str(c.id) for c in file_conns}
        by_conn = defaultdict(list)
        remaining = []
        for t in (tables or []):
            cid = str(getattr(t, 'connection_id', '') or '')
            (by_conn[cid].append(t) if cid in file_conn_ids else remaining.append(t))

        scopes = []
        for c in file_conns:
            cfg = c.config
            if isinstance(cfg, str):
                try:
                    cfg = _json.loads(cfg or "{}")
                except Exception:
                    cfg = {}
            cfg = cfg or {}
            globs = [g.strip() for g in _re.split(r"[,\n]", str(cfg.get("include_globs") or "")) if g.strip()]
            index_mode = cfg.get("index_mode") or ("content" if cfg.get("index_content", True) else "metadata")
            base = (f"s3://{cfg.get('bucket')}/{cfg.get('prefix') or ''}"
                    if cfg.get("bucket") else cfg.get("root_path"))
            cid = str(c.id)
            ftabs = by_conn.get(cid, [])
            sample = [t.name for t in ftabs[:5] if getattr(t, 'name', None)]
            topics, seen = [], set()
            for t in ftabs:
                mj = getattr(t, 'metadata_json', None) or {}
                sub = (mj.get("network_dir") or mj.get("s3") or mj.get("graph")
                       or mj.get("google_drive") or {}) if isinstance(mj, dict) else {}
                for kw in (sub.get("keywords") or []):
                    k = str(kw).lower()
                    if k not in seen:
                        seen.add(k); topics.append(kw)
                    if len(topics) >= 12:
                        break
                if len(topics) >= 12:
                    break
            supports_search = (c.type in self._NATIVE_SEARCH_TYPES) or (index_mode == "content")
            # Per-user OAuth connection: each user reads with their own token, so
            # the cached sample/count (if any) is not a global truth. Flag it so
            # the descriptor tells the model discovery is per-user + live.
            per_user = (getattr(c, "auth_policy", None) == "user_required"
                        and "oauth" in (getattr(c, "allowed_user_auth_modes", None) or []))
            enforces_scope = c.type not in self._TOKEN_SCOPED_TYPES
            scopes.append(FileScopeItem(
                connection_id=cid, name=c.name, type=c.type, base=base,
                globs=globs, index_mode=index_mode, file_count=len(ftabs),
                capped=False, sample=sample, topics=topics,
                supports_search=supports_search, writable=bool(cfg.get("writable")),
                per_user=bool(per_user), enforces_scope=enforces_scope,
            ))
        return scopes, remaining

    async def _build_mcp_tools(self, ds) -> List[MCPToolItem]:
        """Query effective MCP/custom_api tools for a data source's connections.

        Enablement + policy are resolved through all three layers (per-agent
        overlay > connection default, then the run user's own preference).
        Tools whose effective policy is 'deny' — or which are disabled at
        either admin layer — are excluded so the planner never plans around
        tools it cannot call; 'ask'/'auto' tools are annotated instead.
        """
        from app.models.connection_tool import ConnectionTool
        from app.models.connection import Connection
        from app.models.data_source_connection_tool import DataSourceConnectionTool
        from app.services.tool_policy_service import (
            ToolPolicyService, resolve_effective_policy, normalize_tool_policy,
        )

        mcp_conn_ids = []
        for conn in (getattr(ds, 'connections', None) or []):
            if getattr(conn, 'type', None) in ('mcp', 'custom_api'):
                mcp_conn_ids.append(str(conn.id))
        if not mcp_conn_ids:
            return []

        try:
            result = await self.db.execute(
                select(ConnectionTool)
                .options(selectinload(ConnectionTool.connection))
                .where(ConnectionTool.connection_id.in_(mcp_conn_ids))
            )
            tools = result.scalars().all()

            overlay_rows = await self.db.execute(
                select(DataSourceConnectionTool).where(
                    DataSourceConnectionTool.data_source_id == str(ds.id)
                )
            )
            overlays = {str(o.connection_tool_id): o for o in overlay_rows.scalars().all()}

            user_prefs = {}
            if self.user is not None and tools:
                user_prefs = await ToolPolicyService().get_user_preferences(
                    self.db, str(self.user.id), [str(t.id) for t in tools]
                )

            items: List[MCPToolItem] = []
            for t in tools:
                overlay = overlays.get(str(t.id))
                is_enabled = overlay.is_enabled if overlay else t.is_enabled
                if not is_enabled:
                    continue
                admin_policy = overlay.policy if overlay else t.policy
                effective = resolve_effective_policy(admin_policy, user_prefs.get(str(t.id)))
                if effective == "deny":
                    continue
                items.append(
                    MCPToolItem(
                        name=t.name,
                        description=t.description,
                        connection_id=str(t.connection_id),
                        connection_name=getattr(t.connection, 'name', None),
                        policy=effective,
                    )
                )
            return items
        except Exception:
            return []

    # Backward-compatibility helpers (temporary; will be removed after full migration)
    async def get_data_source_count(self) -> int:
        data_sources = getattr(self.report, 'data_sources', []) or []
        return len(data_sources)

    async def get_file_count(self) -> int:
        files = getattr(self.report, 'files', []) or []
        return len(files)
