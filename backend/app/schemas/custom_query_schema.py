from typing import Any, List, Optional

from pydantic import BaseModel, Field


class CustomQueryCreate(BaseModel):
    name: str
    definition_sql: str
    # The agent it was created from — activated there only. Every other agent on
    # the connection gets an inactive row, same as a regular table.
    activate_for_datasource_id: Optional[str] = None
    description: Optional[str] = None
    refresh_schedule_mode: str = "interval"   # 'interval' | 'time'
    refresh_interval_minutes: Optional[int] = 60
    refresh_at_time: Optional[str] = None      # "HH:MM"


class CustomQueryUpdate(BaseModel):
    name: Optional[str] = None
    definition_sql: Optional[str] = None
    description: Optional[str] = None
    refresh_schedule_mode: Optional[str] = None
    refresh_interval_minutes: Optional[int] = None
    refresh_at_time: Optional[str] = None


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

    class Config:
        from_attributes = True

    @classmethod
    def from_model(cls, cq, active_agent_count: int = 0, next_run_at=None) -> "CustomQuerySchema":
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
        )
