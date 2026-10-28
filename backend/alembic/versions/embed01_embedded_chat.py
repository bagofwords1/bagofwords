"""Embedded chat deployments

Revision ID: embed01
Revises: refhist01
Create Date: 2026-10-28

* ``embeds``: one row per embedded-chat deployment (origins, assertion
  issuer, assertion public key, epoch, runtime + branding JSON).
* ``embed_assertion_replays``: accepted assertion ``jti`` values until expiry
  (one-time bootstrap exchange).
* ``oauth_mcp_access_tokens.embed_id``: embed sessions reuse the token table.
* ``reports.embed_id`` + partial unique index ``uq_reports_embed_user``: one
  live private thread per (embed, user), enforced by the database so two
  concurrent first messages cannot create two threads.

Additive only; no existing row changes.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "embed01"
down_revision: str | None = "refhist01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)
_ONE_THREAD_WHERE = sa.text("embed_id IS NOT NULL AND deleted_at IS NULL")


def upgrade() -> None:
    op.create_table(
        "embeds",
        sa.Column("id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("public_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("allowed_origins", sa.JSON(), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject_attribute", sa.String(length=64), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=32), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("runtime", sa.JSON(), nullable=False),
        sa.Column("branding", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", S36, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_embeds"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_embeds_organization_id"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_embeds_created_by_user_id"),
    )
    op.create_index("ix_embeds_organization_id", "embeds", ["organization_id"])
    op.create_index("ix_embeds_public_id", "embeds", ["public_id"], unique=True)

    op.create_table(
        "embed_assertion_replays",
        sa.Column("id", S36, nullable=False),
        sa.Column("embed_id", S36, nullable=False),
        sa.Column("jti", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_embed_assertion_replays"),
        sa.ForeignKeyConstraint(["embed_id"], ["embeds.id"], name="fk_embed_assertion_replays_embed_id",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("embed_id", "jti", name="uq_embed_assertion_replays_embed_jti"),
    )
    op.create_index("ix_embed_assertion_replays_embed_id", "embed_assertion_replays", ["embed_id"])
    op.create_index("ix_embed_assertion_replays_expires_at", "embed_assertion_replays", ["expires_at"])

    with op.batch_alter_table("oauth_mcp_access_tokens") as batch:
        batch.add_column(sa.Column("embed_id", S36, nullable=True))
        batch.create_foreign_key("fk_oauth_mcp_access_tokens_embed_id", "embeds", ["embed_id"], ["id"],
                                 ondelete="CASCADE")
    op.create_index("ix_oauth_mcp_access_tokens_embed_id", "oauth_mcp_access_tokens", ["embed_id"])

    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("embed_id", S36, nullable=True))
        batch.create_foreign_key("fk_reports_embed_id", "embeds", ["embed_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_reports_embed_id", "reports", ["embed_id"])
    op.create_index("uq_reports_embed_user", "reports", ["embed_id", "user_id"], unique=True,
                    sqlite_where=_ONE_THREAD_WHERE, postgresql_where=_ONE_THREAD_WHERE)


def downgrade() -> None:
    op.drop_index("uq_reports_embed_user", table_name="reports")
    op.drop_index("ix_reports_embed_id", table_name="reports")
    with op.batch_alter_table("reports") as batch:
        batch.drop_constraint("fk_reports_embed_id", type_="foreignkey")
        batch.drop_column("embed_id")
    op.drop_index("ix_oauth_mcp_access_tokens_embed_id", table_name="oauth_mcp_access_tokens")
    with op.batch_alter_table("oauth_mcp_access_tokens") as batch:
        batch.drop_constraint("fk_oauth_mcp_access_tokens_embed_id", type_="foreignkey")
        batch.drop_column("embed_id")
    op.drop_index("ix_embed_assertion_replays_expires_at", table_name="embed_assertion_replays")
    op.drop_index("ix_embed_assertion_replays_embed_id", table_name="embed_assertion_replays")
    op.drop_table("embed_assertion_replays")
    op.drop_index("ix_embeds_public_id", table_name="embeds")
    op.drop_index("ix_embeds_organization_id", table_name="embeds")
    op.drop_table("embeds")
