"""Readable share link per artifact: artifacts.slug + artifact_slug_history.

Revision ID: artslug01
Revises: artshare01

/r/{slug} opens one artifact, as /r/{report_id}?artifact={id} does. The slug
is optional (NULL until the owner sets one) and unique across organizations.
artifact_slug_history keeps earlier slugs so links already sent keep
resolving after a rename; a released name stays there with no artifact,
reserved for its organization. Schema only — no existing row changes.
"""

import sqlalchemy as sa

from alembic import op

revision = "artslug01"
down_revision = "artshare01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("slug", sa.String(80), nullable=True))
        batch_op.create_index("ix_artifacts_slug", ["slug"], unique=True)

    op.create_table(
        "artifact_slug_history",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
    )
    op.create_index("ix_artifact_slug_history_slug", "artifact_slug_history", ["slug"], unique=True)
    op.create_index("ix_artifact_slug_history_artifact_id", "artifact_slug_history", ["artifact_id"])
    op.create_index("ix_artifact_slug_history_organization_id", "artifact_slug_history", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_slug_history_organization_id", table_name="artifact_slug_history")
    op.drop_index("ix_artifact_slug_history_artifact_id", table_name="artifact_slug_history")
    op.drop_index("ix_artifact_slug_history_slug", table_name="artifact_slug_history")
    op.drop_table("artifact_slug_history")

    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index("ix_artifacts_slug")
        batch_op.drop_column("slug")
