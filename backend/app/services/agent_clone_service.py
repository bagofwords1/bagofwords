"""Clone an agent (DataSource) into a new one, in ONE transaction.

Every existing write path an agent is made of (create_data_source, instruction
create/version/build, directories, evals, saved queries, lists) commits part
way, and several roll back and swallow errors. A clone that failed half way
would leave a half-built agent, so this service writes the rows itself in the
caller's session and commits once at the end. Only read-only helpers are
reused (license checks, name check, version hash, auto-publish check).

What is always carried over: the agent's settings, its connections (the same
ones, linked), its tables (with their selection and hidden columns), its tool
policies and its prompts. Instructions, saved queries, lists, evals, files and
access follow the caller's choice (``AgentCloneIn``). Conversations,
dashboards, Git sync, per-user credentials and history never move.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import JSON, func, insert, null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import TypeDecorator

from app.models.data_source import DataSource
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.data_source_file_association import data_source_file_association
from app.models.data_source_membership import PRINCIPAL_TYPE_GROUP, PRINCIPAL_TYPE_USER, DataSourceMembership
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.prompt import Prompt
from app.models.prompt_data_source_association import prompt_data_source_association
from app.models.resource_grant import ResourceGrant
from app.schemas.agent_clone_schema import AgentCloneIn, CloneComponent, CloneSkipped

logger = logging.getLogger(__name__)

_ROW_META = {"id", "created_at", "updated_at", "deleted_at"}


def _is_json(col) -> bool:
    t = col.type
    return isinstance(t, JSON) or (isinstance(t, TypeDecorator) and isinstance(getattr(t, "impl_instance", None), JSON))


def sql_nulls(table, values: Dict[str, Any]) -> Dict[str, Any]:
    """``None`` into a JSON column is stored as the JSON text 'null', not SQL
    NULL, unless the column says otherwise; rows written by hand would then
    differ from rows nobody set (``IS NULL`` stops matching). Write SQL NULL."""
    json_cols = {c.key for c in table.columns if _is_json(c)}
    return {k: (null() if v is None and k in json_cols else v) for k, v in values.items()}


def copy_columns(row, *, skip=(), **overrides) -> Dict[str, Any]:
    """Column values of ``row`` for a new row of the same model: every column
    except its identity/timestamps and ``skip``, with ``overrides`` applied.
    Empty JSON columns stay SQL NULL, as on the source."""
    out = {}
    for col in row.__table__.columns:
        if col.key in _ROW_META or col.key in skip:
            continue
        out[col.key] = getattr(row, col.key)
    out.update(overrides)
    return sql_nulls(row.__table__, out)


@dataclass
class CloneContext:
    db: Any
    organization: Any
    user: Any
    source: DataSource
    opts: AgentCloneIn
    clone: Optional[DataSource] = None
    # old id -> new id, per kind, for the components that copy rows.
    table_map: Dict[str, str] = field(default_factory=dict)
    instruction_map: Dict[str, str] = field(default_factory=dict)
    list_map: Dict[str, str] = field(default_factory=dict)
    entity_map: Dict[str, str] = field(default_factory=dict)
    summary: Dict[str, CloneComponent] = field(default_factory=dict)
    # Set when instructions went into a build promoted to main.
    promoted_build_id: Optional[str] = None
    # (old, new) bow table names of copied lists, for saved-query code.
    list_names: List[tuple] = field(default_factory=list)
    # Names an @mention can resolve to (load_mention_matcher).
    mention_matcher: Any = None
    # The source's name is also one of its tables' (see _means_a_table).
    source_name_is_a_table: bool = False

    def part(self, name: str) -> CloneComponent:
        return self.summary.setdefault(name, CloneComponent())

    def skip(self, part: str, id: str, name: Optional[str], reason: str) -> None:
        self.part(part).skipped.append(CloneSkipped(id=str(id), name=name, reason=reason))

    @property
    def source_id(self) -> str:
        return str(self.source.id)

    @property
    def clone_id(self) -> str:
        return str(self.clone.id)


# ── mentions ───────────────────────────────────────────────────────────────

# Prompts store mentions grouped by display name with untyped items
# (MentionInput.buildMentionGroups); eval cases keep typed MentionItems.
# Group names are matched upper-cased, as mention_service reads them.
_GROUP_KINDS = {"DATA SOURCES": "data_source", "TABLES": "datasource_table", "ENTITIES": "entity",
                "CONNECTION TOOLS": "connection_tool"}


def _group_kind(value: dict) -> Optional[str]:
    if not isinstance(value.get("items"), list):
        return None
    return _GROUP_KINDS.get(str(value.get("name") or "").upper())


def _mention_items(value: Any, kind: Optional[str] = None):
    """(kind, item) for every mention item, in either stored shape."""
    if isinstance(value, list):
        for v in value:
            yield from _mention_items(v, kind)
    elif isinstance(value, dict):
        group_kind = _group_kind(value)
        if group_kind:
            yield from _mention_items(value["items"], group_kind)
        else:
            yield value.get("type") or kind, value


def remap_mentions(value: Any, ctx: CloneContext, kind: Optional[str] = None) -> Any:
    """Point mention objects (prompt and eval mentions) at the clone: the
    source agent becomes the clone, its tables and copied saved queries their
    copies. Anything else is returned unchanged. An item's kind is its
    ``type``, else the group it sits in."""
    if isinstance(value, list):
        return [remap_mentions(v, ctx, kind) for v in value]
    if not isinstance(value, dict):
        return value
    group_kind = _group_kind(value)
    if group_kind:
        return {**value, "items": remap_mentions(value["items"], ctx, group_kind)}
    out = {k: remap_mentions(v, ctx) for k, v in value.items()}
    kind = out.get("type") or kind
    src, dst = ctx.source_id, ctx.clone_id
    if kind == "data_source" and str(out.get("id")) == src:
        out["id"] = dst
        out["name"] = ctx.clone.name
    elif kind == "datasource_table" and str(out.get("id")) in ctx.table_map:
        # A table the clone doesn't have (a user-discovered one) stays on the
        # source: pointing it at the clone would name a table that isn't there.
        out["id"] = ctx.table_map[str(out["id"])]
        # Typed items say data_source_id, grouped ones datasource_id.
        key = "data_source_id" if "data_source_id" in out else "datasource_id"
        if str(out.get(key)) == src:
            out[key] = dst
            out["data_source_name"] = ctx.clone.name
    elif kind == "entity":
        if str(out.get("id")) in ctx.entity_map:
            out["id"] = ctx.entity_map[str(out["id"])]
        if isinstance(out.get("data_source_ids"), list):
            out["data_source_ids"] = [dst if str(i) == src else i for i in out["data_source_ids"]]
    elif kind == "connection_tool" and str(out.get("data_source_id")) == src:
        out["data_source_id"] = dst
    return out


async def load_mention_matcher(ctx: CloneContext) -> None:
    """The names an ``@`` in the source's text can resolve to: the org's agents
    and the source's tables and saved queries. Parsing against them (longest
    match wins, as when the text is used) is what tells ``@Sales`` from
    ``@Sales Orders``. The clone and deleted agents are left out: the text was
    written without them, and as longer names they would shadow the source."""
    from app.models.entity import Entity, entity_data_source_association as entity_links
    from app.utils.mentions import MentionMatcher

    agents = (await ctx.db.execute(
        select(DataSource.name).where(
            DataSource.organization_id == str(ctx.organization.id),
            DataSource.id != ctx.clone_id, DataSource.deleted_at.is_(None),
        )
    )).scalars().all()
    tables = (await ctx.db.execute(
        select(DataSourceTable.name).where(
            DataSourceTable.datasource_id == ctx.source_id, DataSourceTable.deleted_at.is_(None)
        )
    )).scalars().all()
    entities = (await ctx.db.execute(
        select(Entity.title)
        .join(entity_links, entity_links.c.entity_id == Entity.id)
        .where(entity_links.c.data_source_id == ctx.source_id, Entity.deleted_at.is_(None))
    )).scalars().all()
    ctx.mention_matcher = MentionMatcher([ctx.source.name, *agents, *tables, *entities])
    source = ctx.source.name.casefold()
    ctx.source_name_is_a_table = any((t or "").casefold() == source for t in tables)


def _means_a_table(mentions: Any, ctx: CloneContext) -> bool:
    """When the source shares its name with one of its tables, the text alone
    can't say which an ``@`` means; the stored mentions can."""
    if not ctx.source_name_is_a_table:
        return False
    source = ctx.source.name.casefold()
    items = list(_mention_items(mentions))
    names_agent = any(k == "data_source" and str(i.get("id")) == ctx.source_id for k, i in items)
    names_table = any(k == "datasource_table" and str(i.get("name") or "").casefold() == source for k, i in items)
    return names_table and not names_agent


