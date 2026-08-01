from pydantic import BaseModel, field_validator
from typing import List, Optional, Literal
from datetime import datetime

from app.schemas.user_schema import UserSchema


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Project name cannot be empty")
        if len(v) > 120:
            raise ValueError("Project name is too long (max 120 characters)")
        return v


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    access: Optional[Literal["private", "org"]] = None
    # Project-local instructions (plain text, injected into agent context for
    # every report in the project). Sentinel: omit = unchanged, "" = clear.
    instructions: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Project name cannot be empty")
        if len(v) > 120:
            raise ValueError("Project name is too long (max 120 characters)")
        return v


class ProjectMiniSchema(BaseModel):
    """Lightweight embed for ReportSchema (the prompt-box chip and lists)."""
    id: str
    name: str
    color: Optional[str] = None

    class Config:
        from_attributes = True


def _connector_key_for_ds(conn) -> Optional[str]:
    """Connector brand key for a data source's first connection (icon lookup).
    Reuses the derivation in data_source_schema; never raises."""
    if conn is None:
        return None
    try:
        from app.schemas.data_source_schema import _connector_key_from_config
        return _connector_key_from_config(getattr(conn, "config", None))
    except Exception:
        return None


class ProjectAgentMini(BaseModel):
    id: str
    name: str
    # Icon inputs for the frontend DataSourceIcon (connection type, optional
    # connector brand key, optional per-agent icon override).
    type: Optional[str] = None
    connector_key: Optional[str] = None
    icon: Optional[str] = None

    class Config:
        from_attributes = True


class ProjectFileMini(BaseModel):
    id: str
    filename: str

    class Config:
        from_attributes = True


class ProjectSchema(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    instructions: Optional[str] = None
    access: Literal["private", "org"] = "private"
    user_id: str
    user: Optional[UserSchema] = None
    created_at: datetime
    updated_at: datetime
    # Per-caller context, filled by the service.
    report_count: int = 0
    member_count: int = 0
    # For the delete confirmation and the /projects index: how many of the
    # project's reports carry dashboards, and how many automations (scheduled
    # tasks + dashboard refreshes) run through them.
    dashboard_count: int = 0
    automation_count: int = 0
    is_owner: bool = False
    can_manage: bool = False
    # Defaults copied onto every new report created in this project.
    data_sources: list[ProjectAgentMini] = []
    files: list[ProjectFileMini] = []

    class Config:
        from_attributes = True


class ProjectListResponse(BaseModel):
    projects: List[ProjectSchema]


class ProjectMemberSchema(BaseModel):
    """A user who has been granted access to a project."""
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    permissions: List[str] = []


class ProjectMemberUpsert(BaseModel):
    user_id: str
    permissions: List[Literal["view", "manage"]] = ["view"]

    @field_validator("permissions")
    @classmethod
    def at_least_view(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("At least one permission is required")
        return v


class ProjectDataSourcesUpdate(BaseModel):
    """Replace the project's default agents."""
    data_source_ids: List[str] = []


class ProjectFilesUpdate(BaseModel):
    """Replace the project's default files."""
    file_ids: List[str] = []


class ProjectAutomationItem(BaseModel):
    """One automation of the project: 'task' = ScheduledPrompt and
    'refresh' = report.cron_schedule (both reached through reports.project_id),
    'trigger' = a standalone webhook bound to the project.

    A trigger has no single report — it spawns one per delivery — so
    ``report_id``/``cron_schedule`` are empty for that kind.
    """
    id: str
    kind: Literal["task", "refresh", "trigger"]
    report_id: Optional[str] = None
    report_title: Optional[str] = None
    label: str
    cron_schedule: str = ""
    is_active: bool = True


class ReportMoveRequest(BaseModel):
    """Bulk move: assign (or clear) the project for a set of owned reports."""
    report_ids: List[str]
    project_id: Optional[str] = None  # None/empty clears back to the root list
