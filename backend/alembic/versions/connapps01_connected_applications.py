"""Unify released OAuth and host registrations without replacing hostapp01.

Revision ID: connapps01
Revises: payload01
Create Date: 2026-10-31

Copy before switching foreign keys, then remove the old registration tables.
Keep IDs, credentials, epochs, grants, replay protection and artifact bindings.
Existing registrations retain their unrestricted application agent ceiling;
the user's and artifact's permissions still apply. Never merge apps by name.
"""
import sqlalchemy as sa

from alembic import op

revision = "connapps01"
down_revision = "payload01"
branch_labels = None
depends_on = None

S36 = sa.String(36)
COMMON = "id, organization_id, name, created_at, updated_at, deleted_at"
OAUTH = "id, client_id, client_secret_hash, redirect_uris, scopes, trusted, entra_exchange"
EMBED = ("id, public_id, allowed_origins, identity_mode, identity_source, create_users, "
         "issuer, public_key, key_fingerprint, audience, epoch, revision")
HOST_COMMON = COMMON + ", enabled, created_by_user_id"
CHILDREN = ("artifact_host_applications", "host_assertion_replays", "oauth_mcp_access_tokens")


def _timestamps():
    return [sa.Column(name, sa.DateTime(), nullable=True)
            for name in ("created_at", "updated_at", "deleted_at")]


def _oauth_table(name, *, legacy=False):
    columns = [
        sa.Column("id", S36, *([] if legacy else [sa.ForeignKey("connected_applications.id", ondelete="CASCADE")]), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("client_secret_hash", sa.String(64), nullable=False),
        sa.Column("redirect_uris", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(255), nullable=False, server_default="mcp"),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("entra_exchange", sa.JSON(), nullable=True),
    ]
    if legacy:
        columns += [sa.Column("organization_id", S36, sa.ForeignKey("organizations.id"), nullable=False, index=True),
                    sa.Column("name", sa.String(255), nullable=False), *_timestamps()]
    op.create_table(name, *columns)


def _embedding_table(name, *, legacy=False):
    columns = [
        sa.Column("id", S36, *([] if legacy else [sa.ForeignKey("connected_applications.id", ondelete="CASCADE")]), primary_key=True),
        sa.Column("public_id", sa.String(40), nullable=False),
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
    ]
    if legacy:
        columns += [sa.Column("organization_id", S36, sa.ForeignKey("organizations.id"), nullable=False),
                    sa.Column("name", sa.String(120), nullable=False),
                    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
                    sa.Column("created_by_user_id", S36, sa.ForeignKey("users.id"), nullable=True), *_timestamps()]
    else:
        columns.append(sa.UniqueConstraint("public_id"))
    op.create_table(name, *columns)
    if legacy:
        op.create_index("ix_host_applications_public_id", name, ["public_id"], unique=True)
        op.create_index("ix_host_applications_organization_id", name, ["organization_id"])


def _repoint_children(target):
    # Only rebuild referencing tables on SQLite. Rebuilding/dropping the old
    # parent while it is still referenced can cascade-delete live sessions.
    for table in CHILDREN:
        name = f"fk_{table}_host_application_id"
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="foreignkey")
            batch.create_foreign_key(name, target, ["host_application_id"], ["id"], ondelete="CASCADE")


def _complete_schema():
    inspector = sa.inspect(op.get_bind())
    required = ("connected_applications", "application_oauth_config", "application_embedding_config", *CHILDREN)
    return (all(inspector.has_table(name) for name in required)
            and not any(inspector.has_table(name) for name in ("oauth_mcp_clients", "host_applications"))
            and all(any(fk["referred_table"] == "application_embedding_config"
                        for fk in inspector.get_foreign_keys(table)) for table in CHILDREN))


def upgrade():
    conn = op.get_bind()
    if sa.inspect(conn).has_table("connected_applications"):
        # Also accepts complete disposable databases made by the prerelease
        # rewritten hostapp01, and the dashboard corpus's unstamped upgrade.
        if not _complete_schema():
            raise RuntimeError("Incomplete connected application schema; restore the pre-upgrade backup before retrying")
        return
    if conn.execute(sa.text("SELECT 1 FROM oauth_mcp_clients o JOIN host_applications h ON h.id=o.id LIMIT 1")).first():
        raise RuntimeError("OAuth and host application IDs collide; resolve the IDs before upgrading (no changes made)")
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
    op.execute(sa.text(f"INSERT INTO connected_applications ({COMMON}) SELECT {COMMON} FROM oauth_mcp_clients"))
    op.execute(sa.text(f"INSERT INTO connected_applications ({HOST_COMMON}) SELECT {HOST_COMMON} FROM host_applications"))
    _oauth_table("application_oauth_config")
    op.execute(sa.text(f"INSERT INTO application_oauth_config ({OAUTH}) SELECT {OAUTH} FROM oauth_mcp_clients"))
    _embedding_table("application_embedding_config")
    op.execute(sa.text(f"INSERT INTO application_embedding_config ({EMBED}) SELECT {EMBED} FROM host_applications"))
    _repoint_children("application_embedding_config")
    op.drop_table("host_applications")
    op.drop_table("oauth_mcp_clients")


def downgrade():
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("connected_applications"):
        return
    # The released schema has no app-level agent ceiling or OAuth disable
    # flag. Refuse a rollback that would silently widen access or lose apps.
    for app in conn.execute(sa.text(
        "SELECT a.agent_ids, a.enabled, a.name, o.id AS oauth_id, e.id AS embed_id "
        "FROM connected_applications a LEFT JOIN application_oauth_config o ON o.id=a.id "
        "LEFT JOIN application_embedding_config e ON e.id=a.id"
    )).mappings():
        if (app["agent_ids"] not in (None, "null") or (app["oauth_id"] and not app["enabled"])
                or (app["oauth_id"] and app["embed_id"])
                or (not app["oauth_id"] and not app["embed_id"])
                or (app["embed_id"] and len(app["name"]) > 120)):
            raise RuntimeError("Cannot safely downgrade connected application policies; restore a pre-upgrade backup instead")
    _oauth_table("oauth_mcp_clients", legacy=True)
    fields = COMMON + ", " + OAUTH.removeprefix("id, ")
    selected = ", ".join("a." + name for name in COMMON.split(", ")) + ", " + ", ".join("c." + name for name in OAUTH.split(", ")[1:])
    op.execute(sa.text(f"INSERT INTO oauth_mcp_clients ({fields}) SELECT {selected} "
                       "FROM application_oauth_config c JOIN connected_applications a ON a.id=c.id"))
    _embedding_table("host_applications", legacy=True)
    fields = HOST_COMMON + ", " + EMBED.removeprefix("id, ")
    selected = ", ".join("a." + name for name in HOST_COMMON.split(", ")) + ", " + ", ".join("c." + name for name in EMBED.split(", ")[1:])
    op.execute(sa.text(f"INSERT INTO host_applications ({fields}) SELECT {selected} "
                       "FROM application_embedding_config c JOIN connected_applications a ON a.id=c.id"))
    _repoint_children("host_applications")
    op.drop_table("application_embedding_config")
    op.drop_table("application_oauth_config")
    op.drop_table("connected_applications")
