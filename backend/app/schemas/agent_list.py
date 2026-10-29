"""API schemas for Agent Lists."""
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

FieldType = Literal["string", "number", "integer", "boolean", "date", "enum"]
FieldMethod = Literal["extract", "classify", "derive"]

FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_FIELDS = 40
MAX_LISTS_PER_AGENT = 10


class ListFieldDef(BaseModel):
    """One column of a list. ``id`` is stable across renames."""

    id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(..., description="snake_case column name")
    type: FieldType = "string"
    description: Optional[str] = Field(default="", max_length=2000)
    required: bool = False
    enum: Optional[List[str]] = None
    unit: Optional[str] = Field(default=None, max_length=32)
    method: Optional[FieldMethod] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not FIELD_NAME_RE.match(v):
            raise ValueError(
                "must be snake_case: start with a lowercase letter, then lowercase letters, digits or _ (max 48)"
            )
        return v

    @model_validator(mode="after")
    def _enum(self):
        if self.type == "enum":
            vals = [str(x).strip() for x in (self.enum or []) if str(x).strip()]
            if not vals:
                raise ValueError("enum fields need at least one allowed value")
            if len(set(vals)) != len(vals):
                raise ValueError("enum values must be unique")
            self.enum = vals
        else:
            self.enum = None
        if self.method is None:
            self.method = "classify" if self.type in ("enum", "boolean") else "extract"
        if not self.id:
            self.id = "f_" + uuid.uuid4().hex[:10]
        return self


class ListSchemaIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(default="", max_length=4000)
    fields: List[ListFieldDef] = Field(..., min_length=1)
    key_field: Optional[str] = Field(
        default=None, description="Name (or id) of the field used to match/update existing rows."
    )
    require_evidence: bool = False
    allow_viewer_submissions: bool = False
    keep_human_edits: bool = False

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @model_validator(mode="after")
    def _fields(self):
        if len(self.fields) > MAX_FIELDS:
            raise ValueError(f"a list can have at most {MAX_FIELDS} fields")
        names = [f.name for f in self.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate field names: {', '.join(dupes)}")
        ids = [f.id for f in self.fields]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate field ids")
        if self.key_field:
            match = next((f for f in self.fields if f.name == self.key_field or f.id == self.key_field), None)
            if match is None:
                raise ValueError("key_field must name one of the fields")
            if match.type not in ("string", "integer", "enum", "date"):
                raise ValueError("key_field must be a text, integer, choice or date field")
        return self


class ListFieldOut(BaseModel):
    id: str
    name: str
    type: str
    description: Optional[str] = ""
    required: bool = False
    enum: Optional[List[str]] = None
    unit: Optional[str] = None
    method: Optional[str] = None


class AgentListOut(BaseModel):
    id: str
    data_source_id: str
    name: str
    slug: str
    description: Optional[str] = ""
    fields: List[ListFieldOut]
    key_field_id: Optional[str] = None
    key_field: Optional[str] = None
    require_evidence: bool = False
    allow_viewer_submissions: bool = False
    keep_human_edits: bool = False
    version: int = 1
    row_count: int = 0
    tool_name: str
    table_name: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    can_manage: bool = False
    # Present on PUT responses: what the edit did to the schema version.
    change: Optional[Literal["none", "additive", "breaking"]] = None


class RowOut(BaseModel):
    id: str
    key_value: Optional[str] = None
    values: Dict[str, Any]
    schema_version: int
    row_version: int
    locked_fields: List[str] = []
    report_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    stale: bool = False


class RowsPage(BaseModel):
    rows: List[RowOut]
    total: int
    offset: int
    limit: int


class RowPatchIn(BaseModel):
    row_version: int
    # {field name or id: new value (None clears)}
    fields: Dict[str, Any] = Field(default_factory=dict)
    unlock: List[str] = Field(default_factory=list, description="Field names/ids to unlock for agent updates.")


MAX_ROWS_PER_DELETE = 1000


class RowsDeleteIn(BaseModel):
    """Delete some rows (``row_ids``) or empty the list (``all``). The list
    itself — schema, tool, table name — is kept either way."""
    row_ids: List[str] = Field(default_factory=list, max_length=MAX_ROWS_PER_DELETE)
    all: bool = False

    @model_validator(mode="after")
    def _one_target(self):
        if self.all == bool(self.row_ids):
            raise ValueError("Pass either row_ids or all=true")
        return self


class RowsDeleteOut(BaseModel):
    deleted: int


class RevisionOut(BaseModel):
    id: str
    actor_type: str
    actor_user_id: Optional[str] = None
    actor_name: Optional[str] = None
    action: str
    report_id: Optional[str] = None
    changed: Dict[str, Any]
    created_at: Optional[datetime] = None
