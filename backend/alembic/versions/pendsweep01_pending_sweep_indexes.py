"""pending sweep perf indexes

Speeds up InstructionService.get_pending_change_instruction_ids (the "pending
sweep") which backs GET /api/instructions/counts and
GET /api/instructions/pending-changes. These fire on every /agents page mount.

Root cause (observed on prod Postgres: ~15s for an org with only ~7 live
pending, but tens of thousands of accumulated non-main builds + build_contents):

  1. build_contents has NO index on instruction_id or build_id on its own. Its
     only composite index is the UNIQUE constraint (build_id, instruction_id).
     The sweep filters `build_contents.instruction_id IN (...)` (main_text and
     base_text sub-queries) — instruction_id is the SECOND column of that unique
     index, so it is NOT a usable seek prefix and Postgres falls back to a
     sequential scan of the entire (huge) build_contents table on every call.
     It also joins on build_contents.build_id, which is likewise unindexed
     (Postgres/SQLite do not auto-index FK columns).

  2. instruction_builds is filtered by (organization_id, is_main, status,
     source). The existing (organization_id, is_main) index cannot narrow on
     status/source, so the candidate ("sug_rows") query scans every non-main
     build for the org before discarding the terminal (approved/rejected) ones.

Fix: add the FK indexes on build_contents and a composite index on
instruction_builds that covers the pending-candidate predicate. Behavior is
unchanged — same pending set, same SQL, just index-backed lookups.

Revision ID: pendsweep01
Revises: fileref01
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "pendsweep01"
down_revision: Union[str, None] = "fileref01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_index_if_not_exists(index_name: str, table_name: str, columns: list, **kwargs):
    """Create index only if it doesn't already exist (idempotent across reruns)."""
    try:
        op.create_index(index_name, table_name, columns, **kwargs)
    except Exception:
        pass


def _drop_index_if_exists(index_name: str, table_name: str):
    try:
        op.drop_index(index_name, table_name)
    except Exception:
        pass


def upgrade() -> None:
    # --- build_contents: index the two FK columns the sweep filters/joins on.
    # instruction_id is filtered directly (main_text / base_text sub-queries);
    # build_id is joined to instruction_builds. Neither is currently indexed on
    # its own (only the (build_id, instruction_id) UNIQUE constraint exists).
    _create_index_if_not_exists(
        "ix_build_contents_instruction_id", "build_contents", ["instruction_id"]
    )
    _create_index_if_not_exists(
        "ix_build_contents_build_id", "build_contents", ["build_id"]
    )
    _create_index_if_not_exists(
        "ix_build_contents_instruction_version_id",
        "build_contents",
        ["instruction_version_id"],
    )

    # --- instruction_builds: composite covering the pending-candidate predicate
    # (organization_id, is_main, status, source) + deleted_at. Lets the planner
    # SEEK the handful of live draft/pending_approval suggestion builds instead
    # of scanning every non-main build for the org.
    _create_index_if_not_exists(
        "ix_instruction_builds_pending_sweep",
        "instruction_builds",
        ["organization_id", "is_main", "status", "source", "deleted_at"],
    )


def downgrade() -> None:
    _drop_index_if_exists("ix_instruction_builds_pending_sweep", "instruction_builds")
    _drop_index_if_exists("ix_build_contents_instruction_version_id", "build_contents")
    _drop_index_if_exists("ix_build_contents_build_id", "build_contents")
    _drop_index_if_exists("ix_build_contents_instruction_id", "build_contents")
