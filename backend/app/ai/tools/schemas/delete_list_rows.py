"""Schemas for ``delete_list_rows``: remove rows from an Agent List after the
user confirms on a card. Rows are picked by ``_row_id`` (from querying the
list's bow table), by key-field value, or all at once (clear the list, keep
its schema). Deletion is permanent, like the list page's Delete button.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

MAX_ROWS_PER_CALL = 500

DeleteListRowsStatus = Literal[
    "deleted", "rejected", "timed_out", "invalid", "no_match",
    "permission_denied", "list_not_found", "failed",
]


class DeleteListRowsInput(BaseModel):
    """Pick the rows with exactly one of ``row_ids``, ``keys`` or ``all``."""

    list_id: str = Field(..., description="List id, or its exact name, slug or submit_<slug> tool name.")
    row_ids: Optional[List[str]] = Field(
        None,
        description="Row ids to delete — the _row_id column of the list's bow table (query it first to pick rows by any condition).",
        max_length=MAX_ROWS_PER_CALL,
    )
    keys: Optional[List[str]] = Field(
        None,
        description="Key-field values of the rows to delete (lists with a key field only). Matched like submissions: case and extra spaces are ignored.",
        max_length=MAX_ROWS_PER_CALL,
    )
    all: bool = Field(False, description="Delete every row (clears the list, keeps its schema). Only when the user asked to clear or empty it.")
    reason: Optional[str] = Field(
        None,
        description="ONE short sentence shown on the confirmation card: why these rows (e.g. 'User asked to drop the duplicate Acme rows').",
        max_length=300,
    )

    @model_validator(mode="after")
    def _one_selector(self):
        picked = [n for n, v in (("row_ids", self.row_ids), ("keys", self.keys)) if v] + (["all"] if self.all else [])
        if len(picked) != 1:
            raise ValueError("pass exactly one of row_ids, keys or all=true")
        return self


class DeleteListRowsOutput(BaseModel):
    success: bool
    status: DeleteListRowsStatus
    message: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
    feedback: Optional[str] = Field(None, description="The user's note when they declined.")
    list_id: Optional[str] = None
    list_name: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    matched: int = Field(0, description="Rows that matched the selection and were shown on the card.")
    deleted: int = 0
    remaining: Optional[int] = Field(None, description="Rows left in the list after the delete.")
    not_found: List[str] = Field(default_factory=list, description="Requested row ids / keys that matched no row.")
    # The rows shown on the card (key + a few values). UI only.
    preview: List[Dict[str, Any]] = Field(default_factory=list)
