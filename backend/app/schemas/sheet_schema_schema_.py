from pydantic import BaseModel
from uuid import UUID


class SheetSchemaBase(BaseModel):
    pass

class SheetSchema(SheetSchemaBase):
    id: str
    sheet_name: str
    sheet_index: int
    schema: dict
