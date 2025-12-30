from sqlalchemy import Column, String, Boolean, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UserConnectionTable(BaseSchema):
    """
    Tracks per-user table visibility for connections with auth_policy = "user_required".
    Records what tables a specific user can see based on their database grants.
    """
    __tablename__ = "user_connection_tables"
    __table_args__ = (
        UniqueConstraint('connection_id', 'user_id', 'table_name', name='uq_user_conn_table'),
    )

    connection_id = Column(String(36), ForeignKey('connections.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    table_name = Column(String, nullable=False)
    connection_table_id = Column(String(36), ForeignKey('connection_tables.id'), nullable=True)

    # Visibility and status for this user
    is_accessible = Column(Boolean, nullable=False, default=True)
    status = Column(String, nullable=False, default="accessible")  # accessible | inaccessible | unknown

    # Provenance and diagnostics
    metadata_json = Column(JSON, nullable=True)

    # Relationships
    connection = relationship("Connection", back_populates="user_tables", lazy="selectin")
    user = relationship("User", lazy="selectin")
    connection_table = relationship("ConnectionTable", back_populates="user_overlays", lazy="selectin")


class UserConnectionColumn(BaseSchema):
    """
    Tracks per-user column visibility within a table.
    """
    __tablename__ = "user_connection_columns"
    __table_args__ = (
        UniqueConstraint('user_connection_table_id', 'column_name', name='uq_user_conn_table_column'),
    )

    user_connection_table_id = Column(String(36), ForeignKey('user_connection_tables.id'), nullable=False, index=True)
    column_name = Column(String, nullable=False)
    is_accessible = Column(Boolean, nullable=False, default=True)
    is_masked = Column(Boolean, nullable=False, default=False)
    data_type = Column(String, nullable=True)

    # Relationships
    user_connection_table = relationship("UserConnectionTable", backref="columns", lazy="selectin")

