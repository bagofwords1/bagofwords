"""Default concurrent queries per connection: 4 -> 12

The limit is now app-wide (every worker and replica together) instead of per
process, so the old default of 4 would cut a typical 3-worker install from an
effective 12 to 4. The schema default is now 12, but organization settings
store a snapshot of each feature's value when created, and that stored value
wins: without this migration every existing organization keeps 4.

A stored 4 was the default everyone got, so it is treated as "never changed"
and moved to 12. Any other value is an admin's choice and is left alone.
Idempotent; downgrade restores 4 only where the value is 12.

Revision ID: queryexec03
Revises: queryexec02
Create Date: 2026-11-16
"""
import json

import sqlalchemy as sa

from alembic import op

revision = "queryexec03"
down_revision = "queryexec02"
branch_labels = None
depends_on = None

KEY = "max_concurrent_queries_per_connection"
OLD_DEFAULT = 4
NEW_DEFAULT = 12


def _rewrite(old: int, new: int) -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("organization_settings"):
        return
    table = sa.table("organization_settings", sa.column("id", sa.String), sa.column("config", sa.JSON))
    for row_id, config in bind.execute(sa.select(table.c.id, table.c.config)).all():
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        if not isinstance(config, dict):
            continue
        changed = False
        for holder in (config, config.get("ai_features")):
            entry = holder.get(KEY) if isinstance(holder, dict) else None
            if isinstance(entry, dict) and entry.get("value") == old:
                entry["value"] = new
                changed = True
            elif isinstance(holder, dict) and holder.get(KEY) == old:
                holder[KEY] = new
                changed = True
        if changed:
            bind.execute(sa.update(table).where(table.c.id == row_id).values(config=config))


def upgrade() -> None:
    _rewrite(OLD_DEFAULT, NEW_DEFAULT)


def downgrade() -> None:
    _rewrite(NEW_DEFAULT, OLD_DEFAULT)
