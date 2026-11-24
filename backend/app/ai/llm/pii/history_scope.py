"""Agents whose data may already sit in a report's conversation history.

A run's PII agent scope (``data_source_ids`` in the usage attribution, see
``docs/design/pii-agent-policies.md``) starts from the agents the run is given.
But each run also reloads the report's earlier turns, and those can carry rows
from an agent that has since been detached. That agent's policy must keep
applying, or a follow-up turn would send its data without it.

The provenance is the run exposure already recorded for monitoring
(``execution_exposed_agents``: every agent each run was given). When it is
complete, the historical agents are added to the scope. When it isn't — an
earlier turn has no recorded run, a run's exposure is missing (written before
exposure existed, or the best-effort write failed), or the history was copied
from another report by a fork — the answer is ``None``: the scope is unknown and
every agent policy applies (fail closed).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import and_, exists, func, or_, select

logger = logging.getLogger(__name__)


async def historical_agent_ids(db, report_id: Optional[str], *, exclude_completion_id: Optional[str] = None) -> Optional[List[str]]:
    """Agent ids exposed to this report's earlier runs, sorted; ``[]`` for a
    report with no earlier turns; ``None`` when the history's provenance is
    incomplete (the caller then leaves the scope unknown)."""
    if not report_id:
        return []
    from app.models.agent_execution import AgentExecution, execution_exposed_agents as exposed
    from app.models.completion import Completion
    from app.models.report import Report

    try:
        report = (await db.execute(
            select(Report.forked_from_id, Report.forked_from_artifact_id).where(Report.id == report_id)
        )).one_or_none()
        if report is None:
            return []
        # History copied from elsewhere: its agents were never this report's runs.
        if report.forked_from_id or report.forked_from_artifact_id:
            return None
        earlier = [Completion.report_id == report_id, Completion.role == "system"]
        if exclude_completion_id:
            earlier.append(Completion.id != str(exclude_completion_id))
        forked_in = (await db.execute(
            select(func.count()).select_from(Completion).where(
                Completion.report_id == report_id,
                or_(Completion.source_report_id.is_not(None), Completion.is_fork_summary.is_not(None)),
            )
        )).scalar_one()
        if forked_in:
            return None
        # An earlier turn with no recorded run: nothing says which agents it used.
        unrecorded = (await db.execute(
            select(func.count()).select_from(Completion).where(
                *earlier,
                ~exists().where(AgentExecution.completion_id == Completion.id),
            )
        )).scalar_one()
        if unrecorded:
            return None
        # A recorded run whose exposure is missing.
        unexposed = (await db.execute(
            select(func.count()).select_from(AgentExecution).join(
                Completion, Completion.id == AgentExecution.completion_id
            ).where(
                *earlier,
                ~exists().where(exposed.c.execution_id == AgentExecution.id),
            )
        )).scalar_one()
        if unexposed:
            return None
        ids = (await db.execute(
            select(exposed.c.agent_id).distinct().join(
                AgentExecution, AgentExecution.id == exposed.c.execution_id
            ).join(
                Completion, Completion.id == AgentExecution.completion_id
            ).where(and_(*earlier))
        )).scalars().all()
        return sorted({str(i) for i in ids})
    except Exception:  # pragma: no cover - defensive: unknown is the safe answer
        logger.warning("PII history scope lookup failed for report %s", report_id, exc_info=True)
        return None
