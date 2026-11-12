from typing import Any, List, Optional

from pydantic import BaseModel, Field


class CustomQueryRefreshConfig(BaseModel):
    """How a custom table refreshes beyond its schedule.

    Only meaningful fields need sending; on update, omitted ones keep their
    stored value.
    """

    # The column whose value grows when a row is added or changed.
    cursor_column: Optional[str] = None
    # 'timestamp' | 'date' | 'number' | 'text'. Needed up front only when the
    # query places the filter itself with {{since}} / {{until}}.
    cursor_kind: Optional[str] = None
    # Merge only: the column(s) that identify a row.
    unique_key: Optional[List[str]] = None
    # How far before the checkpoint each refresh re-reads, to catch rows that
    # arrive late. Seconds for dates/timestamps, plain units for numbers.
    lookback_seconds: Optional[int] = None
    lookback_units: Optional[int] = None
    # Where the first load starts (older rows are left out).
    initial_value: Optional[str] = None
    # 'hour' | 'day' | 'month': start each refresh at a whole period, for a
    # query that aggregates by one.
    grain: Optional[str] = None
    # Rebuild from scratch this often; the only way deleted rows disappear.
    full_refresh_every_days: Optional[int] = None
    # Keep the previous data when the row count falls by more than this share.
    row_drop_guard_pct: Optional[int] = None


class CustomQueryCreate(BaseModel):
    name: str
    definition_sql: str
    # Where the query runs, for connectors addressed per target (Power BI: the
    # semantic model, {datasetId, workspaceId, datasetName}). Omitted means the
    # source resolves it from the query text.
    target: Optional[dict] = None
    # The agent it was created from — activated there only. Every other agent on
    # the connection gets an inactive row, same as a regular table.
    activate_for_datasource_id: Optional[str] = None
    description: Optional[str] = None
    refresh_schedule_mode: str = "interval"   # 'interval' | 'time'
    refresh_interval_minutes: Optional[int] = 60
    refresh_at_time: Optional[str] = None      # "HH:MM"
    # 'full' (default) rebuilds every time; 'append' / 'merge' fetch only what
    # is new since the last refresh.
    refresh_mode: Optional[str] = None
    refresh_config: Optional[CustomQueryRefreshConfig] = None


class CustomQueryUpdate(BaseModel):
    name: Optional[str] = None
    definition_sql: Optional[str] = None
    # Sent as an empty dict to clear an explicit target; omitted keeps it.
    target: Optional[dict] = None
    description: Optional[str] = None
    refresh_schedule_mode: Optional[str] = None
    refresh_interval_minutes: Optional[int] = None
    refresh_at_time: Optional[str] = None
    refresh_mode: Optional[str] = None
    refresh_config: Optional[CustomQueryRefreshConfig] = None


class CustomQueryRlsUpdate(BaseModel):
    """Row-level security on one relation.

    Its own payload and its own route rather than a field on CustomQueryUpdate:
    changing a row policy is a security-relevant assertion and is audited as
    `connection.custom_query.rls_changed`, which only works if it cannot ride
    along inside an unrelated edit.
    """

    rls_enabled: bool
    rls_mode: str = "attribute"
    rls_policy: Optional[dict] = None
    rls_default_deny: bool = True


class CustomQueryRlsPreviewRequest(BaseModel):
    """Preview the relation as a specific member would see it."""

    user_id: str
    # The policy to try, so an admin can see the effect BEFORE saving. Omitted
    # means preview what is currently stored.
    rls_enabled: Optional[bool] = None
    rls_mode: Optional[str] = None
    rls_policy: Optional[dict] = None
    rls_default_deny: Optional[bool] = None


class CustomQueryRlsPreviewResponse(BaseModel):
    columns: List[dict] = []
    rows: List[List[Any]] = []
    row_limit: int
    # What the compiler decided and why — the part that turns "it returned no
    # rows" from a mystery into an answer.
    filtered: bool = False
    denied: bool = False
    column: Optional[str] = None
    allowed_values: List[str] = []
    reason: str = ""


class RlsPrincipal(BaseModel):
    id: str
    name: str


class CustomQueryRlsOptions(BaseModel):
    """What the policy editor can offer, drawn from what the org actually has."""

    attribute_keys: List[str] = []
    groups: List[RlsPrincipal] = []
    roles: List[RlsPrincipal] = []
    members: List[RlsPrincipal] = []


class CustomQueryPreviewRequest(BaseModel):
    definition_sql: str
    target: Optional[dict] = None
    # So a query using {{since}} / {{until}} can be previewed before saving.
    refresh_config: Optional[CustomQueryRefreshConfig] = None


class CustomQueryTarget(BaseModel):
    """One place a custom table on this connection can run against — a Power
    BI semantic model, with the tables the catalog knows in it."""

    datasetId: str
    workspaceId: Optional[str] = None
    workspaceName: Optional[str] = None
    datasetName: Optional[str] = None
    tables: List[str] = []


