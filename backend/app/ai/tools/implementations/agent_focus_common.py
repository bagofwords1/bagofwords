"""Shared helpers for the agent focus tools (search_agents / set_report_agents).

"Agent" == ``DataSource``. Selection semantics:

  - ``report.data_sources`` EMPTY = **Auto**: the user delegated scoping, so the
    working set is every agent they can access and the tools act freely.
  - Non-empty = **manual**: the user picked agents, and that selection is a HARD
    scope — search and focus operate on the selected agents only; other
    accessible agents are never searched, loaded, or proposed. The user changes
    the scope themselves from the agent selector.
  - **training**: the actor curates agents they MANAGE (org-level
    ``manage_instructions`` → all agents, or a per-agent ``manage`` grant).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


# The capability that means "manages this agent" — mirrors report_service's
# training-mode gate. A per-agent `manage` grant implies it; org-level holders
# manage every agent.
MANAGE_PERMISSION = "manage_instructions"


async def accessible_agents(db, organization: Any, user: Any) -> List[Any]:
    """All live agents this user can use: org-scoped, active, not disabled,
    public OR granted (full admin sees all). Connections eager-loaded."""
    from app.core.permission_resolver import get_accessible_data_source_ids
    from app.models.data_source import DataSource

    stmt = (
        select(DataSource)
        .options(selectinload(DataSource.connections))
        .where(
            DataSource.organization_id == str(organization.id),
            DataSource.deleted_at.is_(None),
            DataSource.is_active == True,  # noqa: E712
            DataSource.publish_status != "disabled",
        )
        .order_by(DataSource.name)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    from app.services.connected_application_service import filter_agents
    rows = await filter_agents(db, rows, organization.id)
    from app.services.data_source_service import DataSourceService
    from app.core.permission_resolver import resolve_permissions
    rights = await resolve_permissions(db, str(user.id), str(organization.id)) if user else None
    rows = [ds for ds in rows if DataSourceService.lifecycle_visible(ds, rights)]
    if user is None:
        return [ds for ds in rows if getattr(ds, "is_public", False)]
    is_admin, member_ids = await get_accessible_data_source_ids(
        db, str(user.id), str(organization.id)
    )
    if is_admin:
        return rows
    allowed = set(str(x) for x in (member_ids or []))
    return [ds for ds in rows if getattr(ds, "is_public", False) or str(ds.id) in allowed]


async def signin_required_ids(db, agents: List[Any], user: Any) -> set:
    """Ids of agents the user can SEE but cannot USE yet — user_required auth
    with no personal credentials and no service-account fallback (e.g. PowerBI
    OBO before sign-in). Callers surface these as "sign-in required" instead of
    attaching them and failing mid-query."""
    if not agents:
        return set()
    try:
        from app.services.data_source_service import DataSourceService
        usable, _skipped = await DataSourceService().filter_user_usable_data_sources(
            db, list(agents), user
        )
        usable_ids = {str(d.id) for d in usable}
        return {str(d.id) for d in agents} - usable_ids
    except Exception:
        return set()


def agent_is_unreachable(ds: Any) -> bool:
    """True when every connection of the agent is flagged unreachable.

    ``Connection.is_active`` is the cached health flag a failed probe clears
    (ConnectionService._record_probe_outcome). construct_clients builds no
    client for such a connection, so an agent with nothing else is dropped from
    the run. Tool-provider connections (MCP / custom API / remote server) are
    built whatever the flag says, so one of those keeps the agent reachable.
    """
    conns = list(getattr(ds, "connections", None) or [])
    if not conns:
        return False
    from app.services.connection_tool_gateway import TOOL_CONNECTION_TYPES
    return all(
        getattr(c, "is_active", True) is False and getattr(c, "type", None) not in TOOL_CONNECTION_TYPES
        for c in conns
    )


def agent_unreachable_since(ds: Any):
    """When an unreachable agent's last connection went down (UTC), or None
    when that is not known for every connection. The agent was still usable
    through whichever connection went down last, so that one dates it."""
    stamps = [getattr(c, "unreachable_since", None) for c in (getattr(ds, "connections", None) or [])]
    if not stamps or any(s is None for s in stamps):
        return None
    return max(stamps)


def format_since(stamp) -> Optional[str]:
    """A stable, minute-precision UTC stamp for prompts and tool results."""
    return stamp.strftime("%Y-%m-%dT%H:%MZ") if stamp else None


async def folder_branch_agents(db, organization: Any, user: Any, report: Any) -> Tuple[List[Any], bool]:
    """``(agents, queryable)`` for a folder-scoped report: every agent the user
    can access in the report's folders (plus its pins), BEFORE the sign-in
    filter. ``queryable`` is False when no folder may be queried at all (the
    user only curates them in training)."""
    from app.services.folder_service import load_report_folders, training_curator_only, visible_branch
    attached = list(getattr(report, "data_sources", None) or [])
    branch: set = set()
    queryable = False
    for fid in await load_report_folders(db, report):
        if await training_curator_only(db, organization.id, getattr(user, "id", None), fid, getattr(report, "mode", None)):
            continue
        queryable = True
        branch.update(await visible_branch(db, organization.id, getattr(user, "id", None), fid))
    if not queryable and not attached:
        return [], False
    chosen = {a.id for a in attached}
    agents = [a for a in await accessible_agents(db, organization, user) if a.folder_id in branch or a.id in chosen]
    return agents, queryable


async def signin_scope_agents(db, organization: Any, user: Any, report: Any,
                              working_set: Optional[List[Any]] = None) -> List[Any]:
    """Every agent and connector this report's runs may reach for this user,
    including the ones a run leaves out because the user has not signed in.

    ``working_set`` is the run's own agents when the caller has them (AgentV2);
    otherwise it is resolved here. It already holds the person's own connectors
    (mail, Teams, Drive, per-user MCP), connected or not. A folder-scoped
    report's agents are added on top: resolve_run_agents filters those by
    sign-in before the run starts, so a working set never contains them.

    Callers pass the result to ``signin_required_ids`` to learn which of them
    actually need this user's sign-in.
    """
    out: List[Any] = []
    seen: set = set()

    def _add(items):
        for ds in items or []:
            key = str(getattr(ds, "id", ""))
            if key and key not in seen:
                seen.add(key)
                out.append(ds)

    if working_set is None:
        try:
            working_set = await resolve_run_agents(db, organization, user, report)
        except Exception:
            working_set = []
    _add(working_set)
    if report is None or user is None:
        return out
    try:
        if getattr(report, "agent_folder_id", None):
            agents, _ = await folder_branch_agents(db, organization, user, report)
            from app.services.data_source_service import DataSourceService
            _add(a for a in agents if DataSourceService.is_execution_live(a))
    except Exception:
        logger.warning("sign-in scope: folder agents unavailable", exc_info=True)
    return out


def run_scope_agents(runtime_ctx: dict[str, Any]) -> list[Any]:
    """The agents a tool call may resolve against: the run's working set plus
    the report's own attachments, de-duplicated, attachments last.

    The working set (``runtime_ctx["run_agents"]``, from AgentV2) is what Auto
    resolves to — the running user's accessible agents — so a tool that only
    walked ``report.data_sources`` saw nothing under Auto. Attachments are
    added because a mid-run set_report_agents / create_agent attaches to the
    report. Callers without a working set (standalone tool calls, tests) get
    the attachments alone, exactly as before. No report means no scope.
    """
    report = runtime_ctx.get("report")
    if report is None:
        return []
    out: list[Any] = []
    seen: set = set()
    from app.core.request_origin import current
    run_agents = list(runtime_ctx.get("run_agents") or [])
    attachments = list(getattr(report, "data_sources", None) or [])
    if current() is not None and "run_agents" in runtime_ctx:
        allowed = {str(ds.id) for ds in run_agents}
        attachments = [ds for ds in attachments if str(ds.id) in allowed]
    for ds in run_agents + attachments:
        key = str(getattr(ds, "id", "") or id(ds))
        if key in seen:
            continue
        seen.add(key)
        out.append(ds)
    return out


def report_selection_is_auto(report: Any) -> bool:
    """Empty attached roster = Auto (the user never pinned a selection)."""
    return not list(getattr(report, "data_sources", None) or [])


def _is_personal_connector(ds: Any) -> bool:
    """A connector that reads with each person's OWN credentials (mail, Teams,
    personal Drive, per-user MCP) — as opposed to a shared-key connector."""
    from app.services.data_source_kind import is_connector
    if not is_connector(ds):
        return False
    return any(getattr(c, "auth_policy", None) == "user_required" for c in (getattr(ds, "connections", None) or []))


async def scheduled_run_reaches_others(db, scheduled_prompt_id: Optional[str], owner_id: Optional[str]) -> bool:
    """True when a scheduled prompt delivers its results to anyone other than
    its owner. Such a run must not read the owner's personal connectors: their
    mail or chats would land in other people's inboxes."""
    if not scheduled_prompt_id:
        return False
    try:
        from app.models.scheduled_prompt import ScheduledPrompt
        sp = await db.get(ScheduledPrompt, str(scheduled_prompt_id))
    except Exception:
        logger.warning("scheduled prompt %s unreadable; treating its output as shared", scheduled_prompt_id, exc_info=True)
        return True
    if sp is None:
        return False
    for sub in (getattr(sp, "notification_subscribers", None) or []):
        if not isinstance(sub, dict):
            return True
        if (sub.get("type") or "user") == "user" and str(sub.get("id") or "") == str(owner_id or sp.user_id):
            continue
        return True
    return False


