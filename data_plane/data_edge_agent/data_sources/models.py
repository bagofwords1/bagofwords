"""Schema types that cross the tunnel.

Kept structurally identical to `app.ai.prompt_formatters.Table` / `TableColumn`,
because the control plane deserializes what this sends into those.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TableColumn(BaseModel):
    name: str
    dtype: Optional[str] = None


class ForeignKey(BaseModel):
    column: TableColumn
    references_name: str
    references_column: TableColumn


class Table(BaseModel):
    name: str
    columns: list[TableColumn] = Field(default_factory=list)
    pks: list[TableColumn] = Field(default_factory=list)
    fks: list[ForeignKey] = Field(default_factory=list)
    is_active: bool = True
