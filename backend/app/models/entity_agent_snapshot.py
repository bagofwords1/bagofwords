from sqlalchemy import JSON, Column, DateTime, ForeignKey, String, Text, UniqueConstraint

from app.models.base import BaseSchema
from app.ee.encryption import EncryptedJSON


class EntityAgentSnapshot(BaseSchema):
    """A saved query's shared result on one of its agents other than its origin.

    A query shared with several agents runs on each agent's own database, so
    each agent has its own result. The origin agent's stays where it always
    was — Entity.data / applied_params / last_refreshed_at — so every existing
    reader of the shared snapshot is unchanged; this table holds the others,
    one row per (entity, agent). Same shape and the same viewer policy as
    Entity.data: it is the shared (owner-identity) result, and per-viewer
    slices still go to entity_user_results.

    Rows are derived data: dropped when the query's code changes (they were
    produced by other code) and when the agent leaves the query.
    """
    __tablename__ = 'entity_agent_snapshots'
    __table_args__ = (
        UniqueConstraint('entity_id', 'data_source_id', name='uq_entity_agent_snapshots_entity_agent'),
    )

    entity_id = Column(String(36), ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True)
    data_source_id = Column(String(36), ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)

    # Same shape as Entity.data ({"rows": [...], "columns": [...]})
    data = Column(EncryptedJSON, nullable=True, default=dict)
    applied_params = Column(JSON(none_as_null=True), nullable=True, default=None)
    last_refreshed_at = Column(DateTime, nullable=True)
    # 'success' | 'error'
    status = Column(String(20), nullable=False, default='success')
    status_reason = Column(Text, nullable=True)
