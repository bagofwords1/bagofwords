"""Best-effort read-through persistence for pre-summary context rows.

Historical payloads have to be parsed once to recover the bounded prompt
projection. Persist that projection after the legacy read so active reports
pay the cost once, without a fleet-wide migration transaction.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.persisted_summary import (
    build_step_context_summary_from_projection,
    build_tool_context_summary,
)
from app.models.step import Step
from app.models.tool_execution import ToolExecution
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

TERMINAL_TOOL_EXECUTION_STATUSES = frozenset({"success", "error", "failed", "completed", "cancelled", "canceled"})
_WRITE_TIMEOUT_SECONDS = 1.0


def _can_open_independent_session(source_db: AsyncSession) -> bool:
    """In-memory SQLite uses one shared connection; a second session is unsafe."""
    bind = source_db.bind
    if bind is None:
        return False
    url = bind.url
    return not (url.get_backend_name() == "sqlite" and (url.database is None or url.database in {"", ":memory:"}))


async def persist_tool_context_projections(
    source_db: AsyncSession,
    projections: list[dict[str, Any]],
) -> None:
    """Persist terminal tool projections without touching the caller's txn."""
    if not projections or not _can_open_independent_session(source_db):
        return

    async def _write() -> None:
        async with AsyncSession(bind=source_db.bind, expire_on_commit=False) as db:
            for projection in projections:
                status = str(projection.get("status") or "")
                if status not in TERMINAL_TOOL_EXECUTION_STATUSES:
                    continue
                summary = build_tool_context_summary(
                    projection.get("tool_name"),
                    projection.get("result_json"),
                )
                if not isinstance(summary, dict):
                    continue
                source_updated_at = projection.get("updated_at")
                await db.execute(
                    update(ToolExecution)
                    .where(ToolExecution.id == str(projection["id"]))
                    .where(ToolExecution.context_summary_json.is_(None))
                    .where(ToolExecution.status.in_(TERMINAL_TOOL_EXECUTION_STATUSES))
                    .where(ToolExecution.updated_at == source_updated_at)
                    .values(
                        context_summary_json=summary,
                        # A cache write must not make history look newly updated.
                        updated_at=source_updated_at,
                    )
                )
            await db.commit()

    try:
        async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
            await _write()
    except Exception as exc:
        logger.warning("Legacy tool context summary write-through skipped: %s", exc)


async def persist_step_context_projections(
    source_db: AsyncSession,
    projections: list[dict[str, Any]],
) -> None:
    """Persist Step projections with update and retention race guards."""
    if not projections or not _can_open_independent_session(source_db):
        return

    async def _write() -> None:
        async with AsyncSession(bind=source_db.bind, expire_on_commit=False) as db:
            for projection in projections:
                source_updated_at = projection.get("updated_at")
                summary = build_step_context_summary_from_projection(
                    info=projection.get("info"),
                    columns=projection.get("columns"),
                    preview_rows=projection.get("preview_rows"),
                )
                await db.execute(
                    update(Step)
                    .where(Step.id == str(projection["id"]))
                    .where(Step.context_summary_json.is_(None))
                    .where(Step.updated_at == source_updated_at)
                    # Never recreate a summary after the retention job purges
                    # its canonical data between projection and write-through.
                    .where(Step.data.is_not(None))
                    .values(
                        context_summary_json=summary,
                        updated_at=source_updated_at,
                    )
                )
            await db.commit()

    try:
        async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
            await _write()
    except Exception as exc:
        logger.warning("Legacy Step context summary write-through skipped: %s", exc)
