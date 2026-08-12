"""backfill bounded report payload summaries

Revision ID: rptperf001
Revises: ctxsum0001
Create Date: 2026-08-12 00:00:00.000000

Existing full JSON values remain untouched. The migration reads each relevant
legacy value once and stores the same bounded projection that new writes create
synchronously, so opening an old report never has to decode its full history.

Rows are rebuilt when the summary is missing OR stale: version-1 summaries
written by the pre-upgrade hooks (deployed with ctxsum0001) lack the
ui_preview/rows/step_id fields that report read endpoints now serve directly,
and would otherwise render as empty cards forever.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.ai.persisted_summary import (
    CONTEXT_SUMMARY_VERSION,
    SUMMARIZED_TOOL_NAMES,
    build_step_context_summary,
    build_tool_context_summary,
)

revision: str = "rptperf001"
down_revision: str | None = "ctxsum0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 50


def _needs_rebuild(summary: object) -> bool:
    return not (
        isinstance(summary, dict)
        and summary.get("version") == CONTEXT_SUMMARY_VERSION
    )


def _backfill_steps(bind: sa.Connection) -> None:
    steps = sa.table(
        "steps",
        sa.column("id", sa.String()),
        sa.column("data", sa.JSON()),
        sa.column("context_summary_json", sa.JSON()),
    )
    last_id: str | None = None
    while True:
        statement = (
            sa.select(steps.c.id, steps.c.data, steps.c.context_summary_json)
            .where(steps.c.data.is_not(None))
            .order_by(steps.c.id)
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(steps.c.id > last_id)
        rows = bind.execute(statement).all()
        if not rows:
            return
        for step_id, data, summary in rows:
            if isinstance(data, dict) and _needs_rebuild(summary):
                bind.execute(
                    sa.update(steps)
                    .where(steps.c.id == step_id)
                    .values(context_summary_json=build_step_context_summary(data))
                )
        last_id = str(rows[-1].id)


def _backfill_tool_executions(bind: sa.Connection) -> None:
    executions = sa.table(
        "tool_executions",
        sa.column("id", sa.String()),
        sa.column("tool_name", sa.String()),
        sa.column("result_json", sa.JSON()),
        sa.column("context_summary_json", sa.JSON()),
    )
    last_id: str | None = None
    while True:
        statement = (
            sa.select(
                executions.c.id,
                executions.c.tool_name,
                executions.c.result_json,
                executions.c.context_summary_json,
            )
            .where(
                executions.c.result_json.is_not(None),
                executions.c.tool_name.in_(sorted(SUMMARIZED_TOOL_NAMES)),
            )
            .order_by(executions.c.id)
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(executions.c.id > last_id)
        rows = bind.execute(statement).all()
        if not rows:
            return
        for execution_id, tool_name, result_json, existing_summary in rows:
            if not _needs_rebuild(existing_summary):
                continue
            summary = build_tool_context_summary(tool_name, result_json)
            if isinstance(summary, dict):
                bind.execute(
                    sa.update(executions)
                    .where(executions.c.id == execution_id)
                    .values(context_summary_json=summary)
                )
        last_id = str(rows[-1].id)


def upgrade() -> None:
    bind = op.get_bind()
    _backfill_steps(bind)
    _backfill_tool_executions(bind)


def downgrade() -> None:
    # Full payloads were never changed. Summaries are harmless on downgrade and
    # cannot be distinguished from summaries written after deployment.
    pass
