"""grant create_private_connector to system member role

Lets members self-serve PRIVATE, tools-only connectors (mcp/custom_api) without
the org-wide create_data_source governance permission. Idempotently appends the
permission to every system 'member' role's permissions JSON.

Revision ID: cn1prv2conn3
Revises: c3f1a9b2d4e7
Create Date: 2026-06-27 00:00:00.000000
"""
from typing import Sequence, Union
import json

from alembic import op
import sqlalchemy as sa


revision: str = "cn1prv2conn3"
down_revision: Union[str, Sequence[str], None] = "c3f1a9b2d4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERM = "create_private_connector"


def _rewrite(add: bool) -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, permissions FROM roles WHERE name = 'member' AND is_system = 1")
    ).fetchall()
    for rid, perms in rows:
        try:
            lst = json.loads(perms) if isinstance(perms, str) else (perms or [])
        except Exception:
            lst = []
        if add and PERM not in lst:
            lst.append(PERM)
        elif not add and PERM in lst:
            lst = [p for p in lst if p != PERM]
        else:
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(lst), "id": rid},
        )


def upgrade() -> None:
    _rewrite(add=True)


def downgrade() -> None:
    _rewrite(add=False)
