from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)

from app.models.base import BaseSchema


# A request's lifecycle. 'pending' until an editor answers; 'approved' or
# 'denied' after. First answer wins (dashboard_access_requests service), so a
# row leaves 'pending' exactly once.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED)

# A denied requester may ask again only after this long (the product rule:
# "A denied user can ask again after 7 days").
DENIAL_COOLDOWN_DAYS = 7


class DashboardAccessRequest(BaseSchema):
    """A request to view one dashboard, raised by a member who cannot see it.

    A blocked reader opens a dashboard link they have no access to and asks
    for view access with one click; the dashboard's editors answer it from the
    share settings (docs none/above). One open request per (artifact,
    requester) is enforced by a partial unique index -- asking again while one
    is pending is refused, and a denied request stops holding the slot so the
    same person can ask again after the cooldown.
    """

    __tablename__ = "dashboard_access_requests"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_dashboard_access_requests"),
        # At most one PENDING request per (artifact, requester). Denied/approved
        # rows stay as history and do not hold the slot, so the partial index is
        # on the pending ones only (same shape as artifact_releases' one-publishing).
        Index(
            "uq_dashboard_access_request_pending",
            "artifact_id", "requester_user_id", unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_dashboard_access_requests_artifact_status", "artifact_id", "status"),
    )

    organization_id = Column(
        String(36),
        ForeignKey("organizations.id", name="fk_dashboard_access_requests_organization_id"),
        nullable=False, index=True,
    )
    artifact_id = Column(
        String(36),
        ForeignKey("artifacts.id", name="fk_dashboard_access_requests_artifact_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    requester_user_id = Column(
        String(36),
        ForeignKey("users.id", name="fk_dashboard_access_requests_requester_user_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # The requester's optional message to the editors. Rendered escaped
    # everywhere (inbox, email): it is user text.
    note = Column(Text, nullable=True)
    # pending | approved | denied (see STATUS_* above)
    status = Column(String(10), nullable=False, default=STATUS_PENDING, server_default=STATUS_PENDING)
    # The editor who answered (first answer wins), and when.
    decided_by_user_id = Column(
        String(36),
        ForeignKey("users.id", name="fk_dashboard_access_requests_decided_by_user_id"),
        nullable=True,
    )
    decided_at = Column(DateTime, nullable=True)
