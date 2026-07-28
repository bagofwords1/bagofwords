from typing import Any, List, Optional

from pydantic import BaseModel, Field


class CustomQueryCreate(BaseModel):
    name: str
    definition_sql: str
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


class CustomQueryPreviewRequest(BaseModel):
    definition_sql: str


class CustomQueryPreviewResponse(BaseModel):
    columns: List[dict] = []
    rows: List[List[Any]] = []
    row_limit: int
    truncated: bool = False
    estimated_rows: Optional[int] = None
    estimated_bytes: Optional[int] = None
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
    # How many agents have this relation activated — surfaced in the delete
    # confirmation so an admin knows the blast radius.
    active_agent_count: int = 0

    class Config:
        from_attributes = True

    @classmethod
    def from_model(cls, cq, active_agent_count: int = 0) -> "CustomQuerySchema":
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
        )
