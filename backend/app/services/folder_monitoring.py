"""Agent attribution for runs (exposure, observed use) and the monitoring visibility
rules built on it. Detailed console reads recheck content access."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.agent_execution import AgentExecution as AE, folder_execution_agents as used
from app.models.report import Report, folder_report_knowledge as knowledge, folder_report_sources as roster
from app.models.data_source import DataSource


async def record_used_agents(db, execution_id, agents, outcomes, resolved_file_agents):
    from app.ai.llm.header_injection import agent_ids_from_tool_input, agent_ids_from_tool_output

    ids = set(resolved_file_agents or ())
    connections = set()
    for outcome in outcomes:
        observation = outcome.get("observation") or {}
        if (
            outcome.get("skipped")
            or outcome.get("error")
            or observation.get("error")
            or observation.get("success") is False
        ):
            continue
        output = outcome.get("tool_output") or {}
        if isinstance(output, dict) and (output.get("error") or output.get("success") is False):
            continue
        tool = outcome.get("tool_name")
        args = outcome.get("tool_input") or {}
        if tool == "execute_mcp" and isinstance(args, dict) and args.get("connection_id"):
            connections.add(args["connection_id"])
        ids.update(agent_ids_from_tool_input(tool, args))
        ids.update(agent_ids_from_tool_output(tool, output))
    allowed = {str(a.id) for a in agents}
    if connections:
        from app.models.domain_connection import domain_connection as links

        pairs = await db.execute(
            select(links.c.connection_id, links.c.data_source_id).where(
                links.c.connection_id.in_(connections), links.c.data_source_id.in_(allowed)
            )
        )
        # Shared connections alone cannot identify which agent performed the operation.
        by_connection = {}
        for connection_id, agent_id in pairs:
            by_connection.setdefault(connection_id, set()).add(agent_id)
        for candidates in by_connection.values():
            if len(candidates) == 1:
                ids.update(candidates)
    ids.intersection_update(allowed)
    if ids:
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        await db.execute(
            insert(used)
            .values([{"execution_id": execution_id, "agent_id": a} for a in sorted(ids)])
            .on_conflict_do_nothing()
        )


async def record_exposed_agents(db, execution_id, agent_ids):
    """Add agents to a run's exposure (idempotent; the set only grows).

    Commits on its own, like the other run-tracking writes, so the row survives
    a run that fails afterwards — a run whose exposure is missing is hidden from
    scoped managers, never shown with too little attribution.
    """
    from app.models.agent_execution import execution_exposed_agents as exposed

    ids = sorted({str(a) for a in agent_ids or () if a and not str(a).startswith("builtin:")})
    if not execution_id or not ids:
        return
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(exposed)
        .values([{"execution_id": str(execution_id), "agent_id": a} for a in ids])
        .on_conflict_do_nothing()
    )
    await db.commit()


def historic_folder_reports():
    return select(knowledge.c.report_id).union(
        select(roster.c.report_id),
        select(Report.id).where(Report.agent_folder_id.is_not(None)),
        select(AE.report_id).where(AE.starting_folder_id.is_not(None), AE.report_id.is_not(None)),
    )


async def can_open(db, report, user, org, managed):
    from app.core.permission_resolver import resolve_permissions
    from app.services.folder_knowledge import can_read_knowledge_scope
    from app.models.report_data_source_association import report_data_source_association as pins

    rights = await resolve_permissions(db, user.id, org.id)
    starts = await db.execute(select(AE.starting_folder_id, AE.starting_folder_ids)
        .where(AE.report_id == report.id, AE.starting_folder_id.is_not(None)))
    origins = {fid for primary, folders in starts for fid in folders or [primary]}
    from app.services.folder_service import load_report_folders
    origins.update(await load_report_folders(db, report))
    if any(not rights.has_resource_permission("folder", fid, "view") for fid in origins):
        return False
    if not await can_read_knowledge_scope(db, report, org, user, rights):
        return False
    if managed is None:
        return True
    required = set(
        await db.scalars(
            select(used.c.agent_id).join(AE, AE.id == used.c.execution_id).where(AE.report_id == report.id)
        )
    )
    required.update(await db.scalars(select(pins.c.data_source_id).where(pins.c.report_id == report.id)))
    # The stored trace includes the planner roster and context snapshots even
    # for agents that never received a tool call.
    required.update(await db.scalars(select(roster.c.agent_id).where(roster.c.report_id == report.id)))
    return bool(required and required <= set(managed))


def agent_links(include_exposure=False):
    """Agents attributed to reports for monitoring (explicit pins and observed
    use; with ``include_exposure``, also every agent a run was given)."""
    from app.models.agent_execution import execution_exposed_agents as exposed
    from app.models.report_data_source_association import report_data_source_association as pins

    from sqlalchemy import union

    parts = [
        select(pins.c.report_id, pins.c.data_source_id),
        select(AE.report_id, used.c.agent_id).join(used, used.c.execution_id == AE.id),
    ]
    if include_exposure:
        parts.append(select(AE.report_id, exposed.c.agent_id).join(exposed, exposed.c.execution_id == AE.id))
    return union(*parts).subquery()


# ---------------------------------------------------------------------------
# Scoped-manager visibility (see app.core.console_access).
#
# A run's EXPOSURE is every agent it was given (execution_exposed_agents) plus
# every agent it used. A scoped manager may see a run when:
#
#   * its exposure is non-empty and every agent in it is one they manage, AND
#   * the same holds for every EARLIER run in the same report — a run's context
#     replays the conversation so far (prompts, answers, query results), so a
#     turn that touched an unmanaged agent taints every turn after it.
#
# Runs recorded before exposure existed fall back to the old attribution: the
# report's pinned agents and observed use. An Auto run from back then has
# neither and stays hidden, exactly as before.
# ---------------------------------------------------------------------------

def out_of_scope_runs(scope_ids):
    """Runs that, on their own, reach an agent outside ``scope_ids`` or are
    attributable to no agent at all.

    Every helper here uses its own aliases and never correlates: they are
    embedded in queries that already select from agent_executions / reports,
    and auto-correlation would silently bind them to the outer row instead.
    """
    from sqlalchemy import and_, or_, union
    from sqlalchemy.orm import aliased
    from app.models.agent_execution import execution_exposed_agents as exposed
    from app.models.report_data_source_association import report_data_source_association as pins

    scope_ids = [str(s) for s in scope_ids]
    run = aliased(AE)
    recorded = select(exposed.c.execution_id).correlate(None)
    legacy = select(run.id).where(
        run.id.not_in(recorded),
        or_(
            run.report_id.is_(None),
            run.report_id.in_(select(pins.c.report_id).where(pins.c.data_source_id.not_in(scope_ids)).correlate(None)),
            and_(
                run.report_id.not_in(select(pins.c.report_id).correlate(None)),
                run.id.not_in(select(used.c.execution_id).correlate(None)),
            ),
        ),
    ).correlate(None)
    return union(
        select(exposed.c.execution_id).where(exposed.c.agent_id.not_in(scope_ids)).correlate(None),
        select(used.c.execution_id).where(used.c.agent_id.not_in(scope_ids)).correlate(None),
        legacy,
    )


def visible_runs(scope_ids):
    """Runs a manager of ``scope_ids`` may see: in scope themselves and with no
    out-of-scope run at or before them in the same report. None = org-wide."""
    if scope_ids is None:
        return None
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased

    run, earlier = aliased(AE), aliased(AE)
    tainted = exists().where(
        earlier.report_id == run.report_id,
        earlier.created_at <= run.created_at,
        earlier.id.in_(out_of_scope_runs(scope_ids)),
    ).correlate(run)
    return select(run.id).where(run.report_id.is_not(None), ~tainted).correlate(None)


def reports_in_scope(scope_ids):
    """Reports a manager of ``scope_ids`` may see IN FULL — every run visible.

    Used by report-level aggregates (messages, usage, cost, feedback) and
    drill-downs that list every run, which cannot show part of a report. A
    report counts once something attributes it (a run or a pinned agent), and a
    pin outside scope still excludes it, as before. None = org-wide.
    """
    if scope_ids is None:
        return None
    from sqlalchemy.orm import aliased
    from app.models.report_data_source_association import report_data_source_association as pins

    scope_ids = [str(s) for s in scope_ids]
    run, report = aliased(AE), aliased(Report)
    attributed = select(run.report_id).where(run.report_id.is_not(None)).correlate(None).union(
        select(pins.c.report_id).correlate(None))
    tainted = select(run.report_id).where(
        run.report_id.is_not(None), run.id.in_(out_of_scope_runs(scope_ids))).correlate(None)
    outside_pin = select(pins.c.report_id).where(pins.c.data_source_id.not_in(scope_ids)).correlate(None)
    return select(report.id).where(
        report.id.in_(attributed),
        report.id.not_in(tainted),
        report.id.not_in(outside_pin),
    ).correlate(None)


def execution_links():
    """Pinned agents for ordinary runs and observed agents for folder runs."""
    from app.models.report_data_source_association import report_data_source_association as pins

    return (
        select(AE.id.label("execution_id"), pins.c.data_source_id)
        .join(pins, pins.c.report_id == AE.report_id)
        .where(AE.starting_folder_id.is_(None))
        .union(select(used.c.execution_id, used.c.agent_id))
        .subquery()
    )


def detail_condition(report_id, readable):
    from sqlalchemy import or_, true

    if readable is None:
        return true()
    return or_(report_id.is_(None), report_id.not_in(historic_folder_reports()), report_id.in_(readable))


async def readable_reports(db, org, user, managed):
    candidates = select(Report).where(Report.organization_id == org.id, Report.id.in_(historic_folder_reports()))
    if managed is not None:
        links = agent_links()
        candidates = candidates.where(
            Report.id.in_(
                select(links.c.report_id)
                .where(links.c.data_source_id.in_(managed))
                .union(select(roster.c.report_id).where(roster.c.agent_id.in_(managed)))
            )
        )
    from sqlalchemy.orm import lazyload

    reports = (await db.scalars(candidates.options(lazyload("*")))).all()
    readable = [r.id for r in reports if await can_open(db, r, user, org, managed)]
    if managed is None and len(readable) == len(reports):
        return None
    return readable
