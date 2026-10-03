from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UserDataSourceTable(BaseSchema):
    __tablename__ = "user_data_source_tables"
    __table_args__ = (
        # Scoped by CONNECTION, not just by data source. An agent can hold two
        # delegated connections that both expose an `orders`; keyed on
        # (data_source, user, table_name) alone they collided into one row, so
        # the second connection's table silently overwrote the first's
        # accessibility. The old constraint name is kept free for the migration
        # that swaps them.
        UniqueConstraint('data_source_id', 'user_id', 'connection_id', 'table_name', name='uq_user_ds_conn_table'),
        Index('ix_udst_ds_user_conn', 'data_source_id', 'user_id', 'connection_id'),
    )

    data_source_id = Column(String(36), ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    # Which connection this row describes. Nullable for rows written before
    # overlays were connection-aware (and backfilled where the canonical row is
    # linked); readers treat NULL as "applies to this user on any connection",
    # which is safe because every read is already filtered by user_id.
    connection_id = Column(String(36), ForeignKey('connections.id', ondelete='CASCADE'), nullable=True, index=True)
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


