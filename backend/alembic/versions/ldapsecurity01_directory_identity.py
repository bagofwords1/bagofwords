"""Persist verified directory identity without linking existing email matches.

Revision ID: ldapsecurity01
Revises: sessepoch01
"""
from alembic import op
import sqlalchemy as sa

revision = "ldapsecurity01"
down_revision = "sessepoch01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("ldap_identity", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("ldap_subject", sa.String(101), nullable=True))
    op.create_index("ix_users_ldap_subject", "users", ["ldap_subject"], unique=True)
    op.add_column("memberships", sa.Column("directory_provider", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("memberships", "directory_provider")
    op.drop_index("ix_users_ldap_subject", table_name="users")
    op.drop_column("users", "ldap_subject")
    op.drop_column("users", "ldap_identity")
