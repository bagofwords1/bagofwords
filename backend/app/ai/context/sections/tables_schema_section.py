from typing import ClassVar, List, Optional, Literal, Dict, Any
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table as PromptTable


# Schema usage tracking models for context snapshots
class TableUsageItem(BaseModel):
    """Lightweight tracking of a single table's usage in context."""
    name: str
    score: Optional[float] = None
    usage_count: Optional[int] = None
    columns_count: int = 0
    selection_reason: str = "top_k_score"  # 'top_k_score' | 'mentioned' | 'all'


class DataSourceUsage(BaseModel):
    """Tracking of tables used from a single data source."""
    ds_id: str
    ds_name: str
    ds_type: str
    tables_used: List[TableUsageItem] = []
    tables_total: int = 0
    top_k_applied: int = 0


class SchemaUsageSnapshot(BaseModel):
    """Lightweight snapshot of which schemas/tables were used in context."""
    data_sources: List[DataSourceUsage] = []


class MCPToolItem(BaseModel):
    """Lightweight representation of an MCP tool for context injection."""
    name: str
    description: Optional[str] = None
    connection_id: Optional[str] = None
    connection_name: Optional[str] = None
    # Effective execution policy for the run's user (allow | ask | auto).
    # deny tools are excluded from context entirely.
    policy: Optional[str] = None
    # The tool's declared JSON Schema, admin-locked fields already stripped.
    # Carried so <mcp_tools> can render the argument shape inline instead of
    # sending the agent on a search_mcps round trip for it. The block is
    # rebuilt every turn, so a schema here never ages out of context — unlike
    # a search_mcps observation, which past-observation compaction minifies.
    input_schema: Optional[dict] = None


class FileScopeItem(BaseModel):
    """Compact descriptor for a file-source connection (network_dir / s3 /
    sharepoint / drive). Instead of enumerating every file as a <table> — which
    burns tokens and doesn't scale — we describe the SCOPE and point the agent
    at the retrieval tools."""
    connection_id: Optional[str] = None
    name: str
    type: str
    base: Optional[str] = None
    globs: List[str] = []
    index_mode: str = "content"      # none | metadata | content
    file_count: int = 0
    capped: bool = False
    sample: List[str] = []           # a few representative file ids
    topics: List[str] = []           # aggregate keywords (content mode)
    supports_search: bool = True     # native search API OR index_mode==content
    writable: bool = False
    per_user: bool = False           # per-user OAuth (each user reads as themselves)
    # True when this connector enforces a path/glob scope boundary (network_dir/
    # s3/sharepoint). False for token-scoped sources (OneDrive/Drive) whose only
    # boundary is the user's OAuth account — for those we render no <scope> and
    # no "else denied" language, because neither would be true.
    enforces_scope: bool = True


def _render_powerbi_cloud_metadata_xml(t: PromptTable) -> str:
    """Render the `powerbi` (cloud) metadata block for a table, if present.

    Emits datasetId/workspaceId/datasetName/tableName as attributes so the
    agent can execute DAX with explicit IDs (executeQueries is addressed by
    dataset GUID) or with the exact schema table name.
    """
    try:
        meta = t.metadata_json if isinstance(t.metadata_json, dict) else None
        pbi = (meta or {}).get("powerbi")
        if not isinstance(pbi, dict):
            return ""
        attrs = {}
        for k in ("datasetId", "workspaceId", "datasetName", "tableName"):
            v = pbi.get(k)
            if v is not None and v != "":
                attrs[k] = str(v)
        # Row-level security. The agent cannot detect filtering from results —
        # a row-filtered query returns HTTP 200 with fewer rows, identical to a
        # genuinely small result — so this flag is the only signal that totals
        # describe what THIS user can see rather than the whole organization.
        if pbi.get("rowLevelSecurity"):
            attrs["rowLevelSecurity"] = "true"
        if not attrs:
            return ""
        return xml_tag("powerbi", "", attrs)
    except Exception:
        return ""


# Table-level metadata a connector's query path genuinely needs, keyed by the
# `metadata_json` namespace. Allowlisted per namespace on purpose: these blobs
# also carry bulk (rowCount, dashboards[], fields_sampled) that would be noise.
#
# Only entries whose value is NOT recoverable from `Table.name`/`description`
# belong here — most connectors need nothing:
#   - every SQL client builds `name=fqn`, so schema/catalog/dataset is already there
#   - qlik_sense / sisense / businessobjects resolve their object from the table
#     NAME inside execute_query, so their ids are redundant
#   - infor_olap / sap_bw `cubeUniqueName` is literally f"[{cube}]" and `cube` is
#     the second segment of `name`, so it's derivable
#   - splunk writes index/sourcetype into `description`; oracle_bi and
#     sap_datasphere put their qualifier in `name`
_TABLE_META_KEYS: dict[str, tuple[str, ...]] = {
    # TableauClient.execute_query(datasource_luid, ...) — LUID is a REQUIRED
    # positional the agent must supply; `name` is "{project}/{datasource}".
    "tableau": ("datasourceLuid",),
    # AnalysisServicesClient's system prompt instructs the agent to pick MDX vs
    # DAX from modelType — it can't do that if modelType isn't in context.
    "analysis_services": (
        "modelType", "supportsDax", "preferredDialect",
        "entity_contents", "data_category",
    ),
}

# Flat (non-namespaced) table metadata worth surfacing, keyed by connector.
# Prometheus stores these at the top level of metadata_json.
_FLAT_META_KEYS: tuple[str, ...] = (
    # counter vs gauge decides whether rate() is required — querying a counter
    # without it returns meaningless monotonic values. `unit` decides whether a
    # number is seconds or bytes. Neither is reliably in the metric name.
    "metric_type",
    "unit",
)

# Column-level metadata keys to surface beyond kind/role.
#
# `unique_name` is the MDX/DAX identifier for XMLA sources (analysis_services,
# sap_bw, infor_olap): TableColumn.name is the human CAPTION ("Category"), while
# MDX needs the bracketed identifier ("[Product].[Category]"), which is not
# derivable from it. All three clients' system prompts tell the agent to
# reference `metadata.unique_name`, so it must actually be present.
#
# `returns` is a measure's result type. A measure is invoked by name and its
# definition is often unreadable through the REST metadata, so the return type
# is the only thing telling the agent whether it yields a count or a currency.
#
# `hidden` marks a column the model hides from report authors. It stays fully
# queryable — hidden is where join keys live — so the agent must see it to join
# on it, and must know not to offer it as a report field.
#
# `relationship_key` marks a column recovered from a relationship rather than
# from the column listing, so the agent knows it is a join key.
#
# This allowlist is the LAST gate before the prompt: a key absent here never
# reaches the model no matter what discovery captured or persistence stored.
_COLUMN_META_KEYS: tuple[str, ...] = (
    "unique_name", "returns", "hidden", "relationship_key",
    "format_string", "data_category", "display_folder", "sort_by_column",
    "summarize_by", "contents",
)