async def resolve_run_agents(db, organization: Any, user: Any, report: Any,
                             agent_ids: Optional[List[str]] = None,
                             exclude_personal_connectors: bool = False) -> List[Any]:
    """The agents a run actually executes against — the working set.

    Auto is a MODE, not a set: "every agent I can access", resolved here at run
    time against the running user rather than frozen into the report. So an
    unattached report resolves to that user's accessible agents, and one that
    gains access to a new agent picks it up on its next run. A manual selection
    is the report's own snapshot and is used verbatim.

    Only live agents come back: an agent disabled or deactivated after being
    attached must not contribute a client or a schema (see AgentV2.__init__).
    Callers build clients over this list and hand the same list to AgentV2 as
    ``data_sources``, so context, roster and clients all describe one set.
    """
    from app.services.data_source_service import DataSourceService

    if report is None:
        # No report is not Auto — there is no scope to resolve. Returning the
        # whole roster here would hand a caller that never had a report every
        # agent the user can reach.
        return []
    attached = list(getattr(report, "data_sources", None) or [])
    # Artifact-chat threads are never Auto: their roster is the owner-allowed ∩
    # viewer-accessible intersection synced per message, and empty means
    # "dashboard data only" — widening to everything the viewer can access
    # would hand a shared-dashboard chat far more than the owner shared.
    if agent_ids is None and not attached and getattr(report, "report_type", "regular") == "artifact_chat":
        return []
    from app.services.folder_service import load_report_folders, training_curator_only
    if agent_ids is not None and getattr(report, "agent_folder_id", None):
        from app.services.folder_service import require_report_folder
        curator_only = False
        for fid in await load_report_folders(db, report):
            await require_report_folder(db, organization, user, fid, getattr(report, "mode", None))
            if await training_curator_only(db, organization.id, getattr(user, "id", None), fid, getattr(report, "mode", None)):
                curator_only = True
        if curator_only:
            return []
    if agent_ids is not None:
        # A run with its own agents (a scheduled task's selection): exactly
        # those, re-checked against what the running user can use today. Like a
        # pinned selection, the person's own connectors join it (see below).
        wanted = {str(i) for i in agent_ids}
        reachable = await accessible_agents(db, organization, user)
        agents = [a for a in reachable if str(a.id) in wanted]
        agents += [ds for ds in reachable if str(ds.id) not in wanted and _is_personal_connector(ds)]
    elif getattr(report, "agent_folder_id", None):
        in_branch, queryable = await folder_branch_agents(db, organization, user, report)
        if not queryable and not attached:
            return []
        agents, _ = await DataSourceService().filter_user_usable_data_sources(db, in_branch, user)
        if not agents and not in_branch:
            from fastapi import HTTPException
            raise HTTPException(409, "No available agents in this folder")
        # Agents are here but each needs this user's sign-in: the run starts
        # with none of them, and the planner is told which ones to ask the
        # user to connect (AgentV2._signin_required_agents). Failing the
        # request instead hid the one thing the user could do about it.
    elif attached:
        # A pinned selection is used as pinned, MCP tools included, so pinning
        # still narrows what a run offers. The person's OWN connectors (mail,
        # Teams, personal Drive, per-user MCP) join it anyway: they are that
        # person's data, never part of an agent choice. One the user never
        # signed into gets no client and AgentV2 drops it. Shared-key
        # connectors (an org-wide MCP) follow the selection like any agent.
        chosen = {str(ds.id) for ds in attached}
        reachable = await accessible_agents(db, organization, user)
        visible_pins = [ds for ds in reachable if str(ds.id) in chosen]
        if not visible_pins:
            from fastapi import HTTPException
            raise HTTPException(409, "A selected agent is unavailable")
        personal = [
            ds for ds in reachable
            if str(ds.id) not in chosen and _is_personal_connector(ds)
        ]
        agents = visible_pins + personal
    else:
        agents = await accessible_agents(db, organization, user)
    if exclude_personal_connectors:
        agents = [ds for ds in agents if not _is_personal_connector(ds)]
    # A connector the user switched OFF in the chat composer contributes
    # nothing: dropped here, it gets no client, no schema and no tool
    # definitions, and its tools never reach discovery or execution. Only
    # personal connectors are affected — a pinned agent is never filtered — and
    # an anonymous run (no user) has no preferences to apply. One indexed query.
    if user is not None:
        from app.services.connector_preference_service import get_disabled_connector_ids
        disabled = await get_disabled_connector_ids(
            db, str(getattr(user, "id", "") or ""), str(organization.id),
            data_source_ids=[str(ds.id) for ds in agents if _is_personal_connector(ds)],
        )
        if disabled:
            agents = [ds for ds in agents if str(ds.id) not in disabled]
    from app.services.connected_application_service import filter_agents
    agents = await filter_agents(db, agents, organization.id)
    return [ds for ds in agents if DataSourceService.is_execution_live(ds)]


