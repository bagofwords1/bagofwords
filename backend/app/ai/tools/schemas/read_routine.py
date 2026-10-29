from typing import Optional

from pydantic import BaseModel, Field


class ReadRoutineInput(BaseModel):
    """Read the source of ONE stored procedure / function listed in a
    <routines> block of the schema context.

    - name: the routine's qualified name exactly as shown (e.g. "dbo.usp_load_sales")
    - connection_id: optional; disambiguates when the same name exists on more
      than one connection in scope
    """

    name: str = Field(..., description="Qualified routine name, e.g. 'schema.name', as shown in <routines>")
    connection_id: Optional[str] = Field(
        default=None,
        description="Optional connection ID (UUID) to disambiguate a name present on multiple connections",
    )


class ReadRoutineOutput(BaseModel):
    """The routine's source text, plus enough to locate it."""

    found: bool = Field(..., description="True if a routine with this name was found in scope")
    name: str = Field(..., description="Qualified routine name echoed back")
    connection_id: Optional[str] = Field(default=None, description="Connection the routine belongs to")
    connection_name: Optional[str] = Field(default=None, description="Connection display name")
    routine_type: Optional[str] = Field(default=None, description="'procedure' or 'function'")
    definition_available: bool = Field(
        default=False,
        description="False when the routine exists but its source is not visible to the indexing account",
    )
    source: Optional[str] = Field(default=None, description="The routine's source text, when readable")
    message: Optional[str] = Field(default=None, description="Human-readable note when source is unavailable")