def _render_analysis_services_semantics_xml(t: PromptTable) -> str:
    """Render Tabular semantics that cannot be represented as PK/FK tags."""
    try:
        meta = t.metadata_json if isinstance(t.metadata_json, dict) else None
        model = (meta or {}).get("analysis_services")
        if not isinstance(model, dict):
            return ""
        parts: List[str] = []
        for hierarchy in model.get("hierarchies") or []:
            attrs = {"name": str(hierarchy.get("name") or "")}
            if hierarchy.get("description"):
                attrs["description"] = str(hierarchy["description"])
            if hierarchy.get("displayFolder"):
                attrs["display_folder"] = str(hierarchy["displayFolder"])
            if hierarchy.get("hidden"):
                attrs["hidden"] = "true"
            levels = []
            for level in hierarchy.get("levels") or []:
                level_attrs = {"name": str(level.get("name") or "")}
                if level.get("column"):
                    level_attrs["column"] = str(level["column"])
                if level.get("ordinal") is not None:
                    level_attrs["ordinal"] = str(level["ordinal"])
                levels.append(xml_tag("level", "", level_attrs))
            parts.append(xml_tag("hierarchy", "\n".join(levels), attrs))
        for relationship in model.get("relationships") or []:
            if str(relationship.get("state") or "active").lower() != "inactive":
                continue
            attrs = {
                "name": str(relationship.get("name") or ""),
                "from_column": str(relationship.get("fromColumn") or ""),
                "to_table": str(relationship.get("toTable") or ""),
                "to_column": str(relationship.get("toColumn") or ""),
                "state": "inactive",
            }
            if relationship.get("crossFilteringBehavior"):
                attrs["cross_filtering"] = str(relationship["crossFilteringBehavior"])
            parts.append(xml_tag("relationship", "", attrs))
        return xml_tag("tabular_semantics", "\n".join(parts)) if parts else ""
    except Exception:
        return ""


def _render_semantic_model_xml(t: PromptTable) -> str:
    """Render a semantic view's logical tables and internal joins.

    A semantic view is ONE queryable object whose columns come from several base
    tables. The agent never writes these joins — the view resolves them — but it
    has to know they exist to understand that a dimension on one logical table
    can slice a metric on another, which is the entire premise of
    `SEMANTIC_VIEW(view DIMENSIONS ... METRICS ...)`. Without them the columns
    look like one flat object and there is no basis for combining them.
    """
    try:
        meta = t.metadata_json if isinstance(t.metadata_json, dict) else None
        model = (meta or {}).get("semantic_model")
        if not isinstance(model, dict):
            return ""
        parts = []
        for lt in model.get("tables") or []:
            attrs = {"alias": str(lt.get("alias", ""))}
            if lt.get("base_table"):
                attrs["base_table"] = str(lt["base_table"])
            if lt.get("primary_key"):
                attrs["primary_key"] = ", ".join(lt["primary_key"])
            parts.append(xml_tag("logical_table", "", attrs))
        for rel in model.get("relationships") or []:
            attrs = {
                "from_table": str(rel.get("from_table", "")),
                "to_table": str(rel.get("to_table", "")),
            }
            if rel.get("from_columns"):
                attrs["from_columns"] = ", ".join(rel["from_columns"])
            if rel.get("to_columns"):
                attrs["to_columns"] = ", ".join(rel["to_columns"])
            parts.append(xml_tag("join", "", attrs))
        if not parts:
            return ""
        return xml_tag("semantic_model", "\n".join(parts))
    except Exception:
        return ""


def _render_source_metadata_xml(t: PromptTable) -> str:
    """Render connector-specific table metadata the agent needs to query.

    Namespaced blobs (see `_TABLE_META_KEYS`) render as `<tableau .../>`;
    flat keys (see `_FLAT_META_KEYS`) render as `<source_meta .../>`.
    Returns "" when the table carries nothing relevant.
    """
    try:
        meta = t.metadata_json if isinstance(t.metadata_json, dict) else None
        if not meta:
            return ""
        for ns, keys in _TABLE_META_KEYS.items():
            blob = meta.get(ns)
            if isinstance(blob, dict):
                attrs = {}
                for k in keys:
                    v = blob.get(k)
                    if v is not None and v != "":
                        attrs[k] = str(v).lower() if isinstance(v, bool) else str(v)
                if attrs:
                    return xml_tag(ns, "", attrs)
        attrs = {}
        for k in _FLAT_META_KEYS:
            v = meta.get(k)
            if v is not None and v != "":
                attrs[k] = str(v)
        if attrs:
            return xml_tag("source_meta", "", attrs)
        return ""
    except Exception:
        return ""