def remap_mention_text(text: Optional[str], ctx: CloneContext, mentions: Any = None) -> Optional[str]:
    """Mentions of the source agent in text a user wrote become mentions of
    the clone. The text is split by the same parser that resolves it
    (utils/mentions.py), so only an ``@`` that resolves to the source changes:
    ``@"Sales"`` and ``@Sales`` do, ``@Sales Orders`` (another name) doesn't.
    ``mentions`` (the item's stored mentions, before remapping) settle an
    ``@`` that names both the source and one of its tables."""
    from app.utils.mentions import format_mention, parse_mention_segments

    if not text or (mentions and _means_a_table(mentions, ctx)):
        return text
    old = ctx.source.name.replace('"', "").casefold()
    return "".join(
        format_mention(ctx.clone.name)
        if seg.label is not None and seg.label.replace('"', "").casefold() == old
        else seg.text
        for seg in parse_mention_segments(text, ctx.mention_matcher)
    )


# ── preconditions ──────────────────────────────────────────────────────────

async def _load_source(db, organization, data_source_id: str) -> DataSource:
    from sqlalchemy.orm import selectinload

    ds = (await db.execute(
        select(DataSource)
        .options(selectinload(DataSource.connections))
        .where(DataSource.id == str(data_source_id), DataSource.organization_id == str(organization.id))
    )).scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return ds


