"""session revocation: user.session_epoch + login_exchange_codes

Two halves of the same fix for un-revokable session tokens:

* ``users.session_epoch`` — stamped into every session JWT and compared on each
  request, so logout / password change / admin force-signout actually revoke
  tokens that were already issued. Existing tokens carry no claim (read as 0)
  and so are rejected against the seeded value of 1: every user is signed out
  once on upgrade, which is intended for a credential-revocation fix.

* ``login_exchange_codes`` — lets the SSO callback stop putting the JWT in the
  redirect URL, handing back a single-use short-lived code instead.

Revision ID: sessepoch01
Revises: officejs01
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "sessepoch01"
down_revision: Union[str, None] = "officejs01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "login_exchange_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_login_exchange_codes_code_hash",
        "login_exchange_codes",
        ["code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_login_exchange_codes_user_id", "login_exchange_codes", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_login_exchange_codes_user_id", table_name="login_exchange_codes")
    op.drop_index("ix_login_exchange_codes_code_hash", table_name="login_exchange_codes")
    op.drop_table("login_exchange_codes")
    op.drop_column("users", "session_epoch")
