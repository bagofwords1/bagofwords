"""Recover pre-upgrade eval targets where the submission scope is corroborated."""

from alembic import op
import sqlalchemy as sa

revision = "folder09"
down_revision = "folder08"
branch_labels = depends_on = None


def upgrade():
    db = op.get_bind()
    results = sa.table("test_results", sa.column("id"), sa.column("data_source_ids_json", sa.JSON))
    # Main wrote per-result test reports with the case's selected agents.
    # Require an unedited case and matching report attachments. An empty or
    # ambiguous historical scope must continue to require org-level access.
    query = sa.text("""
        SELECT r.id, c.data_source_ids_json AS targets, a.data_source_id AS agent
        FROM test_results r
        JOIN test_cases c ON c.id = r.case_id
        JOIN reports p ON p.id = r.report_id AND p.report_type = 'test'
        JOIN report_data_source_association a ON a.report_id = p.id
        WHERE r.data_source_ids_json IS NULL AND r.folder_id IS NULL
          AND p.agent_folder_id IS NULL AND c.updated_at <= r.created_at
        ORDER BY r.id
    """).columns(targets=sa.JSON)
    from itertools import groupby

    for result_id, rows in groupby(db.execute(query).mappings(), key=lambda row: row["id"]):
        rows = list(rows)
        targets = rows[0]["targets"]
        if targets and set(targets) == {row["agent"] for row in rows}:
            db.execute(results.update().where(results.c.id == result_id).values(data_source_ids_json=targets))


def downgrade():
    # These recovered snapshots are valid at folder08 as well.
    pass