async def _check_license(db, organization, source: DataSource) -> None:
    from app.services.data_source_service import DataSourceService

    svc = DataSourceService()
    await svc.enforce_agent_cap(db, organization)
    svc.enforce_connector_license([conn.type for conn in source.connections or []])


def _name_taken(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"A data source named '{name}' already exists in this organization. Please choose a different name.",
    )


async def _check_name(db, organization, name: str) -> None:
    taken = (await db.execute(
        select(DataSource.id).where(DataSource.organization_id == str(organization.id), DataSource.name == name)
    )).first()
    if taken:
        raise _name_taken(name)


# ── always carried over ────────────────────────────────────────────────────

async def _create_agent(ctx: CloneContext) -> None:
    s, o = ctx.source, ctx.opts
    clone = DataSource(**sql_nulls(DataSource.__table__, dict(
        name=o.name,
        organization_id=str(ctx.organization.id),
        owner_user_id=str(ctx.user.id),
        description=o.description if o.description is not None else s.description,
        context=s.context,
        summary=s.summary,
        icon=s.icon,
        conversation_starters=s.conversation_starters,
        channel_availability=s.channel_availability,
        automation_settings=s.automation_settings,
        is_active=s.is_active,
        # In Training, and never more visible than the source: a live source
        # gives a live clone (with access=default, for the cloner alone); a
        # draft or disabled one stays so, and a source pulled into development
        # (shown to its admins only) gives a clone in development.
        publish_status=s.publish_status,
        reliability_status="development" if s.reliability_status == "development" else "training",
        # Its description and starters came with it; an LLM sync would
        # overwrite what the user just named it.
        use_llm_sync=False,
        is_public=bool(s.is_public) if o.access == "copy" else False,
    )))
    ctx.db.add(clone)
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError:
        raise _name_taken(o.name)
    ctx.clone = clone
    if s.connections:
        await ctx.db.execute(insert(domain_connection),
                             [{"data_source_id": ctx.clone_id, "connection_id": str(c.id)} for c in s.connections])