class CustomQueryPreviewResponse(BaseModel):
    columns: List[dict] = []
    rows: List[List[Any]] = []
    row_limit: int
    truncated: bool = False
    estimated_rows: Optional[int] = None
    estimated_bytes: Optional[int] = None
    # Bytes the SOURCE reads per refresh, where it can say in advance
    # (Snowflake, BigQuery). Distinct from estimated_bytes, which is the size
    # of the cached result — the two diverge hardest on exactly the queries
    # worth caching, and on a consumption-priced warehouse this is the one that
    # costs money, once per refresh, forever.
    scan_bytes: Optional[int] = None
    estimate_supported: bool = True
    estimate_note: str = ""
    # Set when the *unbounded* query would exceed a materialization budget. The
    # preview still returns rows — this tells the UI it cannot be saved as-is.
    budget_error: Optional[str] = None
    # Whether an incremental refresh can ask this source for only the new
    # rows. False: its query runs whole each time and what is new is taken
    # from the result — still incremental here, no lighter on the source.
    filters_at_source: bool = True


class CustomQuerySchema(BaseModel):
    id: str
    name: str
    connection_id: str
    description: Optional[str] = None
    definition_sql: Optional[str] = None
    columns: List[dict] = []
    no_rows: int = 0
    refresh_schedule_mode: Optional[str] = None
    refresh_interval_minutes: Optional[int] = None
    refresh_at_time: Optional[str] = None
    last_refreshed_at: Optional[str] = None
    last_refresh_status: Optional[str] = None
    last_refresh_error: Optional[str] = None
    last_refresh_ms: Optional[int] = None
    artifact_bytes: Optional[int] = None
    next_run_at: Optional[str] = None
    # How many agents have this relation activated — surfaced in the delete
    # confirmation so an admin knows the blast radius.
    active_agent_count: int = 0
    rls_enabled: bool = False
    rls_mode: Optional[str] = None
    rls_policy: Optional[dict] = None
    rls_default_deny: bool = True
    target: Optional[dict] = None
    refresh_mode: str = "full"
    refresh_config: Optional[dict] = None
    # Incremental tables: the highest cursor value the copy holds — the next
    # refresh reads from here (minus the lookback).
    checkpoint: Optional[dict] = None
    last_full_refresh_at: Optional[str] = None
    # A first load still in progress: windows finished so far.
    load_progress: Optional[dict] = None

    class Config:
        from_attributes = True

    @classmethod
    def from_model(cls, cq, active_agent_count: int = 0, next_run_at=None) -> "CustomQuerySchema":
        meta = cq.metadata_json if isinstance(getattr(cq, "metadata_json", None), dict) else {}
        target = meta.get("target") if isinstance(meta.get("target"), dict) else None
        state = getattr(cq, "refresh_state", None)
        state = state if isinstance(state, dict) else {}
        load = state.get("load") if isinstance(state.get("load"), dict) else None
        progress = None
        if load and load.get("windows"):
            windows = load["windows"]
            progress = {
                "windows_total": len(windows),
                "windows_done": sum(1 for w in windows if w.get("status") == "done"),
                "rows": load.get("rows") or 0,
                "source_rows": load.get("source_rows"),
            }
        config = getattr(cq, "refresh_config", None)
        return cls(
            id=str(cq.id),
            name=cq.name,
            connection_id=str(cq.connection_id),
            description=cq.description,
            definition_sql=cq.definition_sql,
            columns=cq.columns or [],
            no_rows=cq.no_rows or 0,
            refresh_schedule_mode=cq.refresh_schedule_mode,
            refresh_interval_minutes=cq.refresh_interval_minutes,
            refresh_at_time=cq.refresh_at_time,
            last_refreshed_at=cq.last_refreshed_at.isoformat()
            if cq.last_refreshed_at
            else None,
            last_refresh_status=cq.last_refresh_status,
            last_refresh_error=cq.last_refresh_error,
            last_refresh_ms=cq.last_refresh_ms,
            artifact_bytes=cq.artifact_bytes,
            active_agent_count=active_agent_count,
            next_run_at=next_run_at.isoformat() if next_run_at else None,
            rls_enabled=bool(getattr(cq, "rls_enabled", False)),
            rls_mode=getattr(cq, "rls_mode", None),
            rls_policy=getattr(cq, "rls_policy", None),
            rls_default_deny=bool(getattr(cq, "rls_default_deny", True)),
            target=target or None,
            refresh_mode=getattr(cq, "refresh_mode", None) or "full",
            refresh_config=config if isinstance(config, dict) else None,
            checkpoint=state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else None,
            last_full_refresh_at=state.get("last_full_refresh_at"),
            load_progress=progress,
        )


class CustomTableRunSummary(BaseModel):
    """One refresh of a custom table, as the history lists it."""

    id: str
    trigger: str
    mode: str
    status: str
    started_at: Optional[Any] = None
    finished_at: Optional[Any] = None
    elapsed_ms: Optional[int] = None
    # The cursor range covered: read from / checkpoint left behind.
    window_from: Optional[dict] = None
    window_to: Optional[dict] = None
    rows_read: Optional[int] = None
    rows_inserted: Optional[int] = None
    rows_updated: Optional[int] = None
    rows_deleted: Optional[int] = None
    row_count: Optional[int] = None
    artifact_bytes: Optional[int] = None
    error: Optional[str] = None
    triggered_by: Optional[dict] = None
    chunks_total: Optional[int] = None
    chunks_done: Optional[int] = None


class CustomTableRunList(BaseModel):
    runs: List[CustomTableRunSummary] = []
    has_more: bool = False
    next_before: Optional[str] = None


class CustomTableRunDetail(CustomTableRunSummary):
    """A run in full: the query as sent, each window, and the event log."""

    rendered_query: Optional[str] = None
    chunks: List[dict] = []
    events: List[dict] = []
    inputs: Optional[dict] = None
    schema_fingerprint: Optional[str] = None