def scope_report_to_run_agents(report: Any, agents: List[Any]) -> None:
    """Make this run see ``agents`` as the report's attached agents.

    The agent loop reads ``report.data_sources`` (roster, capabilities, focus)
    as well as its own working set. A run with its own agents must not see the
    report's other attachments there, so the in-memory collection is replaced
    WITHOUT marking it changed: nothing is written to the report, and another
    task or a chat on the same report keeps its own agents.
    """
    from sqlalchemy.orm.attributes import set_committed_value

    ids = {str(a.id) for a in agents}
    set_committed_value(report, "data_sources", list(agents))
    focus = [str(x) for x in (getattr(report, "focused_data_source_ids", None) or []) if str(x) in ids]
    set_committed_value(report, "focused_data_source_ids", focus or None)


def run_agents_context(agents: Any, report: Any) -> tuple:
    """``(run_agent_ids, pinned_agent_ids)`` for construct_clients: the run's
    agents and the ones the report pins — what the chat tools and the planner
    note scope a shared Slack connection by."""
    return ([str(a.id) for a in (agents or [])],
            [str(d.id) for d in (getattr(report, "data_sources", None) or [])])


async def construct_agent_clients(svc, db, ds: Any, user: Any, *, where: str,
                                  run_agents: Optional[tuple] = None) -> Dict[str, Any]:
    """Build one agent's clients per connection, for run start and for agents
    attached mid-run alike. A connection that cannot be set up gets a stand-in
    client (see construct_clients) and is logged; when every connection fails,
    construct_clients raises."""
    connection_errors: list = []
    extra = {"run_agents": run_agents} if run_agents is not None else {}
    clients = await svc.construct_clients(db, ds, user, connection_errors=connection_errors, **extra) or {}
    for ce in connection_errors:
        logger.warning(
            "%s: connection %s on %s unavailable for this run — %s",
            where, ce.get("connection_name"), getattr(ds, "name", "?"), ce.get("error"),
        )
    return clients


