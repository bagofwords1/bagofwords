from sqlalchemy import Column, String, Text, JSON, DateTime, ForeignKey, Table, UniqueConstraint, Boolean, Integer
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
    original_data_model = Column(JSON, nullable=True, default=dict)
    view = Column(JSON, nullable=True, default=dict)

    status = Column(String, nullable=False, default="draft")  # 'draft' | 'published'
    published_at = Column(DateTime, nullable=True)
    pinned = Column(Boolean, nullable=False, default=False)

    last_refreshed_at = Column(DateTime, nullable=True)
    auto_refresh_enabled = Column(Boolean, nullable=False, default=False)
    auto_refresh_interval = Column(Integer, nullable=True)
    auto_refresh_interval_unit = Column(String, nullable=True)

    # Dual-status lifecycle management (suggestion/approval workflow)
    private_status = Column(String(50), nullable=True)  # draft, published, archived (null for global-only)
    global_status = Column(String(50), nullable=True)   # null, suggested, approved, rejected

    # Audit trail
    reviewed_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)

    # Provenance: link back to originating step (if created from a step)
    source_step_id = Column(String(36), ForeignKey('steps.id'), nullable=True)
    
    # AI provenance metadata
    trigger_reason = Column(String(255), nullable=True)
    ai_source = Column(String(50), nullable=True)  # e.g., 'completion', 'batch_job'

    # Relationships
    organization = relationship("Organization", back_populates="entities")
    owner = relationship("User", foreign_keys=[owner_id], lazy="selectin")
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_user_id], lazy="selectin")
    source_step = relationship("Step", foreign_keys=[source_step_id], lazy="selectin", uselist=False, back_populates="created_entity")
    data_sources = relationship(
        "DataSource",
        secondary=entity_data_source_association,
        back_populates="entities",
        lazy="selectin"
    )

    @property
    def entity_type(self) -> str:
        """Returns the type of entity based on status combination"""
        if self.private_status and not self.global_status:
            return "private"
        elif self.private_status and self.global_status == "suggested":
            return "suggested"
        elif not self.private_status and self.global_status == "approved":
            return "global"
        else:
            return "unknown"
    
    @property
    def is_private(self) -> bool:
        """Returns True if this is a private entity"""
        return bool(self.private_status and not self.global_status)
    
    @property
    def is_suggested(self) -> bool:
        """Returns True if this is a suggested entity"""
        return bool(self.private_status and self.global_status == "suggested")
    
    @property
    def is_global(self) -> bool:
        """Returns True if this is a global entity"""
        return bool(not self.private_status and self.global_status == "approved")
    
    @property
    def is_editable_by_user(self) -> bool:
        """Returns True if the entity can be edited by the user (only private)"""
        return self.is_private
    
    @property
    def can_be_suggested(self) -> bool:
        """Returns True if the entity can be suggested (only private)"""
        return self.is_private
    
    @property
    def can_be_withdrawn(self) -> bool:
        """Returns True if the suggestion can be withdrawn by user"""
        return self.is_suggested
    
    @property
    def can_be_reviewed(self) -> bool:
        """Returns True if the entity can be reviewed by admin"""
        return self.is_suggested


