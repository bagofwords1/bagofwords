"""Schemas for ``edit_list`` (training mode): change an existing Agent List's
schema. Partial update — only what is passed changes. The user approves the
change on a review card; the card says whether the edit is additive or
breaking (a breaking edit bumps the list version and marks old rows stale).
"""
from typing import List, Optional

from pydantic import BaseModel, Field

from app.ai.tools.schemas.create_list import ListFieldSpec, ListFieldType, ListToolOutput  # noqa: F401


class ListFieldUpdate(BaseModel):
    """Change one existing field, addressed by its CURRENT name. Only the
    attributes you pass change; a rename keeps the field's identity (and its
    rows)."""

    name: str = Field(..., description="Current field name.")
    new_name: Optional[str] = Field(None, description="New snake_case name.", max_length=48)
    type: Optional[ListFieldType] = Field(None, description="New type. Changing a type is a BREAKING change.")
    description: Optional[str] = Field(None, max_length=2000)
    required: Optional[bool] = Field(None, description="Making a field required is a BREAKING change.")
    enum: Optional[List[str]] = Field(None, description="Full replacement of the allowed values (enum fields). Removing a value is BREAKING; adding is additive.")
    unit: Optional[str] = Field(None, max_length=32)


class EditListInput(BaseModel):
    """Edit a list on one of this report's agents. Pass the list id (from
    create_list's output or the submit_<slug> tool's description), or its
    exact name / slug / submit tool name. Changes are reviewed on a card
    before they are saved."""

    list_id: str = Field(..., description="List id, or its exact name, slug or submit_<slug> tool name.")
    name: Optional[str] = Field(None, description="New list name.", min_length=1, max_length=120)
    description: Optional[str] = Field(None, description="New list description (replaces the current one).", max_length=4000)
    add_fields: Optional[List[ListFieldSpec]] = Field(None, description="New columns, appended in order. A new REQUIRED field is a breaking change.")
    update_fields: Optional[List[ListFieldUpdate]] = Field(None, description="Changes to existing columns, by current name.")
    remove_fields: Optional[List[str]] = Field(None, description="Names of columns to drop. Removing a field is a BREAKING change — its values are no longer shown.")
    key_field: Optional[str] = Field(None, description="Name of the new key field (string, integer, enum or date). Changing the key is a breaking change. Pass '' to clear it.")
    require_evidence: Optional[bool] = None
    keep_human_edits: Optional[bool] = None
    reason: Optional[str] = Field(
        None,
        description="ONE short sentence shown on the review card: why this change (e.g. 'User asked to track the signing date too').",
        max_length=300,
    )
