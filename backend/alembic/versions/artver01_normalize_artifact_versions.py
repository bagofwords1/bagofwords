"""normalize artifacts into parent artifacts + artifact_versions

Revision ID: artver01
Revises: officejs01
Create Date: 2026-09-03 12:00:00.000000

Until now every row in ``artifacts`` was ONE VERSION of a dashboard / deck /
doc: ``title`` and ``mode`` were copied onto each row, ``version`` was minted
by whoever wrote the row, and nothing related versions of the same deliverable
to each other. This migration splits identity from history:

- the existing table is renamed to ``artifact_versions`` (same ids, so every
  soft reference — tool_executions JSON, fork_asset_refs, session events,
  thumbnails/{id}.png, pdfs/{id}.pdf — stays valid);
- a new ``artifacts`` parent table holds identity (report, org, creator,
  mode, title);
- backfill is deliberately cheap: every existing version row gets a parent of
  its own (no lineage reconstruction — re-pointing ``artifact_id`` later can
  merge chains); version numbers are not touched;
- ``artifact_id`` then becomes NOT NULL with a UNIQUE(artifact_id, version),
  and ``title`` / ``mode`` leave the version rows.

Index choreography: index names are schema-global on both dialects, and the
new parent table reuses the ``ix_artifacts_*`` names, so the old indexes are
dropped up front (SQLite has no ALTER INDEX RENAME). On Postgres the pkey's
backing index is renamed so the parent's ``artifacts_pkey`` can exist.

Runs on both Postgres and SQLite (the test suite replays the full chain on
each). ``_backfill_parents`` is module-level so tests can exercise it alone.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid


# revision identifiers, used by Alembic.
revision: str = 'artver01'
down_revision: Union[str, Sequence[str], None] = 'officejs01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The 7 indexes the old artifacts table carries (n9o0p1q2r3s4 + inline
# index=True columns). Dropped up front; the surviving columns get them back
# under ix_artifact_versions_* names at the end of upgrade().
_OLD_INDEXES = (
    'ix_artifacts_report_id',
    'ix_artifacts_user_id',
    'ix_artifacts_organization_id',
    'ix_artifacts_completion_id',
    'ix_artifacts_mode',
    'ix_artifacts_status',
    'ix_artifacts_report_created',
)

_CHUNK = 500


def _versions_table_lite() -> sa.Table:
    """Just the columns the backfill touches — not the full model."""
    return sa.table(
        'artifact_versions',
        sa.column('id', sa.String(36)),
        sa.column('artifact_id', sa.String(36)),
    )


def _parents_table_lite() -> sa.Table:
    # Timestamp columns deliberately untyped: the backfill re-inserts whatever
    # the raw SELECT returned — datetime objects on Postgres, strings on
    # SQLite — and a sa.DateTime() bind would reject the SQLite strings.
    return sa.table(
        'artifacts',
        sa.column('id'),
        sa.column('report_id'),
        sa.column('organization_id'),
        sa.column('created_by'),
        sa.column('mode'),
        sa.column('title'),
        sa.column('created_at'),
        sa.column('updated_at'),
        sa.column('deleted_at'),
    )


def _backfill_parents(conn) -> int:
    """One parent per existing version row (decision D4: no lineage guessing).

    Copies report/org/user->created_by/mode/title and the row's timestamps —
    deleted_at included, so a soft-deleted version does not resurface as a
    live artifact in parent-table scans. Returns the number of parents made.
    """
    rows = conn.execute(sa.text(
        "SELECT id, report_id, organization_id, user_id, mode, title, "
        "created_at, updated_at, deleted_at FROM artifact_versions"
    )).fetchall()

    parents = []
    links = []
    for r in rows:
        parent_id = str(uuid.uuid4())
        parents.append({
            'id': parent_id,
            'report_id': r.report_id,
            'organization_id': r.organization_id,
            'created_by': r.user_id,
            'mode': r.mode,
            'title': r.title,
            'created_at': r.created_at,
            'updated_at': r.updated_at,
            'deleted_at': r.deleted_at,
        })
        links.append({'aid': parent_id, 'vid': r.id})

    parents_t = _parents_table_lite()
    for i in range(0, len(parents), _CHUNK):
        conn.execute(sa.insert(parents_t), parents[i:i + _CHUNK])
    update_sql = sa.text("UPDATE artifact_versions SET artifact_id = :aid WHERE id = :vid")
    for i in range(0, len(links), _CHUNK):
        conn.execute(update_sql, links[i:i + _CHUNK])
    return len(parents)


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Free the ix_artifacts_* names (schema-global) for the parent table.
    for name in _OLD_INDEXES:
        op.drop_index(name, table_name='artifacts')

    # 2. Postgres: the pkey's backing index is schema-global too. SQLite's
    #    rowid pkey has no named index and follows the rename on its own.
    if conn.dialect.name == 'postgresql':
        op.execute('ALTER INDEX artifacts_pkey RENAME TO artifact_versions_pkey')

    # 3. The version rows keep their table — under its real name.
    op.rename_table('artifacts', 'artifact_versions')
    op.add_column('artifact_versions', sa.Column('artifact_id', sa.String(36), nullable=True))

    # 4. The new identity table.
    op.create_table(
        'artifacts',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('title', sa.String(255), nullable=True),
    )
    op.create_index('ix_artifacts_report_id', 'artifacts', ['report_id'])
    op.create_index('ix_artifacts_organization_id', 'artifacts', ['organization_id'])
    op.create_index('ix_artifacts_created_by', 'artifacts', ['created_by'])
    op.create_index('ix_artifacts_mode', 'artifacts', ['mode'])

    # 5. One parent per version row.
    _backfill_parents(conn)

    # 6. Lock the link down; title/mode now live on the parent alone.
    #    One batch block = one table rebuild on SQLite.
    with op.batch_alter_table('artifact_versions') as batch:
        batch.alter_column('artifact_id', existing_type=sa.String(36), nullable=False)
        batch.create_foreign_key(
            'fk_artifact_versions_artifact_id', 'artifacts',
            ['artifact_id'], ['id'],
        )
        batch.create_unique_constraint(
            'uq_artifact_versions_artifact_version', ['artifact_id', 'version'],
        )
        batch.drop_column('mode')
        batch.drop_column('title')

    # 7. Indexes for the version rows, under their own names.
    op.create_index('ix_artifact_versions_report_id', 'artifact_versions', ['report_id'])
    op.create_index('ix_artifact_versions_user_id', 'artifact_versions', ['user_id'])
    op.create_index('ix_artifact_versions_organization_id', 'artifact_versions', ['organization_id'])
    op.create_index('ix_artifact_versions_completion_id', 'artifact_versions', ['completion_id'])
    op.create_index('ix_artifact_versions_status', 'artifact_versions', ['status'])
    op.create_index('ix_artifact_versions_artifact_id', 'artifact_versions', ['artifact_id'])
    op.create_index('ix_artifact_versions_report_created', 'artifact_versions', ['report_id', 'created_at'])


def downgrade() -> None:
    conn = op.get_bind()

    # Bring title/mode back onto the version rows (nullable until backfilled).
    op.add_column('artifact_versions', sa.Column('title', sa.String(255), nullable=True))
    op.add_column('artifact_versions', sa.Column('mode', sa.String(20), nullable=True))
    conn.execute(sa.text(
        "UPDATE artifact_versions SET "
        "title = (SELECT a.title FROM artifacts a WHERE a.id = artifact_versions.artifact_id), "
        "mode = (SELECT a.mode FROM artifacts a WHERE a.id = artifact_versions.artifact_id)"
    ))

    for name in (
        'ix_artifact_versions_report_id',
        'ix_artifact_versions_user_id',
        'ix_artifact_versions_organization_id',
        'ix_artifact_versions_completion_id',
        'ix_artifact_versions_status',
        'ix_artifact_versions_artifact_id',
        'ix_artifact_versions_report_created',
    ):
        op.drop_index(name, table_name='artifact_versions')

    with op.batch_alter_table('artifact_versions') as batch:
        batch.drop_constraint('uq_artifact_versions_artifact_version', type_='unique')
        batch.drop_constraint('fk_artifact_versions_artifact_id', type_='foreignkey')
        batch.drop_column('artifact_id')
        batch.alter_column('mode', existing_type=sa.String(20), nullable=False)

    op.drop_index('ix_artifacts_report_id', table_name='artifacts')
    op.drop_index('ix_artifacts_organization_id', table_name='artifacts')
    op.drop_index('ix_artifacts_created_by', table_name='artifacts')
    op.drop_index('ix_artifacts_mode', table_name='artifacts')
    op.drop_table('artifacts')

    if conn.dialect.name == 'postgresql':
        op.execute('ALTER INDEX artifact_versions_pkey RENAME TO artifacts_pkey')
    op.rename_table('artifact_versions', 'artifacts')

    # The original 7 indexes, under their original names.
    op.create_index('ix_artifacts_report_id', 'artifacts', ['report_id'])
    op.create_index('ix_artifacts_user_id', 'artifacts', ['user_id'])
    op.create_index('ix_artifacts_organization_id', 'artifacts', ['organization_id'])
    op.create_index('ix_artifacts_completion_id', 'artifacts', ['completion_id'])
    op.create_index('ix_artifacts_mode', 'artifacts', ['mode'])
    op.create_index('ix_artifacts_status', 'artifacts', ['status'])
    op.create_index('ix_artifacts_report_created', 'artifacts', ['report_id', 'created_at'])
