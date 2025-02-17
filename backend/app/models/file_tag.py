from sqlalchemy import Column, String, ForeignKey, UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

class FileTag(BaseSchema):
    __tablename__ = 'file_tags'

    key = Column(String, nullable=False)
    value = Column(String, nullable=False)
    file_id = Column(String(36), ForeignKey('files.id'), nullable=False)

    file = relationship("File", back_populates="file_tags")