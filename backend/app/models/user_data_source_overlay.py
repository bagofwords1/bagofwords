from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UserDataSourceTable(BaseSchema):
    __tablename__ = "user_data_source_tables"
    __table_args__ = (
        UniqueConstraint('data_source_id', 'user_id', 'table_name', name='uq_user_ds_table'),
    )

    data_source_id = Column(String(36), ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    table_name = Column(String, nullable=False)
    # SET NULL (not CASCADE): a schema re-sync that drops a canonical table should
    # keep this per-user row (re-linked by name next sync; preserves audit history).
    # The row is cleaned up via the data_source_id CASCADE when the DS is deleted.
    data_source_table_id = Column(String(36), ForeignKey('datasource_tables.id', ondelete='SET NULL'), nullable=True)

    # Visibility and status for this user
    is_accessible = Column(Boolean, nullable=False, default=True)
    status = Column(String, nullable=False, default="accessible")  # accessible | inaccessible | unknown

    # Provenance and diagnostics
    metadata_json = Column(JSON, nullable=True)

    data_source = relationship("DataSource", lazy="selectin")
    user = relationship("User", lazy="selectin")


class UserDataSourceColumn(BaseSchema):
    __tablename__ = "user_data_source_columns"
    __table_args__ = (
        UniqueConstraint('user_data_source_table_id', 'column_name', name='uq_user_ds_table_column'),
    )

    user_data_source_table_id = Column(String(36), ForeignKey('user_data_source_tables.id', ondelete='CASCADE'), nullable=False, index=True)
    column_name = Column(String, nullable=False)
    is_accessible = Column(Boolean, nullable=False, default=True)
    is_masked = Column(Boolean, nullable=False, default=False)
    data_type = Column(String, nullable=True)