async def prepare_run_agents(db, organization: Any, user: Any, report: Any,
                             data_source_service: Any = None,
                             agent_ids: Optional[List[str]] = None,
                             exclude_personal_connectors: bool = False, folder_ids=None) -> Tuple[List[Any], Dict[str, Any]]:
    """``(agents, clients)`` for a run — the working set and its query clients.

    An agent the user cannot reach 403s inside ``construct_clients``; it is
    skipped rather than failing the run, and AgentV2 then drops it from the
    context so nothing unqueryable is ever advertised to the model.
    """
    from app.services.data_source_service import DataSourceService
    from fastapi import HTTPException

    if folder_ids:
        from app.models.report import ReportAdditionalFolder
        from app.services.folder_service import load_report_folders, require_report_folder, validate_scope_folders
        from sqlalchemy.orm.attributes import set_committed_value
        for fid in await load_report_folders(db, report):
            await require_report_folder(db, organization, user, fid, report.mode)
        await validate_scope_folders(db, organization.id, user.id, folder_ids)
        if report.mode == "training":
            from app.services.report_service import ReportService
            await ReportService()._assert_can_enter_training_mode(db, user, organization, agent_ids or [], folder_ids)
        set_committed_value(report, "agent_folder_id", folder_ids[0])
        set_committed_value(report, "additional_folders", [ReportAdditionalFolder(folder_id=fid) for fid in folder_ids[1:]])
        wanted = set(agent_ids or [])
        pins = [ds for ds in await accessible_agents(db, organization, user) if ds.id in wanted] if wanted else []
        set_committed_value(report, "data_sources", pins)
        agent_ids = None
    svc = data_source_service or DataSourceService()
    agents = await resolve_run_agents(db, organization, user, report, agent_ids=agent_ids,
                                      exclude_personal_connectors=exclude_personal_connectors)
    eval_targets = getattr(report, "eval_target_ids", None)
    if getattr(report, "report_type", None) == "test" and eval_targets is not None:
        from app.core.permissions_decorator import require_org_permission, check_resource_permissions
        from app.models.eval import TestResult, TestCase, TestSuite
        from app.models.report_data_source_association import report_data_source_association
        persisted_pins = set(await db.scalars(
            select(report_data_source_association.c.data_source_id)
            .where(report_data_source_association.c.report_id == report.id)
        ))
        pinned_eval = bool(persisted_pins)
        origin = await db.scalar(select(TestResult.folder_id).where(TestResult.report_id == report.id))
        if getattr(report, "agent_folder_id", None) or origin:
            from app.services.folder_knowledge import require_capability
            from app.models.folder import folder_eval_suites
            fid = report.agent_folder_id or origin
            await require_capability(db, organization, user, fid, "view")
            await require_capability(db, organization, user, fid, "manage_evals")
            suite = await db.scalar(select(TestSuite).join(TestCase).join(TestResult, TestResult.case_id == TestCase.id)
                                    .join(folder_eval_suites, folder_eval_suites.c.suite_id == TestSuite.id)
                                    .where(TestResult.report_id == report.id, folder_eval_suites.c.folder_id == fid,
                                           TestSuite.organization_id == organization.id, TestSuite.deleted_at.is_(None),
                                           TestCase.deleted_at.is_(None), TestCase.status == "active"))
            if not suite or not eval_targets:
                raise HTTPException(409, "Folder eval scope changed")
            if report.agent_folder_id and not pinned_eval and set(eval_targets) != {a.id for a in agents}:
                raise HTTPException(409, "Folder eval scope changed")
            if suite.owner_folder_id:
                await require_capability(db, organization, user, suite.owner_folder_id, "manage_evals")
            elif report.agent_folder_id and not pinned_eval:
                await require_org_permission(db, user.id, organization.id, "manage_evals")
            if pinned_eval or not report.agent_folder_id:
                from types import SimpleNamespace
                branch = await resolve_run_agents(db, organization, user, SimpleNamespace(agent_folder_id=fid, data_sources=[]))
                if not set(eval_targets) <= {a.id for a in branch}:
                    raise HTTPException(409, "An eval target is unavailable in this folder")
        if eval_targets and (pinned_eval or not getattr(report, "agent_folder_id", None)):
            await check_resource_permissions(db, user.id, organization.id, "data_source", eval_targets, "manage_evals")
            allowed = set(eval_targets)
            usable, _ = await svc.filter_user_usable_data_sources(db, agents, user)
            agents = [a for a in usable if a.id in allowed]
            if persisted_pins != allowed or {a.id for a in agents} != allowed:
                raise HTTPException(409, "An eval target is unavailable")
            if origin and not report.agent_folder_id:
                from sqlalchemy.orm.attributes import set_committed_value
                set_committed_value(report, "agent_folder_id", origin)
                set_committed_value(report, "additional_folders", [])
        elif not eval_targets:
            await require_org_permission(db, user.id, organization.id, "manage_evals")
    if report is not None and (agent_ids is not None or getattr(report, "agent_folder_id", None)):
        scope_report_to_run_agents(report, agents)
    elif report is not None and report.data_sources:
        # AgentV2 also reads the report roster directly. Keep its manual pins
        # aligned with this run without pinning personal connectors or writing
        # the filtered selection back to the shared report.
        visible_ids = {agent.id for agent in agents}
        pins = [agent for agent in report.data_sources if agent.id in visible_ids]
        if len(pins) != len(report.data_sources):
            scope_report_to_run_agents(report, pins)
    if getattr(report, "agent_folder_id", None):
        from app.services.folder_service import record_output_scope
        await record_output_scope(db, report.id, [a.id for a in agents])
        from app.services.folder_knowledge import record_knowledge_scope
        await record_knowledge_scope(db, report, organization, user)
    clients: Dict[str, Any] = {}
    # Agents with no connection that this user may not use. Any other agent
    # the user cannot reach is dropped by AgentV2 for having no client; a
    # connectionless agent never has one, so it has to be dropped here.
    denied_connectionless: set = set()
    for ds in agents:
        # Each connection on its own: one that cannot be set up for this user
        # (an unreachable server, a missing sign-in) gets a stand-in client
        # that raises its real error when used, instead of taking the agent's
        # other connections — and every MCP tool on the agent — out of the run.
        # When every connection fails, construct_clients still raises.
        try:
            agent_clients = await construct_agent_clients(
                svc, db, ds, user, where="run agents", run_agents=run_agents_context(agents, report))
            if eval_targets and ds.connections and not agent_clients:
                raise HTTPException(409, "An eval target is unavailable")
            clients.update(agent_clients)
        except HTTPException as e:
            if eval_targets or e.status_code != 403:
                raise
            if not (getattr(ds, "connections", None) or []):
                denied_connectionless.add(str(ds.id))
            logger.warning("run agents: skipping %s — %s", getattr(ds, "name", "?"), e.detail)
        except Exception:
            if eval_targets:
                raise
            logger.exception("run agents: client construction failed for %s", getattr(ds, "name", "?"))
    if denied_connectionless:
        agents = [a for a in agents if str(a.id) not in denied_connectionless]
    return agents, clients


