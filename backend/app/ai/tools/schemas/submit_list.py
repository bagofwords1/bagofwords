from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SubmitListInput(BaseModel):
    """Gateway input for list submissions.

    The model never sees this shape: each list is registered natively as
    ``submit_<slug>`` with the list's compiled schema, and the call is
    rewritten to ``submit_list`` (``list_id`` + ``records``) before execution.
    """

    list_id: str = Field(..., description="Id of the list to write to.")
    records: List[Dict[str, Any]] = Field(default_factory=list)


class SubmitListOutput(BaseModel):
    success: bool
    list_id: Optional[str] = None
    list_name: Optional[str] = None
    data_source_id: Optional[str] = None
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
