"""per-member connection identity (member_mapped auth)

Adds ``connections.member_bindings`` (nullable JSON): which credential fields
take the member's admin-set attribute at query time, e.g.
``{"user_id": "qlik_user_id"}``.

Qlik Sense on-prem connections that required user authentication used the
"identity" mode, where each member typed the Qlik account to act as — with
the connection's site certificate, which can act as ANY account. They move to
``member_mapped`` with no bindings yet, so they fail closed ("an admin has not
chosen which member attribute identifies them") until an admin maps the
fields; the member-typed identity rows are deactivated (kept, not deleted).

Revision ID: memberid01
Revises: membattr01
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op


revision = "memberid01"
down_revision = "membattr01"
branch_labels = None
depends_on = None


_connections = sa.table(
    "connections",
    sa.column("id", sa.String),
    sa.column("type", sa.String),
    sa.column("allowed_user_auth_modes", sa.JSON),
)
_user_creds = sa.table(
    "user_connection_credentials",
    sa.column("connection_id", sa.String),
    sa.column("auth_mode", sa.String),
    sa.column("is_active", sa.Boolean),
)


def upgrade() -> None:
    with op.batch_alter_table("connections") as batch_op:
        batch_op.add_column(sa.Column("member_bindings", sa.JSON(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_connections.c.id, _connections.c.allowed_user_auth_modes).where(
            _connections.c.type == "qlik_sense_onprem"
        )
    ).fetchall()
    for conn_id, modes in rows:
        if not isinstance(modes, list) or "identity" not in modes:
            continue
        new_modes = ["member_mapped"] + [m for m in modes if m not in ("identity", "member_mapped")]
        bind.execute(
            _connections.update()
            .where(_connections.c.id == conn_id)
            .values(allowed_user_auth_modes=new_modes)
        )
        bind.execute(
            _user_creds.update()
            .where(
                _user_creds.c.connection_id == conn_id,
                _user_creds.c.auth_mode == "identity",
            )
            .values(is_active=False)
        )


def downgrade() -> None:
    # The mode conversion is not reverted: restoring member-typed identities
    # would reopen the impersonation hole this migration closes.
    with op.batch_alter_table("connections") as batch_op:
        batch_op.drop_column("member_bindings")
