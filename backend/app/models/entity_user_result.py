from sqlalchemy import JSON, Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class EntityUserResult(BaseSchema):
    """Per-viewer materialization of an identity-scoped entity.

    Entity.data is the single shared snapshot (defaults + the OWNER's
    identity). When an entity declares identity-source parameters, every
    other viewer resolves their own slice — executed with THEIR identity
    binding — and it lands here, keyed by (entity, user, params fingerprint),
    mirroring step_user_results.

    Rows are a cache of derived data: a row is stale (re-executed on next
    read) once the shared snapshot refreshes past its last_run_at, and rows
    are hard-deleted when the entity is deleted (FK cascade).
    """
    __tablename__ = 'entity_user_results'
    __table_args__ = (
        UniqueConstraint(
            'entity_id', 'user_id', 'params_fingerprint',
            name='uq_entity_user_results_entity_user_params',
        ),
    )

    entity_id = Column(String(36), ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)

    # 'success' | 'error'
    status = Column(String(20), nullable=False, default='success')
    status_reason = Column(Text, nullable=True)

    # Same shape as Entity.data ({"rows": [...], "columns": [...]})
    data = Column(JSON, nullable=True, default=dict)

    # Stable hash of the resolved param values ('' = defaults only)
    params_fingerprint = Column(String(64), nullable=False, default='', server_default='')
    applied_params = Column(JSON(none_as_null=True), nullable=True, default=None)

    last_run_at = Column(DateTime, nullable=True)

    entity = relationship("Entity", lazy="selectin")
    user = relationship("User", lazy="selectin")