async def resolve_candidate_agents(
    db, organization: Any, user: Any, report: Any, mode: str
) -> Tuple[List[Any], str, set]:
    """Return ``(agents, scope_label, attached_ids)`` for search/focus.

    ``attached_ids`` is the raw manual selection (empty under Auto). In manual
    mode the selection is the whole candidate pool — agents outside it are out
    of scope entirely.
    """
    if getattr(report, "agent_folder_id", None):
        agents = await resolve_run_agents(db, organization, user, report)
        if getattr(report, "report_type", None) == "test" and report.eval_target_ids is not None:
            allowed = set(report.eval_target_ids)
            agents = [agent for agent in agents if agent.id in allowed]
        if mode == "training":
            from app.core.permission_resolver import resolve_permissions
            rights = await resolve_permissions(db, str(user.id), str(organization.id))
            agents = [a for a in agents if rights.has_resource_permission("data_source", str(a.id), MANAGE_PERMISSION)]
        return agents, "folder", {str(ds.id) for ds in agents}
    if mode == "training":
        from app.core.permission_resolver import get_ds_ids_with_permission
        from app.models.data_source import DataSource

        attached_ids = {str(ds.id) for ds in (getattr(report, "data_sources", None) or [])}
        is_admin, ds_ids = await get_ds_ids_with_permission(
            db, str(user.id) if user else "", str(organization.id), MANAGE_PERMISSION
        )
        stmt = (
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .where(DataSource.organization_id == str(organization.id))
        )
        if attached_ids:
            stmt = stmt.where(DataSource.id.in_(attached_ids))
        if not is_admin:
            if not ds_ids:
                return [], "managed", attached_ids
            stmt = stmt.where(DataSource.id.in_([str(x) for x in ds_ids]))
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows), "managed", attached_ids

    attached = list(getattr(report, "data_sources", None) or [])
    attached_ids = {str(ds.id) for ds in attached}
    if not attached:
        # Artifact-chat threads never widen to Auto (see resolve_run_agents);
        # their focus tools are filtered out anyway — defense in depth.
        if getattr(report, "report_type", "regular") == "artifact_chat":
            return [], "attached", set()
        # Auto: the working set is everything accessible; nothing needs attach.
        agents = await accessible_agents(db, organization, user)
        return agents, "accessible", {str(a.id) for a in agents}

    # Manual: the user's selection is a hard scope — no other agents are
    # searched or proposed. The user widens it from the agent selector.
    visible = [a for a in await accessible_agents(db, organization, user) if str(a.id) in attached_ids]
    return visible, "attached", {str(a.id) for a in visible}


