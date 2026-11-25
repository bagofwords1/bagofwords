"""MCP Tool: create_report - Creates a new analysis session (report)."""

import logging
from typing import Dict, Any, List

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.user import User
from app.models.organization import Organization
from app.services.report_service import ReportService
from app.services.data_source_service import DataSourceService
from app.schemas.report_schema import ReportCreate
from app.settings.config import settings

logger = logging.getLogger(__name__)


def _ids(value) -> List[str]:
    """A list of ids from what a client sends: a list, one id, or nothing."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return list(dict.fromkeys(str(v).strip() for v in value if v and str(v).strip()))


async def report_scope(db, organization, user, report) -> Dict[str, Any]:
    """The scope a report was created with, as this user may see it: the
    selected folders (with their visible paths) and the agents picked by hand.
    Names of folders the user can no longer see are withheld."""
    from app.models.report import report_folder_ids
    from app.services.folder_service import browsable_folder_tree, folder_path

    tree, _rights = await browsable_folder_tree(db, organization, user)
    folders = []
    hidden = 0
    for fid in report_folder_ids(report):
        if fid in tree:
            folders.append({"id": fid, "name": tree[fid][1], "path": folder_path(tree, fid)})
        else:
            hidden += 1
    from sqlalchemy import select
    from app.models.report_data_source_association import report_data_source_association as rds
    pinned_ids = list(await db.scalars(select(rds.c.data_source_id).where(rds.c.report_id == str(report.id))))
    if folders or hidden:
        mode = "folders" if not pinned_ids else "folders_and_agents"
    else:
        mode = "agents"
    return {"mode": mode, "folders": folders, "hidden_folder_count": hidden, "pinned_agent_ids": pinned_ids}


# At most this many folders are reported as missing from a report's scope.
MISSING_FOLDER_RULES_LIMIT = 20


async def missing_folder_rules(db, organization, user, report, agents) -> List[Dict[str, Any]]:
    """Folders whose live rules this report does NOT apply, though it uses
    agents filed in them.

    Folder rules apply only in reports scoped to that folder. A report pinned
    to an agent (or a title-only report, which pins every agent) runs without
    the rules of the agent's folder, and nothing else tells the client. Only
    folders the caller can query (the folder ``view`` right) are named, so a
    direct grant on one agent reveals nothing about its folder.
    """
    from sqlalchemy import select
    from app.models.folder import folder_instructions
    from app.models.report import report_folder_ids
    from app.services.folder_knowledge import readable_folder_rules
    from app.services.folder_service import browsable_folder_tree, folder_path

    selected = set(report_folder_ids(report))
    homes: Dict[str, List[str]] = {}
    for a in agents or []:
        home = getattr(a, "folder_id", None)
        if home and home not in selected:
            homes.setdefault(home, []).append(str(a.id))
    if not homes:
        return []
    # One query for the folders that have any rule attached: a title-only
    # report spans every agent, and most folders have none to load.
    with_rules = set(await db.scalars(
        select(folder_instructions.c.folder_id).where(folder_instructions.c.folder_id.in_(homes)).distinct()
    ))
    if not with_rules:
        return []
    tree, rights = await browsable_folder_tree(db, organization, user)
    out = []
    for fid in sorted(with_rules, key=lambda f: folder_path(tree, f).lower() if f in tree else ""):
        if fid not in tree or not rights.has_resource_permission("folder", fid, "view"):
            continue
        rules = await readable_folder_rules(db, organization, user, fid)
        if rules:
            out.append({"folder_id": fid, "path": folder_path(tree, fid), "rule_count": len(rules),
                        "agent_ids": homes[fid]})
        if len(out) >= MISSING_FOLDER_RULES_LIMIT:
            break
    return out


def missing_folder_rules_hint(missing: List[Dict[str, Any]]) -> str:
    paths = ", ".join(f"{m['path']} ({m['rule_count']})" for m in missing)
    return (
        f"Rules attached to these folders do NOT apply in this report: {paths}. Folder rules apply only "
        "in reports scoped to the folder. If the user's question is about these agents, create the report "
        "with agent_folder_ids set to these folder ids (keep data_source_ids for any other agents) so the "
        "folder's rules are followed."
    )


class CreateReportTool(MCPTool):
    """Create a new analysis session (report).

    Returns a report_id that can be used in subsequent tool calls
    to maintain conversation context.
    """

    name = "create_report"
    description = (
        "Create a new analysis session (report). "
        "Call this once at the start of a conversation to get a report_id. "
        "Use that report_id in all subsequent create_data, inspect_data, and get_context calls. "
        "With only a title, the report uses every agent you can access. To scope it, pass "
        "agent_folder_ids (a folder covers every agent in its branch) and/or data_source_ids "
        "(single agents) — find the ids with browse_agents. Rules attached to a folder apply "
        "only in reports scoped to that folder: when the user's agents live in a folder, scope by "
        "the folder. The response lists missing_folder_rules when the report uses agents whose "
        "folder rules it does not apply."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "A short, descriptive title summarizing what the user wants to analyze. "
                        "Examples: 'Customer Revenue Analysis', 'Q4 Sales Trends', 'Top Products by Region'. "
                        "Derive from the user's request - avoid generic titles like 'Data Analysis'."
                    )
                },
                "agent_folder_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Folder ids (from browse_agents) to scope the report to. The report uses every "
                        "agent in these folders' branches, re-checked against your access on each call."
                    ),
                },
                "data_source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent ids (from browse_agents) to add to the report's scope.",
                },
            },
            "required": ["title"],
        }

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Create a new report: scoped to the folders/agents asked for, or with
        all the user's active data sources attached (title-only)."""

        report_service = ReportService()
        ds_service = DataSourceService()
        folder_ids = _ids(args.get("agent_folder_ids"))
        picked_ids = _ids(args.get("data_source_ids"))

        # Get active data sources the calling user is allowed to see. Passing
        # current_user is essential: without it, get_active_data_sources skips
        # all visibility filtering and attaches every org data source —
        # including private ones the user isn't a member of.
        data_sources = await ds_service.get_active_data_sources(
            db, organization, current_user=user, channel="mcp"
        )

        if folder_ids or picked_ids:
            usable = {str(ds.id): ds for ds in data_sources}
            unknown = [i for i in picked_ids if i not in usable]
            if unknown:
                # An agent the user can use once they sign in is not "not
                # found": say what is missing, by name. Anything else stays
                # undisclosed.
                connectable = {
                    str(ds.id): ds.name for ds in await ds_service.get_active_data_sources(
                        db, organization, current_user=user, include_unconnected=True, channel="mcp"
                    )
                }
                needs_signin = [connectable[i] for i in unknown if i in connectable]
                if needs_signin:
                    raise ValueError(
                        f"Sign-in required for: {', '.join(needs_signin)}. The user must connect their own "
                        "account to it in Bag of Words (agent selector, Connect), then retry."
                    )
                raise ValueError(
                    f"Agent(s) not found or not available to you: {', '.join(unknown)}. "
                    "Use browse_agents to find the agents you can use."
                )
            from app.services.folder_service import browsable_folder_tree
            tree, rights = await browsable_folder_tree(db, organization, user)
            unusable = [f for f in folder_ids if f not in tree or not rights.has_resource_permission("folder", f, "view")]
            if unusable:
                raise ValueError(
                    f"Folder(s) not found or not available to you: {', '.join(unusable)}. "
                    "Use browse_agents to find the folders you can use."
                )
            attached = [usable[i] for i in picked_ids]
        else:
            attached = list(data_sources)

        # Get or create MCP platform
        platform = await self._get_or_create_mcp_platform(db, organization)

        # Ensure user mapping exists for Members page visibility
        await self._ensure_mcp_user_mapping(db, user, organization, platform)

        # Create the report with MCP platform
        try:
            report = await report_service.create_report(
                db=db,
                report_data=ReportCreate(
                    title=args.get("title") or "MCP Session",
                    data_sources=[ds.id for ds in attached],
                    agent_folder_ids=folder_ids or None,
                    external_platform_id=str(platform.id),
                ),
                current_user=user,
                organization=organization,
            )
        except HTTPException as e:
            raise ValueError(f"Could not create the report: {e.detail}") from e

        base_url = settings.bow_config.base_url
        result = {
            "report_id": str(report.id),
            "title": report.title,
            "url": f"{base_url}/reports/{report.id}",
        }
        if not folder_ids:
            result["data_sources"] = [
                {"id": str(ds.id), "name": ds.name, "type": ds.type} for ds in attached
            ]
            if picked_ids:
                result["scope"] = {"mode": "agents", "folders": [], "hidden_folder_count": 0,
                                   "pinned_agent_ids": [str(ds.id) for ds in attached]}
            await self._add_missing(result, db, organization, user, report, attached)
            return result

        # Folder scope: report what the run will actually use — the folders'
        # branches as this user sees them today, plus the hand-picked agents.
        loaded = await self._load_report(db, str(report.id), user, organization)
        from app.services.folder_service import visible_agent_paths
        effective = list(loaded.data_sources or [])
        paths = await visible_agent_paths(db, organization, user, effective)
        result["scope"] = await report_scope(db, organization, user, loaded)
        result["data_sources"] = [
            {"id": str(ds.id), "name": ds.name, "type": getattr(ds, "type", None),
             "folder_path": paths.get(str(ds.id))}
            for ds in effective
        ]
        await self._add_missing(result, db, organization, user, loaded, effective)
        await db.commit()
        return result

    @staticmethod
    async def _add_missing(result: Dict[str, Any], db, organization, user, report, agents) -> None:
        """Add the missing-folder-rules note. It is advice: the report already
        exists, so a failure here must not fail the call (the client would
        retry and create a second report). Runs in a savepoint so a failed
        read leaves the caller's transaction usable."""
        try:
            async with db.begin_nested():
                missing = await missing_folder_rules(db, organization, user, report, agents)
        except Exception:
            logger.exception("create_report: listing missing folder rules failed")
            return
        if missing:
            result["missing_folder_rules"] = missing
            result["missing_folder_rules_hint"] = missing_folder_rules_hint(missing)
