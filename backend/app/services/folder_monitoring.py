"""Folder activity contains attribution only. Detailed console reads recheck content access."""

from datetime import timezone

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


async def activity(db, org, user, managed, start, end, limit=50):
    if start and start.tzinfo:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
    if end and end.tzinfo:
        end = end.astimezone(timezone.utc).replace(tzinfo=None)

    query = (
        select(AE.id, AE.created_at, AE.status, AE.report_id, AE.completion_id, used.c.agent_id, DataSource.name)
        .join(used, used.c.execution_id == AE.id)
        .join(DataSource, DataSource.id == used.c.agent_id)
        .where(
            AE.organization_id == org.id,
            DataSource.organization_id == org.id,
            AE.starting_folder_id.is_not(None),
            DataSource.deleted_at.is_(None),
        )
    )
    if managed is not None:
        query = query.where(used.c.agent_id.in_(managed))
    if start:
        query = query.where(AE.created_at >= start)
    if end:
        query = query.where(AE.created_at <= end)
    rows = await db.execute(query.order_by(AE.created_at.desc(), AE.id, used.c.agent_id).limit(limit))
    results, permissions = [], {}
    for eid, date, status, rid, cid, aid, name in rows:
        if rid not in permissions:
            report = await db.get(Report, rid)
            permissions[rid] = bool(report and await can_open(db, report, user, org, managed))
        results.append(
            dict(
                execution_id=eid,
                created_at=date,
                status=status,
                agent_id=aid,
                agent_name=name,
                completion_id=cid if permissions[rid] else None,
                report_id=rid if permissions[rid] else None,
            )
        )
    return results


def agent_links():
    """Agents attributed to reports for monitoring (explicit pins and observed use)."""
    from app.models.report_data_source_association import report_data_source_association as pins

    return (
        select(pins.c.report_id, pins.c.data_source_id)
        .union(select(AE.report_id, used.c.agent_id).join(used, used.c.execution_id == AE.id))
        .subquery()
    )


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
