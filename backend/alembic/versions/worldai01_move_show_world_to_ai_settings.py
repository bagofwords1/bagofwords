"""Move the World view toggle from `general.show_world` to a top-level feature.

The World switch moved from Settings > General to Settings > AI settings, which
lists top-level FeatureConfig entries. Orgs that had World on keep it on: their
`general.show_world: true` becomes `show_world: {value: true, ...}`. The old
`general.show_world` key is removed either way. An org that already has the new
key is left alone. Data only.

Downgrade puts `general.show_world` back from the feature's value.
"""
from typing import Sequence, Union
import json

from alembic import op
import sqlalchemy as sa


revision: str = 'worldai01'
down_revision: Union[str, None] = 'slackchan01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_KEY = "show_world"


def _feature(on: bool) -> dict:
    return {
        "value": on,
        "name": "World view",
        "description": "",  # refreshed from the schema on the next settings read
        "is_lab": False,
        "editable": True,
        "state": "enabled" if on else "disabled",
    }


def _rows(conn):
    for row_id, raw in conn.execute(sa.text("SELECT id, config FROM organization_settings")).fetchall():
        try:
            config = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if isinstance(config, dict):
            yield row_id, config


def _save(conn, row_id, config) -> None:
    conn.execute(
        sa.text("UPDATE organization_settings SET config = :c WHERE id = :i"),
        {"c": json.dumps(config), "i": row_id},
    )


def upgrade() -> None:
    conn = op.get_bind()
    for row_id, config in list(_rows(conn)):
        general = config.get("general")
        if not isinstance(general, dict) or "show_world" not in general:
            continue
        old = general.pop("show_world")
        if NEW_KEY not in config and old is True:
            config[NEW_KEY] = _feature(True)
        _save(conn, row_id, config)


def downgrade() -> None:
    conn = op.get_bind()
    for row_id, config in list(_rows(conn)):
        entry = config.pop(NEW_KEY, None)
        if entry is None:
            continue
        general = config.get("general") if isinstance(config.get("general"), dict) else {}
        general["show_world"] = isinstance(entry, dict) and entry.get("value") is True
        config["general"] = general
        _save(conn, row_id, config)
