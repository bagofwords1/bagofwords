from sqlalchemy import Column, String, Text, JSON, DateTime, ForeignKey, Table, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


# Association table for many-to-many relationship between entities and data sources
entity_data_source_association = Table(
    'entity_data_source_association',
    BaseSchema.metadata,
    Column('entity_id', String(36), ForeignKey('entities.id'), primary_key=True),
    Column('data_source_id', String(36), ForeignKey('data_sources.id'), primary_key=True),
    UniqueConstraint('entity_id', 'data_source_id', name='uq_entity_data_source')
)


class Entity(BaseSchema):
    __tablename__ = "entities"

    # Ownership and scoping
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    owner_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)

    # Core catalog shape
    type = Column(String, nullable=False)  # 'model' | 'metric'
    title = Column(String, nullable=False, default="")
    slug = Column(String, nullable=False)  # unique per organization
    description = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True, default=list)

    # Execution and preview
    code = Column(Text, nullable=False)  # single source of truth (SQL or expression)
    data = Column(JSON, nullable=True, default=dict)
    view = Column(JSON, nullable=True, default=dict)

    status = Column(String, nullable=False, default="draft")  # 'draft' | 'published'
    published_at = Column(DateTime, nullable=True)
    last_refreshed_at = Column(DateTime, nullable=True)

    # Relationships
    organization = relationship("Organization", back_populates="entities")
    owner = relationship("User", foreign_keys=[owner_id], lazy="selectin")
    data_sources = relationship(
        "DataSource",
        secondary=entity_data_source_association,
        back_populates="entities",
        lazy="selectin"
    )


