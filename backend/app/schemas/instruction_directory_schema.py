from typing import List, Optional

from pydantic import BaseModel

from app.schemas.base import UTCDatetime


class InstructionDirectoryBase(BaseModel):
    name: str
    #: Owning agent id. None => the Global instructions scope.
    data_source_id: Optional[str] = None
    #: Parent directory id. None => top-level within the scope.
    parent_id: Optional[str] = None


class InstructionDirectoryCreate(InstructionDirectoryBase):
    pass


class InstructionDirectoryUpdate(BaseModel):
    """Partial update. Only provided fields change.

    ``parent_id`` uses a sentinel: omit it to leave the parent unchanged; pass
    ``null`` to move the directory to the scope root. Because a plain optional
    can't distinguish "absent" from "explicit null", the route reads the raw
    body for parent_id — see ``fields_set``.
    """
    name: Optional[str] = None
    parent_id: Optional[str] = None
    position: Optional[int] = None


class InstructionDirectorySchema(BaseModel):
    id: str
    name: str
    data_source_id: Optional[str] = None
    parent_id: Optional[str] = None
    position: int = 0
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class InstructionPlacementSchema(BaseModel):
    """One (instruction -> directory) edge for the current scope."""
    instruction_id: str
    directory_id: str

    class Config:
        from_attributes = True


class InstructionDirectoryTreeSchema(BaseModel):
    """Everything the tree needs for one scope: the folders plus the placement
    edges of instructions in that scope. Instructions themselves come from the
    normal light list; this only says which folder each one lives in."""
    directories: List[InstructionDirectorySchema] = []
    placements: List[InstructionPlacementSchema] = []


class InstructionPlacementUpdate(BaseModel):
    """Move one instruction into a directory (or to the scope root).

    ``directory_id`` None => remove any placement in this scope (back to root).
    ``data_source_id`` identifies the scope when ``directory_id`` is None (it is
    derived from the directory otherwise). None scope => the Global group.
    """
    directory_id: Optional[str] = None
    data_source_id: Optional[str] = None