async def _copy_tables(ctx: CloneContext) -> None:
    rows = (await ctx.db.execute(
        select(DataSourceTable).where(
            DataSourceTable.datasource_id == ctx.source_id, DataSourceTable.deleted_at.is_(None)
        )
    )).scalars().all()
    for t in rows:
        # A table a user found through their own connection is theirs: it is
        # rebuilt per user on the clone, like on any agent.
        if t.connection_table_id is None and (t.metadata_json or {}).get("discovered_by") == "user":
            continue
        new_id = str(uuid.uuid4())
        ctx.db.add(DataSourceTable(id=new_id, **copy_columns(t, datasource_id=ctx.clone_id)))
        ctx.table_map[str(t.id)] = new_id
    ctx.part("tables").copied = len(ctx.table_map)


async def _copy_tool_policies(ctx: CloneContext) -> None:
    # Without these the clone falls back to the connection defaults, so a tool
    # denied on the source would be allowed on the clone.
    rows = (await ctx.db.execute(
        select(DataSourceConnectionTool).where(DataSourceConnectionTool.data_source_id == ctx.source_id)
    )).scalars().all()
    for r in rows:
        ctx.db.add(DataSourceConnectionTool(**copy_columns(r, data_source_id=ctx.clone_id)))
    ctx.part("tool_policies").copied = len(rows)


async def _copy_prompts(ctx: CloneContext) -> None:
    """Agent prompts (conversation starters included) are copied, never
    linked: a prompt is shown only to members of ALL its agents, so linking
    would hide it from people who can use the source alone. A private or
    global prompt that names the source isn't the agent's: copying it would
    show its owner, or the whole org, the same prompt twice."""
    rows = (await ctx.db.execute(
        select(Prompt)
        .join(prompt_data_source_association, prompt_data_source_association.c.prompt_id == Prompt.id)
        .where(prompt_data_source_association.c.data_source_id == ctx.source_id,
               Prompt.scope == "agent", Prompt.deleted_at.is_(None))
    )).scalars().unique().all()
    if not rows:
        return
    links = []
    for p in rows:
        # On the clone only: also keeping the original's other agents would
        # show those agents the same prompt twice.
        new_id = str(uuid.uuid4())
        ctx.db.add(Prompt(id=new_id, **copy_columns(p, mentions=remap_mentions(p.mentions, ctx),
                                                    text=remap_mention_text(p.text, ctx, p.mentions))))
        links.append({"prompt_id": new_id, "data_source_id": ctx.clone_id})
    await ctx.db.flush()
    await ctx.db.execute(insert(prompt_data_source_association), links)
    ctx.part("prompts").copied = len(rows)


async def _link_files(ctx: CloneContext) -> None:
    part = ctx.part("files")
    if ctx.opts.files != "link":
        return
    file_ids = sorted({str(f) for f in (await ctx.db.execute(
        select(data_source_file_association.c.file_id)
        .where(data_source_file_association.c.data_source_id == ctx.source_id)
    )).scalars().all()})
    if file_ids:
        await ctx.db.execute(insert(data_source_file_association),
                             [{"data_source_id": ctx.clone_id, "file_id": fid} for fid in file_ids])
    part.linked = len(file_ids)


