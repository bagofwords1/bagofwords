"""What an app activity event carried: request, response, stream

Revision ID: payload01
Revises: mrgheads11
Create Date: 2026-10-31

``artifact_activity_payloads``: one optional row per activity event, private
to the person whose session produced it, encrypted at rest (EncryptedJSON),
kept 24 hours (services/artifact_activity.py). Inspect shows it to its owner;
the agent sees it through the org's data gate.

Additive and idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "payload01"
down_revision: str | None = "mrgheads11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "artifact_activity_payloads"
S36 = sa.String(length=36)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", S36, nullable=False),
        sa.Column("event_id", S36, sa.ForeignKey("artifact_activity_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", S36, sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", S36, nullable=False),
        # EncryptedJSON rides on the JSON type (app/ee/encryption/types.py).
        sa.Column("request", sa.JSON(), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("stream", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_artifact_activity_payloads_event"),
    )
    op.create_index("ix_artifact_activity_payloads_organization_id", TABLE, ["organization_id"])
    op.create_index("ix_artifact_activity_payloads_created", TABLE, ["created_at"])


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    for name in ("ix_artifact_activity_payloads_created", "ix_artifact_activity_payloads_organization_id"):
        try:
            op.drop_index(name, table_name=TABLE)
        except Exception:
            pass
    op.drop_table(TABLE)
