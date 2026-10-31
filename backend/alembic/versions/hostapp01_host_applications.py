"""Connected applications with OAuth and embedding capabilities

Revision ID: hostapp01
Revises: refhist01
Create Date: 2026-10-29

* ``connected_applications``: shared name, lifecycle and agent access ceiling.
* ``application_oauth_config``: existing OAuth registrations, moved without rotating credentials.
* ``application_embedding_config``: external systems (ERP, ServiceNow, portals) allowed
  to frame artifacts and sign their users in (origins, identity mode and
  source, issuer + public key or IdP audience, epoch).
* ``artifact_host_applications``: the artifact's side of the opt-in.
* ``host_assertion_replays``: accepted assertion ``jti`` values (one-time use).
* ``oauth_mcp_access_tokens.host_application_id``: embed sessions reuse the
  token table.

Replaces the unreleased host schema; idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "hostapp01"
down_revision: str | None = "refhist01"
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


def _oauth_table(name, *, configuration):
    columns = [
        sa.Column("id", S36, *([sa.ForeignKey("connected_applications.id", ondelete="CASCADE")] if configuration else []), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("client_secret_hash", sa.String(64), nullable=False),
        sa.Column("redirect_uris", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(255), nullable=False, server_default="mcp"),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("entra_exchange", sa.JSON(), nullable=True),
    ]
    if not configuration:
        columns += [sa.Column("organization_id", S36, sa.ForeignKey("organizations.id"), nullable=False, index=True),
                    sa.Column("name", sa.String(255), nullable=False), *_timestamps()]
    op.create_table(name, *columns)


def upgrade() -> None:
    if _has_table("connected_applications"):
        required = ("application_oauth_config", "application_embedding_config",
                    "artifact_host_applications", "host_assertion_replays")
        if not all(_has_table(name) for name in required) or _has_table("oauth_mcp_clients"):
            raise RuntimeError("Incomplete connected application schema; restore the pre-migration database and retry")
        return
    op.create_table(
        "connected_applications",
        sa.Column("id", S36, primary_key=True),
        sa.Column("organization_id", S36, sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("agent_ids", sa.JSON(), nullable=True),
        sa.Column("created_by_user_id", S36, sa.ForeignKey("users.id"), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_connected_applications_organization_id", "connected_applications", ["organization_id"])
    # Preserve every registered OAuth identity and credential. Tokens reference
    # the unchanged public client_id, not the client table's physical name.
    op.execute(sa.text("INSERT INTO connected_applications (id, organization_id, name, created_at, updated_at, deleted_at) "
                       "SELECT id, organization_id, name, created_at, updated_at, deleted_at FROM oauth_mcp_clients"))
    _oauth_table("application_oauth_config", configuration=True)
    fields = "id, client_id, client_secret_hash, redirect_uris, scopes, trusted, entra_exchange"
    op.execute(sa.text(f"INSERT INTO application_oauth_config ({fields}) SELECT {fields} FROM oauth_mcp_clients"))
    op.drop_table("oauth_mcp_clients")
    op.create_table(
        "application_embedding_config",
        sa.Column("id", S36, sa.ForeignKey("connected_applications.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("public_id", sa.String(40), nullable=False, unique=True),
        sa.Column("allowed_origins", sa.JSON(), nullable=False),
        sa.Column("identity_mode", sa.String(16), nullable=False, server_default="assertion"),
        sa.Column("identity_source", sa.String(80), nullable=True),
        sa.Column("create_users", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("issuer", sa.String(255), nullable=True),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("key_fingerprint", sa.String(32), nullable=True),
        sa.Column("audience", sa.String(255), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
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
        sa.ForeignKeyConstraint(["host_application_id"], ["application_embedding_config.id"],
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
        sa.ForeignKeyConstraint(["host_application_id"], ["application_embedding_config.id"],
                                name="fk_host_assertion_replays_host_application_id", ondelete="CASCADE"),
        sa.UniqueConstraint("host_application_id", "jti", name="uq_host_assertion_replays_host_jti"),
    )
    op.create_index("ix_host_assertion_replays_host_application_id", "host_assertion_replays", ["host_application_id"])
    op.create_index("ix_host_assertion_replays_expires_at", "host_assertion_replays", ["expires_at"])

    with op.batch_alter_table("oauth_mcp_access_tokens") as batch:
        batch.add_column(sa.Column("host_application_id", S36, nullable=True))
        batch.create_foreign_key("fk_oauth_mcp_access_tokens_host_application_id", "application_embedding_config",
                                 ["host_application_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_oauth_mcp_access_tokens_host_application_id", "oauth_mcp_access_tokens",
                    ["host_application_id"])


def downgrade() -> None:
    if not _has_table("connected_applications"):
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
    op.drop_table("application_embedding_config")
    _oauth_table("oauth_mcp_clients", configuration=False)
    op.execute(sa.text("INSERT INTO oauth_mcp_clients (id, organization_id, name, created_at, updated_at, deleted_at, "
                       "client_id, client_secret_hash, redirect_uris, scopes, trusted, entra_exchange) "
                       "SELECT a.id, a.organization_id, a.name, a.created_at, a.updated_at, a.deleted_at, "
                       "c.client_id, c.client_secret_hash, c.redirect_uris, c.scopes, c.trusted, c.entra_exchange "
                       "FROM application_oauth_config c JOIN connected_applications a ON a.id = c.id"))
    op.drop_table("application_oauth_config")
    op.drop_index("ix_connected_applications_organization_id", table_name="connected_applications")
    op.drop_table("connected_applications")
