"""Host applications that embed artifacts

Revision ID: hostapp01
Revises: connfail01
Create Date: 2026-10-29

* ``host_applications``: external systems (ERP, ServiceNow, portals) allowed
  to frame artifacts and sign their users in (origins, identity mode and
  source, issuer + public key or IdP audience, epoch).
* ``artifact_host_applications``: the artifact's side of the opt-in.
* ``host_assertion_replays``: accepted assertion ``jti`` values (one-time use).
* ``oauth_mcp_access_tokens.host_application_id``: embed sessions reuse the
  token table.

Additive only, and idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "hostapp01"
down_revision: str | None = "connfail01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    ]


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("host_applications"):
        return
    op.create_table(
        "host_applications",
        sa.Column("id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("public_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("allowed_origins", sa.JSON(), nullable=False),
        sa.Column("identity_mode", sa.String(length=16), nullable=False, server_default="assertion"),
        sa.Column("identity_source", sa.String(length=80), nullable=True),
        sa.Column("create_users", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("issuer", sa.String(length=255), nullable=True),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("key_fingerprint", sa.String(length=32), nullable=True),
        sa.Column("audience", sa.String(length=255), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", S36, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_host_applications"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_host_applications_organization_id"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_host_applications_created_by_user_id"),
    )
    op.create_index("ix_host_applications_organization_id", "host_applications", ["organization_id"])
    op.create_index("ix_host_applications_public_id", "host_applications", ["public_id"], unique=True)

    op.create_table(
        "artifact_host_applications",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("host_application_id", S36, nullable=False),
        sa.Column("created_by_user_id", S36, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_host_applications"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], name="fk_artifact_host_applications_artifact_id",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["host_application_id"], ["host_applications.id"],
                                name="fk_artifact_host_applications_host_application_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"],
                                name="fk_artifact_host_applications_created_by_user_id"),
        sa.UniqueConstraint("artifact_id", "host_application_id", name="uq_artifact_host_applications_pair"),
    )
    op.create_index("ix_artifact_host_applications_artifact_id", "artifact_host_applications", ["artifact_id"])
    op.create_index("ix_artifact_host_applications_host_application_id", "artifact_host_applications",
                    ["host_application_id"])

    op.create_table(
        "host_assertion_replays",
        sa.Column("id", S36, nullable=False),
        sa.Column("host_application_id", S36, nullable=False),
        sa.Column("jti", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_host_assertion_replays"),
        sa.ForeignKeyConstraint(["host_application_id"], ["host_applications.id"],
                                name="fk_host_assertion_replays_host_application_id", ondelete="CASCADE"),
        sa.UniqueConstraint("host_application_id", "jti", name="uq_host_assertion_replays_host_jti"),
    )
    op.create_index("ix_host_assertion_replays_host_application_id", "host_assertion_replays", ["host_application_id"])
    op.create_index("ix_host_assertion_replays_expires_at", "host_assertion_replays", ["expires_at"])

    with op.batch_alter_table("oauth_mcp_access_tokens") as batch:
        batch.add_column(sa.Column("host_application_id", S36, nullable=True))
        batch.create_foreign_key("fk_oauth_mcp_access_tokens_host_application_id", "host_applications",
                                 ["host_application_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_oauth_mcp_access_tokens_host_application_id", "oauth_mcp_access_tokens",
                    ["host_application_id"])


def downgrade() -> None:
    if not _has_table("host_applications"):
        return
    op.drop_index("ix_oauth_mcp_access_tokens_host_application_id", table_name="oauth_mcp_access_tokens")
    with op.batch_alter_table("oauth_mcp_access_tokens") as batch:
        batch.drop_constraint("fk_oauth_mcp_access_tokens_host_application_id", type_="foreignkey")
        batch.drop_column("host_application_id")
    op.drop_index("ix_host_assertion_replays_expires_at", table_name="host_assertion_replays")
    op.drop_index("ix_host_assertion_replays_host_application_id", table_name="host_assertion_replays")
    op.drop_table("host_assertion_replays")
    op.drop_index("ix_artifact_host_applications_host_application_id", table_name="artifact_host_applications")
    op.drop_index("ix_artifact_host_applications_artifact_id", table_name="artifact_host_applications")
    op.drop_table("artifact_host_applications")
    op.drop_index("ix_host_applications_public_id", table_name="host_applications")
    op.drop_index("ix_host_applications_organization_id", table_name="host_applications")
    op.drop_table("host_applications")
