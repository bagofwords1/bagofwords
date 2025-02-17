from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Enum
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
import enum
from app.models.file import File
from app.models.memory import Memory
from app.models.data_source import DataSource

class MentionType(enum.Enum):
    FILE = "FILE"
    MEMORY = "MEMORY"
    DATA_SOURCE = "DATA_SOURCE"

class Mention(BaseSchema):
    __tablename__ = 'mentions'

    type = Column(Enum(MentionType), nullable=False)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    object_id = Column(String(36), nullable=False)
    mention_content = Column(String, nullable=False)
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False)

    completion = relationship("Completion", back_populates="mentions")


@property
async def object(self):
    if self.type == MentionType.FILE:
        return await File.get(self.object_id)
    elif self.type == MentionType.MEMORY:
        return await Memory.get(self.object_id)
    elif self.type == MentionType.DATA_SOURCE:
        return await DataSource.get(self.object_id)
    return None