class TablesSchemaContext(ContextSection):
    # "Agent" is the product name for a data source; the model-facing schema
    # context uses the same vocabulary as the roster/tools (<agents>/<agent>).
    tag_name: ClassVar[str] = "agents"

    class DataSource(ContextSection):
        tag_name: ClassVar[str] = "agent"
        info: DataSourceSummarySchema
        tables: List[PromptTable] = []
        mcp_tools: List[MCPToolItem] = []
        file_scopes: List[FileScopeItem] = []
        # Connections dropped from `tables`/`file_scopes` because their backing
        # Connection.is_active flag is False (a cached "unreachable" health
        # signal). Retained so the render can KEEP the data source — a live
        # sibling connection must not vanish just because one connection is down —
        # and tell the model which connections are temporarily unavailable,
        # instead of silently omitting the whole agent. Each item: {name, type}.
        unhealthy_connections: List[Dict[str, Any]] = []

        # For a `browser` connection: {"url_patterns": [...], "allow_downloads": bool}.
        # A browser agent has no tables/mcp_tools/file_scopes (its tools are
        # builtin, capability-gated), so without this it would render empty and be
        # dropped from the schema context — leaving the model unaware it can browse
        # and, worse, unaware of WHICH URLs are in scope (so it guesses wrong ones).
        browser_scope: Optional[Dict[str, Any]] = None

        # True when the planner registers this report's MCP tools natively, so
        # each one already carries its schema in the request's tools array.
        # Resolved report-wide by the builder — never recomputed here, or the two
        # sides could disagree about where the schema lives.
        native_mcp: bool = False

        # Below this many files, list them inline (cheap + lets the agent pick
        # directly); above it, emit only scope + sample + topics.
        _FILE_INLINE_THRESHOLD: ClassVar[int] = 15

        # Below this many MCP/custom-API tools, inline each tool's argument
        # schema in <mcp_tools>; above it the block would dominate the prompt
        # every turn, so those connections keep the search_mcps discovery hop.
        _MCP_INLINE_SCHEMA_MAX: ClassVar[int] = 40

        # Short, human-readable explanations of each agent (data source)
        # publishing-status value so the planner understands what the status
        # MEANS, not just the bare token. Kept terse — goes into every block.
        _PUBLISH_STATUS_DESCRIPTIONS: ClassVar[dict] = {
            "published": "live and available to everyone with access",
            "draft": "still being configured by builders; not yet released to consumers",
            "disabled": "turned off and excluded from normal use",
        }

        # Reliability-loop lifecycle, orthogonal to publish_status. Only
        # "training" is surfaced — it's the one value that changes the planner's
        # clarify posture (published + training → "propose, don't ask"). "ok"
        # (production) and "development" (which already implies publish=draft)
        # add no signal the publishing status doesn't already carry.
        _RELIABILITY_STATUS_DESCRIPTIONS: ClassVar[dict] = {
            "training": "live, but still being actively improved — prefer proposing an instruction over asking",
        }

        def _render_status_xml(self) -> str:
            """Render the agent's status block.

            Surfaces the manager-set publishing lifecycle (published/draft/
            disabled) so the planner knows what the status means and can caveat
            its answers. When the source is also in the reliability loop's
            "training" stage, emit that too — it gives the planner a distinct,
            lower-friction clarify posture (see the clarify protocol).
            """
            publish = (getattr(self.info, 'publish_status', None) or '').strip().lower()
            if not publish:
                return ""
            desc = self._PUBLISH_STATUS_DESCRIPTIONS.get(publish, "")
            parts = [xml_tag("publishing", xml_escape(desc), {"value": publish})]
            reliability = (getattr(self.info, 'reliability_status', None) or '').strip().lower()
            rel_desc = self._RELIABILITY_STATUS_DESCRIPTIONS.get(reliability)
            if rel_desc:
                parts.append(xml_tag("reliability", xml_escape(rel_desc), {"value": reliability}))
            return xml_tag("status", "\n".join(parts))

        def _group_tables_by_connection(self) -> dict:
            """Group tables by (connection_id, connection_name).

            The name is part of the key because one physical connection can
            expose two client identities: the live source and its ``::fast``
            sibling serving materialized custom queries. They share a
            connection_id, so keying on the id alone would merge them into one
            <connection> block under whichever name sorted first — and the coder
            maps that name onto a client_key, so the merged block would point
            half the tables at a client that cannot serve them.
            """
            from collections import defaultdict
            groups = defaultdict(list)
            for t in (self.tables or []):
                conn_id = getattr(t, 'connection_id', None) or 'default'
                conn_name = getattr(t, 'connection_name', None) or ''
                key = conn_id if conn_id == 'default' else (conn_id, conn_name)
                groups[key].append(t)
            return groups

        def _render_table_xml(self, t: PromptTable) -> str:
            """Render a single table to XML."""
            col_parts = []
            for c in (t.columns or []):
                attrs = f'name="{xml_escape(c.name)}" dtype="{xml_escape(c.dtype or "")}"'
                if getattr(c, 'description', None):
                    attrs += f' description="{xml_escape(c.description)}"'
                # Column role/kind from metadata (semantic views, PowerBI, Tableau)
                col_meta = getattr(c, 'metadata', None)
                if isinstance(col_meta, dict):
                    role = col_meta.get("kind") or col_meta.get("role")
                    if role:
                        attrs += f' role="{xml_escape(str(role).lower())}"'
                    for mk in _COLUMN_META_KEYS:
                        mv = col_meta.get(mk)
                        if mv is None or mv == "":
                            continue
                        # Booleans render lowercase so the attribute reads as
                        # XML (hidden="true"), not as Python (hidden="True").
                        mv = str(mv).lower() if isinstance(mv, bool) else str(mv)
                        attrs += f' {mk}="{xml_escape(mv)}"'
                col_parts.append(f'<column {attrs}/>')
            cols = "\n".join(col_parts)

            pks = "\n".join(
                f'<pk name="{xml_escape(pk.name)}" dtype="{xml_escape(pk.dtype or "")}"/>'
                for pk in (t.pks or [])
            )
            fks = "\n".join(
                f'<fk column="{xml_escape(fk.column.name)}" '
                f'ref_table="{xml_escape(fk.references_name)}" '
                f'ref_column="{xml_escape(fk.references_column.name)}"/>'
                for fk in (t.fks or [])
            )
            metrics_lines: List[str] = []
            if any(v is not None for v in [t.score, t.usage_count, t.success_count, t.failure_count, t.success_rate, t.pos_feedback_count, t.neg_feedback_count, t.last_used_at, t.last_feedback_at]):
                if t.score is not None:
                    metrics_lines.append(f'<score value="{xml_escape(str(round(t.score, 6)))}"/>')
                if any(v is not None for v in [t.usage_count, t.success_count, t.failure_count]):
                    metrics_lines.append(
                        f'<usage count="{t.usage_count or 0}" success="{t.success_count or 0}" failure="{t.failure_count or 0}"/>'
                    )
                if t.success_rate is not None:
                    metrics_lines.append(f'<success_rate value="{xml_escape(str(round(t.success_rate, 6)))}"/>')
                if any(v is not None for v in [t.pos_feedback_count, t.neg_feedback_count]):
                    metrics_lines.append(
                        f'<feedback pos="{t.pos_feedback_count or 0}" neg="{t.neg_feedback_count or 0}"/>'
                    )
                if t.last_used_at:
                    metrics_lines.append(f'<last_used_at value="{xml_escape(t.last_used_at)}"/>')
                if t.last_feedback_at:
                    metrics_lines.append(f'<last_feedback_at value="{xml_escape(t.last_feedback_at)}"/>')
            metrics_xml = xml_tag("metrics", "\n".join(metrics_lines)) if metrics_lines else ""
            # Optional metadata (compact attributes). Tableau keeps its historical
            # <metadata> tag here; other connectors go through the shared
            # allowlist so this path and _render_topk_tables_full agree.
            metadata_xml = ""
            try:
                tj = (t.metadata_json or {}).get("tableau", {}) if isinstance(t.metadata_json, dict) else {}
                attrs = {}
                for k in ("datasourceLuid", "projectName", "name"):
                    v = tj.get(k)
                    if v is not None:
                        attrs[k] = v
                if attrs:
                    metadata_xml = xml_tag("metadata", "", attrs)
                else:
                    metadata_xml = _render_source_metadata_xml(t)
            except Exception:
                metadata_xml = ""
            # PowerBI Report Server metadata — surface queryability so the planner
            # knows pbix model tables, RDL reports, and shared datasets can be queried.
            pbi_xml = ""
            try:
                pbi = (t.metadata_json or {}).get("powerbi_report_server") if isinstance(t.metadata_json, dict) else None
                if isinstance(pbi, dict):
                    pbi_attrs = {}
                    for k in ("queryable", "report_type", "upstream_source", "report_id", "dataset_id"):
                        v = pbi.get(k)
                        if v is not None and v != "":
                            pbi_attrs[k] = str(v).lower() if isinstance(v, bool) else str(v)
                    pbi_note = pbi.get("query_note")
                    pbi_inner = xml_escape(pbi_note) if pbi_note else ""
                    if pbi_attrs or pbi_inner:
                        pbi_xml = xml_tag("powerbi_report_server", pbi_inner, pbi_attrs)
            except Exception:
                pbi_xml = ""
            # Power BI (cloud) metadata — the executeQueries endpoint is addressed
            # by dataset GUID, so the agent needs datasetId/workspaceId (or the
            # exact schema table name) to run DAX. Without these it has no way to
            # resolve the dataset and ends up asking the user for the GUID.
            pbi_cloud_xml = _render_powerbi_cloud_metadata_xml(t)
            # Add query instructions for semantic views
            is_semantic_view = isinstance(t.metadata_json, dict) and t.metadata_json.get("type") == "semantic_view"
            note_xml = ""
            if is_semantic_view:
                note_xml = xml_tag("note", "Snowflake Semantic View: query with SELECT * FROM SEMANTIC_VIEW(view_name DIMENSIONS dim1, dim2 METRICS metric1, metric2 WHERE condition). Use DIMENSIONS for role=dimension columns, METRICS for role=measure/metric columns.")
            # pks/fks were computed here but never emitted, so this renderer —
            # the fallback used whenever a focused schema context is absent —
            # showed the agent columns with no way to know the tables join. It
            # then had to guess relationships or decline to combine tables.
            inner = "\n".join(filter(None, [
                note_xml,
                xml_tag("columns", cols),
                xml_tag("pks", pks) if pks else "",
                xml_tag("fks", fks) if fks else "",
                _render_semantic_model_xml(t),
                _render_analysis_services_semantics_xml(t),
                metadata_xml, pbi_xml, pbi_cloud_xml, metrics_xml,
            ]))
            table_attrs = {"name": t.name}
            # Mark semantic views
            if is_semantic_view:
                table_attrs["type"] = "semantic_view"
            if getattr(t, 'description', None):
                table_attrs["description"] = t.description
            # A BOW custom query is already materialized locally: querying it
            # costs the source nothing, so the agent should reach for it before
            # re-deriving the same figures from raw tables.
            if getattr(t, 'is_cached', False):
                table_attrs["cached"] = "true"
                if getattr(t, 'cached_as_of', None):
                    table_attrs["as_of"] = t.cached_as_of
                # Without the next fire, `as_of` is unreadable: the same
                # timestamp means "current" on a daily schedule and "eight hours
                # behind" on an hourly one.
                if getattr(t, 'cached_next_refresh', None):
                    table_attrs["next_refresh"] = t.cached_next_refresh
            return xml_tag("table", inner, table_attrs)

        def _render_browser_xml(self) -> str:
            """Render a browser agent's scope: the allowed URL patterns and the
            builtin browser tools. Surfacing the patterns is what stops the model
            guessing an out-of-scope URL (e.g. the site's homepage) when the
            allowlist only permits a specific path."""
            scope = self.browser_scope or {}
            patterns = scope.get("url_patterns") or []
            if not patterns:
                return ""
            pat_xml = "\n".join(xml_tag("url", xml_escape(str(p))) for p in patterns[:30])
            allow_dl = "yes" if scope.get("allow_downloads", True) else "no"
            note = (
                "Browser agent: open pages with browser_navigate, then "
                "browser_snapshot / browser_extract to read, browser_act to interact, "
                "browser_vision to screenshot. You may ONLY visit URLs matching the "
                "patterns below — navigating elsewhere is refused."
            )
            inner = (
                xml_tag("note", note)
                + xml_tag("allowed_urls", pat_xml)
                + xml_tag("downloads_allowed", allow_dl)
            )
            return xml_tag("browser", inner)

        def _render_mcp_tools_xml(self) -> str:
            """Render MCP tools grouped by connection."""
            from collections import defaultdict
            groups = defaultdict(list)
            for tool in (self.mcp_tools or []):
                key = tool.connection_id or 'default'
                groups[key].append(tool)

            # Inline argument schemas when the catalog is small enough to
            # afford it. Above the threshold the block would dominate the
            # prompt every turn, so those fall back to search_mcps discovery.
            #
            # When native registration is on, each tool already carries its
            # schema in the request's tools array. Inlining here as well would
            # pay for the same bytes twice, every turn, so the block degrades to
            # an index and the note points at the real tools.
            total_tools = sum(len(v) for v in groups.values())
            inline_schemas = (not self.native_mcp) and total_tools <= self._MCP_INLINE_SCHEMA_MAX

            conn_parts = []
            has_gated = False
            for conn_id, tools in groups.items():
                tool_xmls = []
                for t in tools:
                    desc = xml_escape(t.description or "")
                    policy = getattr(t, 'policy', None)
                    policy_attr = ""
                    if policy and policy != "allow":
                        policy_attr = f' policy="{xml_escape(policy)}"'
                        has_gated = True
                    args_xml = ""
                    if inline_schemas and getattr(t, "input_schema", None):
                        try:
                            from app.ai.tools.mcp_schema import resolve_refs, render_schema_xml
                            args_xml = render_schema_xml(resolve_refs(t.input_schema))
                        except Exception:
                            args_xml = ""
                    if args_xml:
                        body = (desc + "\n" if desc else "") + args_xml
                        tool_xmls.append(
                            f'<tool name="{xml_escape(t.name)}"{policy_attr}>\n{body}\n</tool>'
                        )
                    else:
                        tool_xmls.append(f'<tool name="{xml_escape(t.name)}"{policy_attr}>{desc}</tool>')
                conn_name = tools[0].connection_name or 'unknown'
                conn_attrs = {"name": conn_name, "type": "mcp"}
                if conn_id != 'default':
                    conn_attrs["id"] = conn_id
                conn_parts.append(xml_tag("connection", "\n".join(tool_xmls), conn_attrs))
            if inline_schemas:
                # Schemas are right here, every turn. Say so explicitly —
                # otherwise the agent defensively calls search_mcps before each
                # execute_mcp anyway, which is a wasted planner turn plus a
                # wasted tool round trip per call.
                conn_parts.append(
                    "<note>The <arg> elements above ARE each tool's full argument schema — name, type, "
                    "requiredness, enums and nested shape. They are authoritative and always current, so "
                    "call execute_mcp directly; do NOT call search_mcps first. Match the declared type "
                    "exactly: an arg typed \"string\" takes a string even when its content is JSON "
                    "(serialize it), and an arg typed \"integer\" takes a number, not a formatted date.</note>"
                )
            elif self.native_mcp:
                conn_parts.append(
                    "<note>Each tool above is also available to you directly as a tool named "
                    "mcp__&lt;connection&gt;__&lt;tool&gt;, carrying its own argument schema — call it "
                    "directly rather than going through execute_mcp, and do not call search_mcps "
                    "for it. Use search_mcps + execute_mcp only for a tool listed here that has no "
                    "matching mcp__ tool available.</note>"
                )
            else:
                conn_parts.append(
                    "<note>Only tool names and descriptions are shown above, not their argument schemas. "
                    "Call search_mcps to get a tool's full input schema (exact argument names and types) "
                    "before calling execute_mcp — do not guess arguments.</note>"
                )
            if has_gated:
                conn_parts.append(
                    '<note>Tools marked policy="ask" pause the run for the user to approve the call '
                    '(they may decline — continue without the tool if so). Tools marked policy="auto" '
                    'are reviewed automatically before running and may be declined.</note>'
                )
            return xml_tag("mcp_tools", "\n".join(conn_parts))

        def _render_file_scope_xml(self, fs: "FileScopeItem") -> str:
            """A file connection as a compact scope descriptor — NOT a file
            enumeration. See FileScopeItem."""
            inner = []
            # Scope: the include-globs (the boundary). Tiny connections list
            # their files directly instead.
            if fs.file_count and fs.file_count <= self._FILE_INLINE_THRESHOLD and fs.sample:
                files_xml = "\n".join(f'<file>{xml_escape(n)}</file>' for n in fs.sample)
                inner.append(xml_tag("files", files_xml))
            else:
                # A <scope> only for connectors that ENFORCE a path/glob boundary
                # (network_dir/s3/sharepoint). Token-scoped sources (OneDrive/
                # Drive) have no such boundary — the user's OAuth account is the
                # limit — so a <scope> here would misrepresent what's enforced.
                if fs.enforces_scope:
                    if fs.globs:
                        patt = "\n".join(f'<pattern>{xml_escape(g)}</pattern>' for g in fs.globs)
                        inner.append(xml_tag("scope", patt))
                    else:
                        inner.append(xml_tag("scope", "<pattern>**</pattern>"))
                if fs.sample:
                    sm = "\n".join(f'<file>{xml_escape(n)}</file>' for n in fs.sample)
                    inner.append(xml_tag("sample", sm))
            # Index/state (self-closing).
            inner.append(
                f'<index mode="{xml_escape(fs.index_mode)}" files="{fs.file_count}" '
                f'capped="{"true" if fs.capped else "false"}"/>'
            )
            if fs.topics:
                inner.append(xml_tag("topics", xml_escape(", ".join(fs.topics[:12]))))
            # Per-user OAuth sources: the sample/count above (if any) reflect the
            # querying user's own account, and discovery is always live — so tell
            # the model not to treat an empty scope as "no files".
            if fs.per_user:
                inner.append(xml_tag(
                    "auth",
                    "per-user — each user reads with their own connected account. "
                    "The file list is per-user and fetched live; an empty scope here "
                    "does NOT mean no files — call list_files/search_files to see the "
                    "current user's files (they must have connected their account)."
                ))
            # Usage guidance — gate search_files on real capability, and the
            # "else denied" clause on real scope enforcement.
            denial = (" Access is limited to the scope above; anything else is denied."
                      if fs.enforces_scope else "")
            if fs.supports_search:
                usage = ("search_files to find by topic · list_files to browse · "
                         "read_file to read (use offset/length for large files)." + denial)
            else:
                usage = ("list_files (with name_pattern to filter) to browse · "
                         "read_file to read (use offset/length for large files). "
                         "No content search on this connection — discover by filename." + denial)
            inner.append(xml_tag("usage", usage))
            attrs = {"name": fs.name, "type": fs.type, "kind": "files"}
            if fs.connection_id:
                attrs["id"] = fs.connection_id
            if fs.writable:
                attrs["writable"] = "true"
            return xml_tag("connection", "\n".join(inner), attrs)

        def _render_unhealthy_connections_xml(self) -> str:
            """Flag connections that are currently unreachable (Connection.is_active
            is False). Their tables are withheld from the schema above — querying a
            dead connection just errors — but we surface them so the agent knows
            the data exists and is temporarily unavailable, rather than concluding
            the whole source has no data."""
            conns = self.unhealthy_connections or []
            if not conns:
                return ""
            items = []
            for c in conns:
                attrs = {"name": (c.get("name") or "unknown")}
                if c.get("type"):
                    attrs["type"] = c["type"]
                items.append(xml_tag(
                    "connection",
                    "temporarily unreachable — its tables are withheld until it "
                    "reconnects; do not attempt to query them.",
                    attrs,
                ))
            return xml_tag("unavailable_connections", "\n".join(items))

        def render(self) -> str:
            # Group tables by connection
            conn_groups = self._group_tables_by_connection()

            content_parts = []
            status_xml = self._render_status_xml()
            if status_xml:
                content_parts.append(status_xml)
            if self.info.context:
                content_parts.append(xml_tag("context", xml_escape(self.info.context)))

            # Check if we have multi-connection (more than one group, or the group isn't 'default')
            # File connections also count — they render as their own <connection>.
            has_multi_connection = (
                len(conn_groups) > 1
                or (len(conn_groups) == 1 and 'default' not in conn_groups)
                or bool(self.file_scopes and self.tables)
            )

            if has_multi_connection:
                # Render with nested <connection> tags
                for conn_id, tables in conn_groups.items():
                    if not tables:
                        continue
                    # Get connection info from first table
                    first_table = tables[0]
                    conn_name = getattr(first_table, 'connection_name', None) or 'unknown'
                    conn_type = getattr(first_table, 'connection_type', None) or 'unknown'

                    tables_xml = [self._render_table_xml(t) for t in tables]
                    conn_attrs = {"name": conn_name, "type": conn_type}
                    if any(getattr(x, 'is_cached', False) for x in tables):
                        conn_attrs["cached"] = "true"
                    if isinstance(conn_id, tuple):
                        conn_id = conn_id[0]
                    if conn_id != 'default':
                        conn_attrs["id"] = conn_id
                    content_parts.append(xml_tag("connection", "\n\n".join(tables_xml), conn_attrs))
            else:
                # Single connection or legacy mode - render tables directly (backward compatible)
                tables_xml = [self._render_table_xml(t) for t in (self.tables or [])]
                content_parts.append("\n\n".join(tables_xml))

            # File-source connections → compact scope descriptors (not tables).
            for fs in (self.file_scopes or []):
                content_parts.append(self._render_file_scope_xml(fs))

            # Render MCP tools if present
            if self.mcp_tools:
                mcp_parts = self._render_mcp_tools_xml()
                if mcp_parts:
                    content_parts.append(mcp_parts)

            # Build data_source attributes
            ds_attrs = {"name": self.info.name, "id": self.info.id}
            # Only include type if single connection (for backward compatibility)
            if not has_multi_connection and not self.file_scopes and self.info.type:
                ds_attrs["type"] = self.info.type

            return xml_tag(self.tag_name, "\n".join(content_parts), ds_attrs)

        # Compact renderers for gist/index/digest
        def _render_gist(self, columns_per_table: int = 2) -> str:
            table_tags: List[str] = []
            for t in (self.tables or []):
                # Per-table metrics: score, usage, columns count
                try:
                    score_val = getattr(t, 'score', None)
                    if score_val is not None:
                        try:
                            score_str = str(round(float(score_val), 2))
                        except Exception:
                            score_str = str(score_val)
                    else:
                        score_str = None
                except Exception:
                    score_str = None
                try:
                    usage_val = getattr(t, 'usage_count', None)
                    usage_str = str(int(usage_val)) if usage_val is not None else None
                except Exception:
                    usage_str = None
                try:
                    cols_count = len(t.columns or [])
                except Exception:
                    cols_count = 0

                meta_parts: List[str] = []
                if score_str is not None:
                    meta_parts.append(f"score: {score_str}")
                if usage_str is not None:
                    meta_parts.append(f"usage: {usage_str}")
                meta_parts.append(f"{cols_count} columns")
                meta_text = f"({', '.join(meta_parts)})" if meta_parts else None

                attrs = {"n": t.name}
                if meta_text:
                    attrs["meta"] = meta_text
                table_tags.append(xml_tag("t", "", attrs))
            # Skip empty data sources in gist
            if not table_tags:
                return ""
            label = xml_tag("label", "Sample top 10 tables for reference")
            inner = label + xml_tag("tables", "".join(table_tags))
            attrs = {"name": self.info.name, "type": self.info.type, "id": self.info.id, "sample": str(len(table_tags))}
            if self.info.context:
                attrs["desc"] = xml_escape(self.info.context)
            return xml_tag("data_source", inner, attrs)

        def _render_names(self) -> str:
            names = [getattr(t, 'name', '') for t in (self.tables or [])]
            # Skip empty data sources
            if not names:
                return ""
            # Ultra-compact: count + comma-separated list on one line
            label = xml_tag("label", "Index of all tables in database")
            payload = label + xml_tag("count", str(len(names))) + xml_tag("list", ", ".join(names))
            return xml_tag("data_source", payload, {"name": self.info.name, "type": self.info.type, "id": self.info.id})

        def _render_digest(self) -> str:
            first_five = [t.name for t in (self.tables or [])][:5]
            payload = xml_tag("count", str(len(self.tables or []))) + xml_tag("top", ", ".join(first_five))
            return xml_tag(self.tag_name, payload, {"name": self.info.name, "type": self.info.type, "id": self.info.id})

        def _render_topk_tables_full(self, top_k: int) -> str:
            """Render top K tables with full schema, grouped by connection if multi-connection."""
            # Cached relations are never sliced away by the cap. They rank first
            # (see schema_context_builder._cached_first), but this render path is
            # also reached from describe_tables with its own smaller k, and a
            # relation the planner cannot see is one it cannot prefer — it will
            # rebuild the same figures against the raw tables instead, which is
            # the load the cache exists to remove.
            all_tables = self.tables or []
            cached = [t for t in all_tables if getattr(t, 'is_cached', False)]
            rest = [t for t in all_tables if not getattr(t, 'is_cached', False)]
            top_tables = cached + rest[: max(0, top_k - len(cached))]
            if not top_tables:
                return ""

            # Group top tables by connection
            from collections import defaultdict
            conn_groups = defaultdict(list)
            for t in top_tables:
                conn_id = getattr(t, 'connection_id', None) or 'default'
                conn_name = getattr(t, 'connection_name', None) or ''
                key = conn_id if conn_id == 'default' else (conn_id, conn_name)
                conn_groups[key].append(t)

            has_multi_connection = len(conn_groups) > 1 or (len(conn_groups) == 1 and 'default' not in conn_groups)

            def render_table(t):
                col_parts = []
                for c in (t.columns or []):
                    col_attrs = f'name="{xml_escape(c.name)}" dtype="{xml_escape(c.dtype or "")}"'
                    if getattr(c, 'description', None):
                        col_attrs += f' description="{xml_escape(c.description)}"'
                    col_meta = getattr(c, 'metadata', None)
                    if isinstance(col_meta, dict):
                        role = col_meta.get("kind") or col_meta.get("role")
                        if role:
                            col_attrs += f' role="{xml_escape(str(role).lower())}"'
                        # Query identifiers that differ from the display name
                        # (XMLA unique_name). Without these the agent has only
                        # captions and cannot author valid MDX/DAX.
                        for mk in _COLUMN_META_KEYS:
                            mv = col_meta.get(mk)
                            if mv is None or mv == "":
                                continue
                            # Booleans render lowercase so the attribute reads
                            # as XML (hidden="true"), not Python (hidden="True").
                            mv = str(mv).lower() if isinstance(mv, bool) else str(mv)
                            col_attrs += f' {mk}="{xml_escape(mv)}"'
                    col_parts.append(f'<column {col_attrs}/>')
                cols = "\n".join(col_parts)
                pks = "\n".join(
                    f'<pk name="{xml_escape(pk.name)}" dtype="{xml_escape(pk.dtype or "")}"/>'
                    for pk in (t.pks or [])
                )
                fks = "\n".join(
                    f'<fk column="{xml_escape(fk.column.name)}" '
                    f'ref_table="{xml_escape(fk.references_name)}" '
                    f'ref_column="{xml_escape(fk.references_column.name)}"/>'
                    for fk in (t.fks or [])
                )
                attrs = {"name": t.name, "cols": str(len(t.columns or []))}
                is_sv = isinstance(getattr(t, 'metadata_json', None), dict) and t.metadata_json.get("type") == "semantic_view"
                if is_sv:
                    attrs["type"] = "semantic_view"
                if getattr(t, 'description', None):
                    attrs["description"] = t.description
                try:
                    if getattr(t, 'score', None) is not None:
                        attrs["score"] = str(round(float(getattr(t, 'score')), 2))
                except Exception:
                    pass
                try:
                    if getattr(t, 'usage_count', None) is not None:
                        attrs["usage"] = str(int(getattr(t, 'usage_count') or 0))
                except Exception:
                    pass
                try:
                    if getattr(t, 'referenced_instructions_count', None) is not None:
                        attrs["instructions"] = str(int(getattr(t, 'referenced_instructions_count')))
                except Exception:
                    pass
                note_xml = ""
                if is_sv:
                    note_xml = xml_tag("note", "Snowflake Semantic View: query with SELECT * FROM SEMANTIC_VIEW(view_name DIMENSIONS dim1, dim2 METRICS metric1, metric2 WHERE condition). Use DIMENSIONS for role=dimension columns, METRICS for role=measure/metric columns.")
                # PowerBI Report Server metadata — surface queryability so the planner
                # knows pbix model tables / RDL reports / datasets are queryable here.
                pbi_xml = ""
                try:
                    pbi = (t.metadata_json or {}).get("powerbi_report_server") if isinstance(getattr(t, 'metadata_json', None), dict) else None
                    if isinstance(pbi, dict):
                        pbi_attrs = {}
                        for k in ("queryable", "report_type", "upstream_source", "report_id", "dataset_id"):
                            v = pbi.get(k)
                            if v is not None and v != "":
                                pbi_attrs[k] = str(v).lower() if isinstance(v, bool) else str(v)
                        pbi_note = pbi.get("query_note")
                        pbi_inner = xml_escape(pbi_note) if pbi_note else ""
                        if pbi_attrs or pbi_inner:
                            pbi_xml = xml_tag("powerbi_report_server", pbi_inner, pbi_attrs)
                except Exception:
                    pbi_xml = ""
                pbi_cloud_xml = _render_powerbi_cloud_metadata_xml(t)
                # Connector-specific identifiers the query path needs (Tableau
                # datasourceLuid, SSAS modelType, Prometheus metric_type/unit).
                src_meta_xml = _render_source_metadata_xml(t)
                inner = "\n".join(filter(None, [note_xml, xml_tag("columns", cols), xml_tag("pks", pks) if pks else "", xml_tag("fks", fks) if fks else "", _render_semantic_model_xml(t), _render_analysis_services_semantics_xml(t), pbi_xml, pbi_cloud_xml, src_meta_xml]))
                if getattr(t, 'is_cached', False):
                    attrs["cached"] = "true"
                    if getattr(t, 'cached_as_of', None):
                        attrs["as_of"] = t.cached_as_of
                    if getattr(t, 'cached_next_refresh', None):
                        attrs["next_refresh"] = t.cached_next_refresh
                return xml_tag("table", inner, attrs)

            if has_multi_connection:
                # Render with nested <connection> tags
                conn_xml_parts = []
                for conn_id, tables in conn_groups.items():
                    if not tables:
                        continue
                    first_table = tables[0]
                    conn_name = getattr(first_table, 'connection_name', None) or 'unknown'
                    conn_type = getattr(first_table, 'connection_type', None) or 'unknown'

                    tables_xml = [render_table(t) for t in tables]
                    conn_attrs = {"name": conn_name, "type": conn_type}
                    if any(getattr(x, 'is_cached', False) for x in tables):
                        conn_attrs["cached"] = "true"
                    if isinstance(conn_id, tuple):
                        conn_id = conn_id[0]
                    if conn_id != 'default':
                        conn_attrs["id"] = conn_id
                    conn_xml_parts.append(xml_tag("connection", "\n".join(tables_xml), conn_attrs))
                return "\n".join(conn_xml_parts)
            else:
                # Single connection - render tables directly
                tables_xml = [render_table(t) for t in top_tables]
                return xml_tag("tables", "\n".join(tables_xml))

        def _render_names_index(self, index_limit: int = 200) -> str:
            tables = list(self.tables or [])
            if not tables:
                return ""
            # Build nested <item> elements with minimal metrics
            items_xml: List[str] = []
            cap = max(0, index_limit)
            for t in tables[:cap if cap > 0 else len(tables)]:
                attrs = {
                    "name": t.name,
                    "cols": str(len(getattr(t, 'columns', []) or [])),
                }
                try:
                    if getattr(t, 'score', None) is not None:
                        attrs["score"] = str(round(float(getattr(t, 'score')), 2))
                except Exception:
                    pass
                try:
                    if getattr(t, 'referenced_instructions_count', None) is not None:
                        attrs["instructions"] = str(int(getattr(t, 'referenced_instructions_count')))
                except Exception:
                    pass
                # Emit self-closing <item .../> to avoid empty inner newlines
                attrs_str = "".join(f' {k}="{xml_escape(str(v))}"' for k, v in attrs.items())
                items_xml.append(f"<item{attrs_str}/>")
            idx_attrs = {"count": str(len(tables))}
            if cap > 0 and len(tables) > cap:
                idx_attrs["truncated"] = "true"
            # Place each item on its own line for better readability
            return xml_tag("index", "\n".join(items_xml), idx_attrs)

    data_sources: List[DataSource] = []

    def render(self, format: Literal["full","gist","names","digest"] = "full", columns_per_table: int = 2) -> str:
        if format == "full":
            return xml_tag(self.tag_name, "\n\n".join(ds.render() for ds in self.data_sources or []))
        if format == "gist":
            # Compact gist with per-table metrics (score, usage, columns)
            return xml_tag(self.tag_name, "".join(ds._render_gist(columns_per_table) for ds in self.data_sources or []))
        if format == "names":
            return xml_tag(self.tag_name, "".join(ds._render_names() for ds in self.data_sources or []))
        if format == "digest":
            return xml_tag(self.tag_name, "".join(ds._render_digest() for ds in self.data_sources or []))
        return xml_tag(self.tag_name, "\n\n".join(ds.render() for ds in self.data_sources or []))

    def render_combined(self, top_k_per_ds: int = 10, index_limit: int = 200, include_index: bool = True) -> str:
        ds_chunks: List[str] = []
        for ds in (self.data_sources or []):
            sample_xml = ds._render_topk_tables_full(top_k_per_ds)
            index_xml = ds._render_names_index(index_limit) if include_index else ""
            # Render MCP tools for this data source
            mcp_xml = ds._render_mcp_tools_xml() if ds.mcp_tools else ""
            # File-source connections render as compact scope descriptors — they
            # are the ONLY content of a files agent (network_dir/s3/sharepoint/
            # drive), so without this a healthy files-only source would be dropped.
            file_xml = "\n".join(ds._render_file_scope_xml(fs) for fs in (ds.file_scopes or []))
            # Browser agents contribute their allowed-URL scope (no tables/tools).
            browser_xml = ds._render_browser_xml() if getattr(ds, "browser_scope", None) else ""
            # Connections withheld because they are unreachable. Surfacing them
            # keeps a multi-connection agent present when one connection is down.
            unhealthy_xml = ds._render_unhealthy_connections_xml()
            # Drop the data source only when it has NOTHING to contribute — no
            # live relational tables, index, MCP tools, file connections, browser
            # scope, or unhealthy connection to report. A source keeps its place as
            # long as ONE connection is live (or there is a down connection worth
            # flagging), so a dead DB connection never takes its healthy file
            # sibling — or the whole agent — down with it.
            if not (sample_xml or index_xml or mcp_xml or file_xml or browser_xml or unhealthy_xml):
                continue

            # Check if multi-connection (sample_xml will contain <connection> tags if so)
            has_multi_connection = ('<connection ' in sample_xml if sample_xml else False) or bool(file_xml)

            inner_parts: List[str] = []
            status_xml = ds._render_status_xml()
            if status_xml:
                inner_parts.append(status_xml)
            if getattr(ds.info, 'context', None):
                inner_parts.append(xml_tag("description", xml_escape(ds.info.context)))
            if sample_xml:
                inner_parts.append(xml_tag("sample", sample_xml, {"k": str(top_k_per_ds)}))
            if index_xml:
                inner_parts.append(index_xml)
            if file_xml:
                inner_parts.append(file_xml)
            if mcp_xml:
                inner_parts.append(mcp_xml)
            if browser_xml:
                inner_parts.append(browser_xml)
            if unhealthy_xml:
                inner_parts.append(unhealthy_xml)

            attrs = {
                "name": ds.info.name,
                "id": ds.info.id,
                "total_tables": str(len(getattr(ds, 'tables', []) or [])),
            }
            # Only include type for single-connection (backward compatibility)
            if not has_multi_connection and ds.info.type:
                attrs["type"] = ds.info.type
            # Ensure separation between <sample> and <index>
            ds_chunks.append(xml_tag("data_source", "\n".join(inner_parts), attrs))
        return xml_tag(self.tag_name, "".join(ds_chunks))

    def get_usage_snapshot(self, top_k_per_ds: int = 10) -> SchemaUsageSnapshot:
        """
        Return a lightweight snapshot of which tables were used in context.
        
        This mirrors the selection logic from render_combined() to accurately
        track what the LLM actually received.
        
        Parameters
        ----------
        top_k_per_ds : int
            Number of top tables per data source (same as render_combined).
            
        Returns
        -------
        SchemaUsageSnapshot
            Compact tracking of used tables with scores and selection reasons.
        """
        ds_usages: List[DataSourceUsage] = []
        
        for ds in (self.data_sources or []):
            tables = list(ds.tables or [])
            tables_total = len(tables)
            
            # Get top K tables (same logic as _render_topk_tables_full)
            top_tables = tables[:max(0, top_k_per_ds)]
            
            tables_used: List[TableUsageItem] = []
            for t in top_tables:
                score_val = None
                try:
                    if getattr(t, 'score', None) is not None:
                        score_val = float(t.score)
                except Exception:
                    pass
                
                usage_val = None
                try:
                    if getattr(t, 'usage_count', None) is not None:
                        usage_val = int(t.usage_count)
                except Exception:
                    pass
                
                cols_count = len(getattr(t, 'columns', []) or [])
                
                tables_used.append(TableUsageItem(
                    name=t.name,
                    score=score_val,
                    usage_count=usage_val,
                    columns_count=cols_count,
                    selection_reason="top_k_score",
                ))
            
            ds_usages.append(DataSourceUsage(
                ds_id=ds.info.id,
                ds_name=ds.info.name,
                ds_type=ds.info.type,
                tables_used=tables_used,
                tables_total=tables_total,
                top_k_applied=top_k_per_ds,
            ))
        
        return SchemaUsageSnapshot(data_sources=ds_usages)
