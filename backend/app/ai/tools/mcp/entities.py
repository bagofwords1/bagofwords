"""MCP Tools: saved queries (catalog entities) — list and describe.

An entity is a published saved query: a metric or model definition someone in
the org already agreed on, with its code, declared parameters and a
materialized snapshot. The in-app planner is shown them in ``<entities>`` and
reuses them through ``describe_entity``; without these tools an external client
(Claude, Cursor) cannot see that they exist and writes its own definition from
scratch.

Same gates as every other surface that serves entities:

- published entities only, scoped to the agents in play (the report's, or every
  agent the caller can see), with ``bow_source_access`` visibility applied —
  shared with the planner via ``EntityContextBuilder.load_entities``;
- the caller must be able to access every agent the entity reads
  (``user_can_access_data_source``), as ``describe_entity`` enforces in-app;
- stored rows go through ``viewer_data_policy.resolve_entity_data``, so on a
  user-scoped source the owner's row slice is never served to another reader —
  the saved code is re-executed under the caller's own credentials instead.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.context.builders.entity_context_builder import EntityContextBuilder
from app.ai.tools.mcp.base import MCPTool
from app.models.entity import Entity
from app.models.organization import Organization
from app.models.user import User
from app.schemas.mcp import (
    MCP_ENTITY_DEFAULT_PREVIEW_ROWS,
    MCPDescribeEntityInput,
    MCPDescribeEntityOutput,
    MCPEntityItem,
    MCPEntityParameter,
    MCPListEntitiesInput,
    MCPListEntitiesOutput,
)

logger = logging.getLogger(__name__)

NOT_FOUND = "No published saved query matches that name or id, or you don't have access to it."


def _parameters(entity: Entity) -> List[MCPEntityParameter]:
    """Declared ParamSpec dicts as the MCP contract exposes them."""
    out: List[MCPEntityParameter] = []
    for p in (getattr(entity, "parameters", None) or []):
        if not isinstance(p, dict) or not p.get("name"):
            continue
        opts = p.get("options")
        out.append(MCPEntityParameter(
            name=str(p["name"]),
            type=str(p.get("type") or "string"),
            source=p.get("source") or None,
            default=p.get("default"),
            required=bool(p.get("required")),
            options=[
                o.get("value") if isinstance(o, dict) else o
                for o in (opts if isinstance(opts, list) else [])
            ],
        ))
    return out


def entity_agent_names(entity: Entity) -> List[str]:
    names = []
    for ds in (getattr(entity, "data_sources", None) or []):
        name = getattr(ds, "name", None)
        if name:
            names.append(str(name))
    return names


async def _scope_data_source_ids(
    tool: MCPTool,
    db: AsyncSession,
    user: User,
    organization: Organization,
    report_id: Optional[str],
) -> List[str]:
    """Agent ids a listing is scoped to.

    With a report, its attached agents filtered to the ones this user may see
    and query — the same two filters ``get_context`` applies, so a listing can
    never advertise a saved query on a source the caller's own context hides.
    Without one, every agent the caller can see.
    """
    from app.services.data_source_service import DataSourceService

    ds_service = DataSourceService()
    if report_id:
        report = await tool._load_report(db, report_id, user, organization)
        visible = await ds_service.filter_user_visible_data_sources(
            db, report.data_sources, user, organization
        )
        usable, _skipped = await ds_service.filter_user_usable_data_sources(db, visible, user)
        return [str(ds.id) for ds in usable]
    listed = await ds_service.get_data_sources(db, user, organization)
    return [str(ds.id) for ds in listed]


class ListEntitiesMCPTool(MCPTool):
    """List the published saved queries available on the agents in play."""

    name = "list_entities"
    read_only_hint = True
    description = (
        "List the organization's published saved queries (metrics and models): the agreed "
        "definitions behind terms like revenue, active user or churn. Returns titles, "
        "descriptions and the parameters each accepts. Check this before writing a query for "
        "a business term — when one of these already answers the ask, call describe_entity "
        "and reuse its code instead of inventing a definition."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPListEntitiesInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        data = MCPListEntitiesInput(**args)
        ds_ids = await _scope_data_source_ids(self, db, user, organization, data.report_id)
        if not ds_ids:
            return MCPListEntitiesOutput(entities=[], total=0).model_dump()

        builder = EntityContextBuilder(db, organization, report=None, user=user)
        keywords = (
            EntityContextBuilder._extract_keywords_from_context(data.search, None)
            if data.search else []
        )
        # The planner's own loader: published + bow_source_access-visible +
        # scoped to these agents, with the same keyword ranking.
        entities = await builder.load_entities(
            keywords=keywords,
            top_k=data.limit,
            require_source_assoc=True,
            data_source_ids=ds_ids,
        )
        if keywords:
            # The planner's loader falls back to the most recent entities when
            # nothing matches, which is right for a turn's context and wrong
            # for an explicit search: a caller that asked for "churn" should
            # get an empty answer, not an unrelated saved query.
            entities = [
                e for e in entities
                if any(
                    kw in f"{e.title} {e.description or ''} {getattr(e, 'slug', '') or ''}".lower()
                    for kw in keywords
                )
            ]
        items = [
            MCPEntityItem(
                id=str(e.id),
                slug=getattr(e, "slug", None),
                type=e.type,
                title=e.title,
                description=e.description or None,
                agents=entity_agent_names(e),
                parameters=_parameters(e),
            )
            for e in entities
        ]
        logger.info("list_entities: returned %d saved queries for user %s", len(items), user.id)
        return MCPListEntitiesOutput(entities=items, total=len(items)).model_dump()


class DescribeEntityMCPTool(MCPTool):
    """Read one saved query in full: its code, parameters and rows."""

    name = "describe_entity"
    read_only_hint = True
    description = (
        "Read one saved query (from list_entities) in full: its code — the organization's "
        "agreed definition — its parameters, and its rows. Pass `params` to run the saved "
        "code with specific values, or run: true to refresh it. Reuse the code it returns "
        "rather than writing your own version of the same metric; pass it to create_data "
        "when the result has to be transformed or combined with other data."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPDescribeEntityInput.model_json_schema()

    async def _find(self, db: AsyncSession, organization: Organization, name_or_id: str) -> Optional[Entity]:
        """Resolve by id, slug, exact title, then a partial match on any of
        those — the same ladder the in-app describe_entity walks."""
        base = (
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(
                Entity.organization_id == str(organization.id),
                Entity.status == "published",
                Entity.deleted_at.is_(None),
            )
        )
        like = f"%{name_or_id}%"
        for clause in (
            Entity.id == name_or_id,
            Entity.slug.ilike(name_or_id),
            Entity.title.ilike(name_or_id),
            or_(Entity.title.ilike(like), Entity.slug.ilike(like), Entity.description.ilike(like)),
        ):
            found = (await db.execute(base.where(clause).limit(1))).scalars().first()
            if found:
                return found
        return None

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        data = MCPDescribeEntityInput(**args)

        def fail(msg: str) -> Dict[str, Any]:
            return MCPDescribeEntityOutput(success=False, error_message=msg).model_dump()

        entity = await self._find(db, organization, data.name_or_id)
        if entity is None:
            return fail(NOT_FOUND)

        # Protected sources: one answer for "not there" and "not yours".
        from app.services.bow_source_access import can_read
        if entity.bow_source_access and not await can_read(db, entity.bow_source_access, user):
            return fail(NOT_FOUND)

        # Every agent the saved query reads must be one this caller can access.
        from app.core.permission_resolver import user_can_access_data_source
        for ds in (entity.data_sources or []):
            if not await user_can_access_data_source(db, str(user.id), str(organization.id), ds):
                return fail(NOT_FOUND)

        from app.services.viewer_data_policy import entity_data_withheld

        withheld = await entity_data_withheld(db, entity, user)
        request_values = dict(data.params or {})
        rows_source = "withheld" if withheld else "snapshot"
        grid: Dict[str, Any] = {} if withheld else (entity.data or {})
        applied_params = dict(getattr(entity, "applied_params", None) or {}) or None
        error_message = None

        if request_values or data.run or withheld:
            # The no-codegen path: the saved code runs with defaults <- the
            # requested values <- this caller's identity bindings, cached per
            # (entity, user, values).
            if not (entity.code or "").strip():
                error_message = "This saved query has no code to run."
            else:
                try:
                    from app.services.entity_service import EntityService
                    run = await EntityService().run_entity_for_user(
                        db, entity, organization, user, request_values, force_refresh=bool(data.run),
                    )
                    grid = run["data"] or {}
                    applied_params = dict(run["applied_params"] or {}) or None
                    rows_source = "cache" if run.get("cached") else "executed"
                except Exception as e:
                    logger.exception("describe_entity run failed for %s: %s", entity.id, e)
                    error_message = f"Running the saved query failed: {str(e)[:300]}"
                    # Never fall back to a snapshot that is withheld from this
                    # reader, or that answers different parameter values.
                    grid = {} if (withheld or request_values) else (entity.data or {})
                    rows_source = "withheld" if withheld else "snapshot"

        rows = (grid or {}).get("rows") or []
        info = (grid or {}).get("info") or {}
        total_rows = info.get("total_rows")
        if not isinstance(total_rows, int):
            total_rows = len(rows)
        limit = data.limit or MCP_ENTITY_DEFAULT_PREVIEW_ROWS
        returned = rows[:limit]
        columns = [
            c.get("field") or c.get("headerName")
            for c in ((grid or {}).get("columns") or [])
            if isinstance(c, dict) and (c.get("field") or c.get("headerName"))
        ]

        return MCPDescribeEntityOutput(
            success=True,
            entity_id=str(entity.id),
            slug=getattr(entity, "slug", None),
            type=entity.type,
            title=entity.title,
            description=entity.description or None,
            agents=entity_agent_names(entity),
            code=entity.code or None,
            parameters=_parameters(entity),
            applied_params=applied_params,
            columns=[str(c) for c in columns],
            rows=[r for r in returned if isinstance(r, dict)],
            total_rows=total_rows,
            truncated=len(returned) < total_rows,
            data_source=rows_source,
            error_message=error_message,
        ).model_dump()