async def _set_access(ctx: CloneContext) -> None:
    """The cloner always manages the clone. ``copy`` also brings the source's
    user and group members with their permissions; grants a role holds belong
    to that role's definition and are not copied."""
    part = ctx.part("access")
    grants: Dict[tuple, List[str]] = {}
    members: set = set()
    if ctx.opts.access == "copy":
        for g in (await ctx.db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == "data_source",
                ResourceGrant.resource_id == ctx.source_id,
                ResourceGrant.deleted_at.is_(None),
                ResourceGrant.principal_type.in_([PRINCIPAL_TYPE_USER, PRINCIPAL_TYPE_GROUP]),
            )
        )).scalars().all():
            grants[(g.principal_type, str(g.principal_id))] = list(g.permissions or [])
        for m in (await ctx.db.execute(
            select(DataSourceMembership).where(DataSourceMembership.data_source_id == ctx.source_id)
        )).scalars().all():
            members.add((m.principal_type, str(m.principal_id)))
    me = (PRINCIPAL_TYPE_USER, str(ctx.user.id))
    grants[me] = sorted(set(grants.get(me, [])) | {"manage"})
    members.add(me)
    for (ptype, pid) in sorted(members | set(grants)):
        if (ptype, pid) in members:
            ctx.db.add(DataSourceMembership(data_source_id=ctx.clone_id, principal_type=ptype, principal_id=pid))
        if (ptype, pid) in grants:
            ctx.db.add(ResourceGrant(
                organization_id=str(ctx.organization.id), resource_type="data_source",
                resource_id=ctx.clone_id, principal_type=ptype, principal_id=pid,
                permissions=grants[(ptype, pid)],
            ))
    part.copied = len(grants) - 1 if ctx.opts.access == "copy" else 0


# ── entry point ────────────────────────────────────────────────────────────

async def clone_agent(db, organization, user, source_id: str, opts: AgentCloneIn, *, request=None):
    """Build the clone and commit once. Returns ``(clone_id, summary)``."""
    from app.core.permission_resolver import invalidate_rbac_memo
    from app.ee.audit.service import audit_service

    from app.services.agent_clone_data import clone_evals, clone_lists, clone_saved_queries
    from app.services.agent_clone_instructions import (
        check_link_authority, check_no_pending_changes, clone_instructions, live_instructions,
    )

    source = await _load_source(db, organization, source_id)
    await _check_license(db, organization, source)
    await _check_name(db, organization, opts.name)
    ctx = CloneContext(db=db, organization=organization, user=user, source=source, opts=opts)
    if opts.instructions == "link":
        # Fail before writing anything; re-checked under the lock later.
        live = await live_instructions(db, str(organization.id), str(source.id))
        await check_link_authority(db, organization, user, str(source.id), live)
        await check_no_pending_changes(db, organization, live)

    try:
        await _create_agent(ctx)
        await _copy_tables(ctx)
        await load_mention_matcher(ctx)
        await _copy_tool_policies(ctx)
        await _set_access(ctx)
        # Later steps check the cloner's authority on the clone.
        invalidate_rbac_memo(db, str(user.id), str(organization.id))
        # Lists before saved queries (their code may read a copied list) and
        # saved queries before evals and prompts (their mentions may name one).
        await clone_lists(ctx)
        await clone_saved_queries(ctx)
        await clone_evals(ctx)
        await _copy_prompts(ctx)
        await _link_files(ctx)
        # Last: it locks the org's main build until the commit.
        await clone_instructions(ctx)
        await audit_service.log(
            db=db, organization_id=str(organization.id), action="data_source.cloned",
            user_id=str(user.id), resource_type="data_source", resource_id=ctx.clone_id,
            details={"source_data_source_id": ctx.source_id, "name": opts.name,
                     "choices": opts.model_dump(exclude={"name", "description"})},
            request=request, commit=False,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    if ctx.promoted_build_id:
        # What promote_build does after a user build reaches main; started only
        # now, since it reads in its own session.
        try:
            from app.services.agent_reliability_service import AgentReliabilityService
            AgentReliabilityService().schedule_for_build(organization_id=str(organization.id),
                                                         build_id=ctx.promoted_build_id)
        except Exception:
            pass
    try:
        from app.core.telemetry import telemetry
        await telemetry.capture(
            "data_source_cloned",
            {"data_source_id": ctx.clone_id, "source_data_source_id": ctx.source_id,
             **opts.model_dump(exclude={"name", "description"})},
            user_id=user.id, org_id=organization.id,
        )
    except Exception:
        pass
    return ctx.clone_id, ctx.summary
