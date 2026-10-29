from typing import List, Optional

from pydantic import BaseModel, Field


class ListRoutinesInput(BaseModel):
    """Discover stored procedures / functions across the agents in scope.

    Unlike the <routines> block (which lists only the SELECTED/active routines),
    this returns ALL indexed routines — use it to scan or find one by name, then
    read its source with read_routine.
    """

    name: Optional[str] = Field(
        default=None,
        description="Optional case-insensitive substring to filter routine names (e.g. 'load', 'sales')",
    )
    connection_id: Optional[str] = Field(
        default=None, description="Optional connection ID (UUID) to scope the listing"
    )


class RoutineListItem(BaseModel):
    name: str
    routine_type: str
    arguments: Optional[str] = None
    returns: Optional[str] = None
    definition_available: bool = True
    is_active: bool = False
    connection_id: Optional[str] = None
    connection_name: Optional[str] = None


class ListRoutinesOutput(BaseModel):
    routines: List[RoutineListItem] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False
