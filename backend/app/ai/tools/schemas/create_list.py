"""Schemas for ``create_list`` (training mode): define a typed Agent List.

A list is a per-agent table the agent fills through its ``submit_<slug>``
tool. The model proposes the schema here; a person approves it on a review
card before anything is written (same flow as ``create_demo_dataset``).
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ListFieldType = Literal["string", "number", "integer", "boolean", "date", "enum"]

LIST_TOOL_STATUSES = Literal[
    "created", "updated", "rejected", "timed_out", "invalid", "permission_denied",
    "needs_agent", "agent_not_found", "list_not_found", "limit_reached", "duplicate_name",
    "no_change", "failed",
]


class ListFieldSpec(BaseModel):
    """One column of the list. The description is the agent's extraction
    instruction for that column — write it for a future reader who has not
    seen the documents."""

    name: str = Field(
        ...,
        description="snake_case column name: lowercase letters, digits and _ (max 48), e.g. 'annual_value'.",
        max_length=48,
    )
    type: ListFieldType = Field(
        "string",
        description=(
            "string (free text), number (decimal), integer (whole number), boolean (yes/no), "
            "date (YYYY-MM-DD), enum (one of the allowed values in 'enum')."
        ),
    )
    description: Optional[str] = Field(
        "",
        description="What the value is and where it is usually found. This is the instruction the agent follows when filling the column.",
        max_length=2000,
    )
    required: bool = Field(False, description="True when every record must carry this value.")
    enum: Optional[List[str]] = Field(
        None,
        description="Allowed values — enum fields only. Keep them short and mutually exclusive.",
    )
    unit: Optional[str] = Field(None, description="Unit for number fields, e.g. 'USD', 'days', '%'.", max_length=32)


class CreateListInput(BaseModel):
    """Propose a new list on one of this report's agents. The user reviews the
    schema on a card and approves it (or sends feedback to revise). On approval
    the list is created and its ``submit_<slug>`` tool becomes available in this
    same session, so you can start filling it right away."""

    name: str = Field(..., description="Human list name, e.g. 'Contracts' or 'Supplier invoices'.", min_length=1, max_length=120)
    description: Optional[str] = Field(
        "",
        description="What one row represents and anything the agent should know when extracting (sources, conventions, exclusions).",
        max_length=4000,
    )
    fields: List[ListFieldSpec] = Field(..., description="The columns, in display order. 1–40 fields.", min_length=1, max_length=40)
    key_field: Optional[str] = Field(
        None,
        description=(
            "Name of the field that identifies a row (string, integer, enum or date type). Records with the same key "
            "UPDATE the existing row instead of adding a duplicate. Strongly recommended — e.g. 'counterparty', 'invoice_number'."
        ),
    )
    require_evidence: bool = Field(
        False,
        description="Require a verbatim source quote for every extracted value (recommended for document extraction).",
    )
    keep_human_edits: bool = Field(
        False,
        description="When a person edits a cell by hand, the agent never overwrites it.",
    )
    agent_id: Optional[str] = Field(
        None,
        description=(
            "Id of the agent (data source) that owns the list. Omit when this report has exactly one agent you manage; "
            "when several are attached, pass the id of the one the list belongs to."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description="ONE short sentence shown on the review card: why this list, grounded in what you saw (e.g. 'Each vendor contract carries renewal terms we need in one table').",
        max_length=300,
    )


class ListFieldOut(BaseModel):
    id: str
    name: str
    type: str
    description: Optional[str] = ""
    required: bool = False
    enum: Optional[List[str]] = None
    unit: Optional[str] = None
    key: bool = False


class ListToolOutput(BaseModel):
    """Shared output of create_list / edit_list."""

    success: bool
    status: LIST_TOOL_STATUSES
    message: Optional[str] = None
    feedback: Optional[str] = Field(None, description="The user's feedback when they declined the proposal.")
    errors: List[str] = Field(default_factory=list, description="Schema problems to fix before calling again.")
    list_id: Optional[str] = None
    list_name: Optional[str] = None
    slug: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    tool_name: Optional[str] = Field(None, description="The submit_<slug> tool that writes records into this list.")
    table_name: Optional[str] = Field(None, description="bow.<agent>.lists.<list> — queryable through create_data.")
    version: Optional[int] = None
    change: Optional[Literal["none", "additive", "breaking"]] = Field(
        None, description="edit_list only: what the edit did to the schema version."
    )
    fields: List[ListFieldOut] = Field(default_factory=list)
    key_field: Optional[str] = None
    row_count: int = 0
    agents: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="When the agent could not be resolved: the attached agents you may create lists on (id, name).",
    )
