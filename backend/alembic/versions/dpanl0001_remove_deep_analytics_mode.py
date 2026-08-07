"""Remove deep analytics mode.

Deep mode is gone: the UI no longer offers it and the API schemas no longer
accept it. Rows still carrying ``mode='deep'`` have to be rewritten before the
tightened ``Literal``s in ``report_schema`` / ``webhook_schema`` see them —
those are *response* models, so a single stale row would 500 the whole
``GET /reports`` list, not just the one conversation.

Ordering matters. Prompts and webhooks are the generators: a scheduled prompt
or a trigger with ``mode='deep'`` stamps that mode onto every report it spawns
(``prompt_service._create_report_for``, ``webhook_service`` trigger spawn). So
they are rewritten FIRST — cleaning ``reports`` while a schedule can still fire
leaves the table dirty again minutes later.

Instructions are a separate case. ``_passes_mode_channel`` already tolerates an
unknown mode in the list, so ``['chat','deep']`` keeps matching chat; only a
``['deep']``-ONLY instruction breaks (it silently matches nothing). Those map to
``['chat']`` rather than ``[]``, because ``[]`` means "all modes" — stripping to
empty would promote a narrowly-scoped instruction into a globally-active one.

``instruction_versions`` is deliberately NOT rewritten: build_service diffs
``applicable_modes`` between versions to compute changed_fields, so editing
history would make every historical version register as changed.
"""
from typing import Sequence, Union
import json

from alembic import op
import sqlalchemy as sa


revision: str = 'dpanl0001'
down_revision: Union[str, None] = 'bcbase001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rewrite_applicable_modes(conn, table: str) -> None:
    """Drop 'deep' from a JSON applicable_modes list.

    A deep-only list becomes ['chat'] (nearest surviving mode), never [] —
    empty means "applies to every mode" and would widen the instruction's
    scope instead of narrowing it.
    """
    rows = conn.execute(
        sa.text(f"SELECT id, applicable_modes FROM {table} "
                f"WHERE applicable_modes IS NOT NULL")
    ).fetchall()
    for row_id, raw in rows:
        if not raw:
            continue
        try:
            modes = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(modes, list) or 'deep' not in modes:
            continue
        kept = [m for m in modes if m != 'deep']
        if not kept:
            kept = ['chat']
        conn.execute(
            sa.text(f"UPDATE {table} SET applicable_modes = :m WHERE id = :i"),
            {"m": json.dumps(kept), "i": row_id},
        )


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Generators first — stop new deep reports being minted mid-migration.
    conn.execute(sa.text("UPDATE webhooks SET mode = 'chat' WHERE mode = 'deep'"))
    conn.execute(sa.text("UPDATE prompts  SET mode = 'chat' WHERE mode = 'deep'"))

    # 2. Then what they already produced.
    conn.execute(sa.text("UPDATE reports  SET mode = 'chat' WHERE mode = 'deep'"))

    # 3. Live instruction scoping only; version history stays immutable.
    _rewrite_applicable_modes(conn, 'instructions')


def downgrade() -> None:
    # Not reversible: 'deep' rows are indistinguishable from genuine 'chat'
    # rows once rewritten, and the deep-only instruction scoping is lost.
    pass
