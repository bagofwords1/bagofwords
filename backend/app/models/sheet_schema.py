
import os
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UUID, JSON
# from sqlalchemy.orm import relationship
from .base import BaseSchema
from sqlalchemy.orm import relationship


class SheetSchema(BaseSchema):
    __tablename__ = "sheet_schemas"

    schema = Column(JSON, index=False, default={})
    sheet_name = Column(String, index=True)
    sheet_index = Column(Integer, index=True)
    file_id = Column(String(36), ForeignKey("files.id"), nullable=False)
    file = relationship("File", back_populates="sheet_schemas")