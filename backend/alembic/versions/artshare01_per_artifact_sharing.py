"""Share each artifact on its own: artifacts.visibility + artifact_shares.

Revision ID: artshare01
Revises: emailclaim01

Until now a report's dashboards were shared together: reports.artifact_visibility
plus report_shares rows with share_type='artifact' opened every artifact of the
report at once. Sharing moves to the artifact itself.

Data migration — nobody gains or loses access on upgrade: every existing
artifact inherits its report's artifact_visibility, and every live
share_type='artifact' grant is copied onto each live artifact of that report.
reports.artifact_visibility and the report_shares artifact rows are left in
place; the app keeps them as the report-wide aggregate (most open visibility,
union of grants), so the downgrade below finds them intact.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa

from alembic import op

revision = "artshare01"
down_revision = "emailclaim01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("visibility", sa.String(20), nullable=False, server_default="none")
        )

    op.create_table(
        "artifact_shares",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("group_id", sa.String(36), sa.ForeignKey("groups.id"), nullable=True),
        sa.UniqueConstraint("artifact_id", "user_id", name="uq_artifact_share_user"),
        sa.UniqueConstraint("artifact_id", "group_id", name="uq_artifact_share_group"),
    )
    op.create_index("ix_artifact_shares_artifact_id", "artifact_shares", ["artifact_id"])
    op.create_index("ix_artifact_shares_report_id", "artifact_shares", ["report_id"])
    op.create_index("ix_artifact_shares_user_id", "artifact_shares", ["user_id"])
    op.create_index("ix_artifact_shares_group_id", "artifact_shares", ["group_id"])

    bind = op.get_bind()

    # 1. Every artifact starts with its report's dashboard visibility.
    bind.execute(sa.text(
        "UPDATE artifacts SET visibility = COALESCE("
        "(SELECT reports.artifact_visibility FROM reports WHERE reports.id = artifacts.report_id), 'none')"
    ))

    # 2. Copy each live report-level dashboard grant onto every live artifact
    #    of that report. Ids are minted here: gen_random_uuid() is Postgres-only.
    rows = bind.execute(sa.text(
        "SELECT a.id, rs.report_id, rs.user_id, rs.group_id "
        "FROM report_shares rs JOIN artifacts a ON a.report_id = rs.report_id "
        "WHERE rs.share_type = 'artifact' AND rs.deleted_at IS NULL AND a.deleted_at IS NULL"
    )).fetchall()
    if rows:
        now = datetime.utcnow()
        shares = sa.table(
            "artifact_shares",
            sa.column("id", sa.String),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
            sa.column("artifact_id", sa.String),
            sa.column("report_id", sa.String),
            sa.column("user_id", sa.String),
            sa.column("group_id", sa.String),
        )
        seen = set()
        payload = []
        for artifact_id, report_id, user_id, group_id in rows:
            key = (artifact_id, user_id, group_id)
            if key in seen:
                continue
            seen.add(key)
            payload.append({
                "id": str(uuid.uuid4()),
                "created_at": now,
                "updated_at": now,
                "artifact_id": artifact_id,
                "report_id": report_id,
                "user_id": user_id,
                "group_id": group_id,
            })
        op.bulk_insert(shares, payload)


def downgrade() -> None:
    op.drop_index("ix_artifact_shares_group_id", table_name="artifact_shares")
    op.drop_index("ix_artifact_shares_user_id", table_name="artifact_shares")
    op.drop_index("ix_artifact_shares_report_id", table_name="artifact_shares")
    op.drop_index("ix_artifact_shares_artifact_id", table_name="artifact_shares")
    op.drop_table("artifact_shares")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_column("visibility")
