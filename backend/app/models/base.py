from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, DateTime, UUID
from sqlalchemy.orm import declared_attr
import uuid
from datetime import datetime

Base = declarative_base()

class SoftDeleteMixin:
    @declared_attr
    def deleted_at(cls):
        return Column(DateTime, default=None)

    @classmethod
    def get_query(cls, query, **kwargs):
        return query.filter(cls.deleted_at.is_(None))

class BaseSchema(Base, SoftDeleteMixin):
    __abstract__ = True

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime)

    def __repr__(self):
        return f'<{self.__class__.__name__}(id={self.id})>'

metadata = Base.metadata