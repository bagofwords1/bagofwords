"""SAML identities and single-use authentication requests."""

from alembic import op
import sqlalchemy as sa

revision = "saml1001"
down_revision = "umbr0928"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("deleted_at", sa.DateTime()),
    ]


def upgrade():
    op.create_table(
        "saml_identities",
        *common(),
        sa.Column("identity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("provider", sa.String(48), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("subject", sa.String(2048), nullable=False),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("provider", "organization_id", "user_id", name="uq_saml_user_provider_org"),
    )
    op.create_index("ix_saml_identities_user_id", "saml_identities", ["user_id"])
    op.create_table(
        "saml_requests",
        *common(),
        sa.Column("request_id", sa.String(128), nullable=False, unique=True),
        sa.Column("provider", sa.String(48), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("relay_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("assertion_hash", sa.String(64), unique=True),
    )
    op.create_index("ix_saml_requests_expires_at", "saml_requests", ["expires_at"])


def downgrade():
    op.drop_table("saml_requests")
    op.drop_table("saml_identities")