async def render_agents_full(db, organization: Any, report: Any, user: Any, data_sources: List[Any],
                             context_hub: Any = None) -> str:
    """Full tables/tools schema + always-on instructions for the given agents —
    the same shape an attached agent's eager context has. Used by search_agents
    results AND set_report_agents success observations, so the planner can act
    on the schema immediately even if the rendered context lags a turn.

    ``context_hub``: the run's hub. Its MCP catalog is applied to the rendered
    sections, so MCP tools are listed only when the run can call them (with
    their deferred flag and policy) — never "in your tool list" for an agent
    whose connection could not be set up for this run."""
    import logging
    logger = logging.getLogger(__name__)
    if not data_sources:
        return ""
    parts: List[str] = []
    ds_ids = [str(ds.id) for ds in data_sources]
    try:
        from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
        builder = SchemaContextBuilder(db, data_sources, organization, report, user=user)
        ctx = await builder.build(with_stats=True, data_source_ids=ds_ids)
        unavailable: List[str] = []
        if context_hub is not None and hasattr(context_hub, "apply_mcp_catalog_to"):
            sections = list(getattr(ctx, "data_sources", None) or [])
            had_tools = {id(s): bool(getattr(s, "mcp_tools", None)) for s in sections}
            context_hub.apply_mcp_catalog_to(sections)
            unavailable = [
                getattr(getattr(s, "info", None), "name", None) or "?"
                for s in sections if had_tools.get(id(s)) and not getattr(s, "mcp_tools", None)
            ]
        schema_xml = ctx.render_combined(top_k_per_ds=10, index_limit=1000)
        if schema_xml:
            parts.append(schema_xml)
        if unavailable:
            parts.append(
                "<note>MCP tools of " + ", ".join(unavailable) + " are not in your tool list at this "
                "step. For an agent that just joined this run they are added from the next step; "
                "otherwise a connection could not be set up for this run, or its tools are off. "
                "Call an MCP tool only once it is listed in <mcp_tools>.</note>"
            )
    except Exception:
        logger.exception("render_agents_full: schema render failed")
    try:
        from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
        ib = InstructionContextBuilder(db, organization, current_user=user, data_source_ids=ds_ids)
        isec = await ib.build(query=None, data_source_ids=ds_ids)
        # On demand nothing smart is inlined, so the planner (which reads this
        # and can call read_instruction) needs the catalog. Off: unchanged.
        inst_xml = isec.render(include_catalog=isec.on_demand)
        if inst_xml and inst_xml.strip():
            parts.append(inst_xml)
    except Exception:
        logger.exception("render_agents_full: instruction render failed")
    return "\n".join(parts)


async def user_can_focus_agent(
    db, organization: Any, user: Any, ds_id: str, mode: str
) -> bool:
    """Whether the actor may focus/attach the given agent id in this mode."""
    if mode == "training":
        from app.core.permission_resolver import resolve_permissions

        resolved = await resolve_permissions(
            db, str(user.id) if user else "", str(organization.id)
        )
        return resolved.has_org_permission(MANAGE_PERMISSION) or resolved.has_resource_permission(
            "data_source", str(ds_id), MANAGE_PERMISSION
        )

    from app.core.permission_resolver import user_can_access_data_source, resolve_permissions
    from app.models.data_source import DataSource
    from app.services.data_source_service import DataSourceService

    agent = await db.scalar(select(DataSource).where(DataSource.id == ds_id,
        DataSource.organization_id == str(organization.id), DataSource.deleted_at.is_(None),
        DataSource.is_active.is_(True), DataSource.publish_status != "disabled"))
    if agent is None:
        return False
    rights = await resolve_permissions(db, str(user.id), str(organization.id)) if user else None
    if not DataSourceService.lifecycle_visible(agent, rights):
        return False
    return await user_can_access_data_source(
        db, str(user.id) if user else "", str(organization.id), None, ds_id=str(ds_id)
    )
