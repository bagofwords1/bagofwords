"""Keep `http` in generated code for orgs that already had web fetch on.

Web fetch used to be one switch (`enable_web_fetch`) for both the agent's own
web_fetch tool and the `http` client generated code gets. The new Sandbox
settings split them: `enable_sandbox_web_fetch` (default off) now gates the
code side. Without this migration, every org that had web fetch on would
silently lose `http` in code — including saved steps and dashboards whose
code fetches URLs on refresh.

So: an org whose `enable_web_fetch` is on gets `enable_sandbox_web_fetch` on.
Orgs with it off (and new orgs) keep the default, off. An org that already
has the new key is left alone. Data only; downgrade is a no-op because the
key is harmless to keep and the old code ignores it.
"""
from typing import Sequence, Union
import json

from alembic import op
import sqlalchemy as sa


revision: str = 'sbxweb01'
down_revision: Union[str, None] = 'connfail01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_KEY = "enable_sandbox_web_fetch"
NEW_FEATURE = {
    "value": True,
    "name": "Web fetch in code",
    "description": "",  # refreshed from the schema on the next settings read
    "is_lab": False,
    "editable": True,
    "state": "enabled",
}


def _feature_on(entry) -> bool:
    if isinstance(entry, dict):
        if "value" in entry:
            return bool(entry.get("value"))
        return entry.get("state") == "enabled"
    return bool(entry)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, config FROM organization_settings")).fetchall()
    for row_id, raw in rows:
        try:
            config = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(config, dict) or NEW_KEY in config:
            continue
        ai = config.get("ai_features") if isinstance(config.get("ai_features"), dict) else {}
        old = config.get("enable_web_fetch", ai.get("enable_web_fetch"))
        if old is None or not _feature_on(old):
            continue
        config[NEW_KEY] = dict(NEW_FEATURE)
        conn.execute(
            sa.text("UPDATE organization_settings SET config = :c WHERE id = :i"),
            {"c": json.dumps(config), "i": row_id},
        )


def downgrade() -> None:
    pass
