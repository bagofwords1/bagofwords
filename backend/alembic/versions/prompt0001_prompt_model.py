"""prompt model: completion-shaped, multi-agent, scoped

Extends the existing `prompts` table into a reusable, access-scoped prompt
(mode/model/mentions/parameters/scope/is_starter) and adds a prompt<->data_source
many-to-many. No scheduling/subscription/channel columns — just the model.

Revision ID: prompt0001
Revises: c3f1a9b2d4e7
Create Date: 2026-06-26
"""
from typing import Sequence, Union
import json
import uuid
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision: str = 'prompt0001'
down_revision: Union[str, None] = 'c3f1a9b2d4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(insp, table: str, column: str) -> bool:
    try:
        return column in {c['name'] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = [
        sa.Column('mode', sa.String(), nullable=False, server_default='chat'),
        sa.Column('model_id', sa.String(length=36), nullable=True),
        sa.Column('mentions', sa.JSON(), nullable=True),
        sa.Column('parameters', sa.JSON(), nullable=True),
        sa.Column('scope', sa.String(), nullable=False, server_default='agent'),
        sa.Column('is_starter', sa.Boolean(), nullable=False, server_default=sa.false()),
    ]
    for col in cols:
        if not _has_column(insp, 'prompts', col.name):
            op.add_column('prompts', col)

    if 'prompt_data_source_association' not in insp.get_table_names():
        op.create_table(
            'prompt_data_source_association',
            sa.Column('prompt_id', sa.String(length=36), sa.ForeignKey('prompts.id'), nullable=True),
            sa.Column('data_source_id', sa.String(length=36), sa.ForeignKey('data_sources.id'), nullable=True),
        )

    _backfill_starters(bind)


def _backfill_starters(bind) -> None:
    """Migrate existing data_source.conversation_starters JSON into agent-scoped
    starter Prompts (idempotent). Lets old deployments carry their starters over."""
    rows = bind.execute(sa.text(
        "SELECT id, organization_id, owner_user_id, conversation_starters "
        "FROM data_sources WHERE conversation_starters IS NOT NULL AND deleted_at IS NULL"
    )).fetchall()

    # Already-materialized (ds_id, text) pairs — guard against double-runs.
    existing = set()
    try:
        link_rows = bind.execute(sa.text(
            "SELECT a.data_source_id, p.text FROM prompt_data_source_association a "
            "JOIN prompts p ON p.id = a.prompt_id WHERE p.is_starter = :tru"
        ), {"tru": True}).fetchall()
        existing = {(r[0], r[1]) for r in link_rows}
    except Exception:
        existing = set()

    now = datetime.utcnow()
    for ds_id, org_id, owner_id, starters in rows:
        if isinstance(starters, (str, bytes)):
            try:
                starters = json.loads(starters)
            except Exception:
                continue
        if not isinstance(starters, list):
            continue
        for raw in starters:
            text = raw if isinstance(raw, str) else (raw.get('value') if isinstance(raw, dict) else None)
            if not text or (ds_id, text) in existing:
                continue
            pid = str(uuid.uuid4())
            bind.execute(
                sa.text(
                    "INSERT INTO prompts (id, title, text, user_id, organization_id, mode, "
                    "scope, is_starter, created_at, updated_at) VALUES "
                    "(:id, :title, :text, :uid, :oid, 'chat', 'agent', :starter, :now, :now)"
                ),
                {"id": pid, "title": text[:60], "text": text, "uid": owner_id, "oid": org_id,
                 "starter": True, "now": now},
            )
            bind.execute(
                sa.text(
                    "INSERT INTO prompt_data_source_association (prompt_id, data_source_id) "
                    "VALUES (:pid, :ds)"
                ),
                {"pid": pid, "ds": ds_id},
            )
            existing.add((ds_id, text))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'prompt_data_source_association' in insp.get_table_names():
        op.drop_table('prompt_data_source_association')
    for col in ['mode', 'model_id', 'mentions', 'parameters', 'scope', 'is_starter']:
        if _has_column(insp, 'prompts', col):
            op.drop_column('prompts', col)
