"""Lists, saved queries and evals for an agent clone.

Lists: copy (a new, empty list with the same fields, field ids and machine
name) or link (the same list and rows on both agents, see agent_lists).

Saved queries: copy only. Their code names the agent it runs on
(``ds_clients["<agent>:<connection>"]``, its ``::fast`` sibling, or the bare
``"<agent>"`` alias), so the copy's code is rewritten to the clone's name. When
lists were copied, list ids in the code (and ``bow.<agent>.lists.`` names) move
to the copies too, or the copy would keep reading the source's lists. Code
that reaches ``ds_clients`` any other way (a computed key, ``.items()``) can't
be rewritten safely and is not copied; it is reported in the summary.

Evals: suites homed on the source, with their cases, re-pointed at the clone.
Archived cases stay behind, and so do cases with no agent (they already run
on every agent, the clone included) and cases that don't run on the source
(copying them would only duplicate them). A suite left with no case is not
created.
"""
from __future__ import annotations

import re
import uuid
from typing import Dict, List, Optional

from sqlalchemy import insert, or_, select

from app.models.agent_list import AgentList, agent_list_data_sources as list_links
from app.models.entity import Entity, entity_data_source_association as entity_links
from app.models.eval import TEST_CASE_STATUS_ARCHIVED, TestCase, TestSuite
from app.services.agent_clone_service import copy_columns, remap_mention_text, remap_mentions

# Same literal-key access the fork service trusts (fork_service._CLIENT_KEY_ACCESS).
_CLIENT_KEY = re.compile(
    r"""(?P<head>\bds_clients\s*(?:\[\s*|\.get\(\s*))(?P<q>['"])(?P<key>[^'"\n]+)(?P=q)"""
)
_CLIENT_NAME = re.compile(r"\bds_clients\b")
# ``def generate_df(ds_clients, excel_files, params)``: naming the parameter is
# not a use of it.
_DEF_PARAMS = re.compile(r"\bdef\s+\w+\s*\(([^)]*)\)")


def rewrite_client_keys(code: str, old: str, new: str, connection_names=None) -> Optional[str]:
    """Code with every client key of agent ``old`` renamed to ``new``, or None
    when ``ds_clients`` is reached other than through a literal key.

    Keys are ``<agent>``, ``<agent>:<connection>`` and ``<agent>:<connection>::fast``.
    Agent names may hold ':' themselves, so with ``connection_names`` given only
    those exact keys are renamed (``"Sales: EU:pg"`` is not agent ``Sales``)."""
    code = code or ""
    params = sum(len(_CLIENT_NAME.findall(m.group(1))) for m in _DEF_PARAMS.finditer(code))
    if len(_CLIENT_KEY.findall(code)) != len(_CLIENT_NAME.findall(code)) - params:
        return None
    if connection_names is None:
        ours = None
    else:
        ours = {old} | {f"{old}:{c}" for c in connection_names} | {f"{old}:{c}::fast" for c in connection_names}

    def _sub(m):
        key = m.group("key")
        if key == old or (key.startswith(old + ":") and (ours is None or key in ours)):
            key = new + key[len(old):]
        q = m.group("q")
        if q in key:  # the new name holds the quote character
            q = "'" if q == '"' else '"'
        return f"{m.group('head')}{q}{key}{q}"
    return _CLIENT_KEY.sub(_sub, code)


# ── lists ──────────────────────────────────────────────────────────────────

async def clone_lists(ctx) -> None:
    from app.models.data_source import DataSource
    from app.services.agent_lists import service as lists_svc
    from app.services.agent_lists.naming import table_name_for

    part = ctx.part("lists")
    if ctx.opts.lists == "none":
        return
    lists = await lists_svc.live_lists_for_agents(ctx.db, [ctx.source_id])
    if ctx.opts.lists == "link":
        for lst in lists:
            await lists_svc.link_list(ctx.db, lst, ctx.clone, commit=False)
            ctx.list_map[str(lst.id)] = str(lst.id)
            part.linked += 1
        return
    if not lists:
        return
    # Code may name a list after any agent it is linked to.
    agent_ids = await lists_svc.linked_agent_ids(ctx.db, [l.id for l in lists])
    names = dict((await ctx.db.execute(
        select(DataSource.id, DataSource.name)
        .where(DataSource.id.in_({a for ids in agent_ids.values() for a in ids}))
    )).all())
    copies = []
    for lst in lists:
        copy_id = str(uuid.uuid4())
        copies.append(copy_id)
        ctx.db.add(AgentList(id=copy_id, **copy_columns(
            lst, data_source_id=ctx.clone_id, created_by_user_id=str(ctx.user.id), version=1,
        )))
        ctx.list_map[str(lst.id)] = copy_id
        for ds_id in agent_ids.get(str(lst.id), []):
            ctx.list_names.append((table_name_for(names[ds_id], ds_id, lst.slug),
                                   table_name_for(ctx.clone.name, ctx.clone_id, lst.slug)))
        part.copied += 1
    await ctx.db.flush()
    await ctx.db.execute(insert(list_links), [
        {"list_id": copy_id, "data_source_id": ctx.clone_id} for copy_id in copies
    ])


# ── saved queries ──────────────────────────────────────────────────────────

def _rewrite_lists(code: str, ctx) -> str:
    if ctx.opts.lists != "copy":
        return code
    for old, new in ctx.list_map.items():
        code = code.replace(old, new)
    for old, new in ctx.list_names:
        code = code.replace(old, new)
    return code


async def _unique_slugs(ctx, texts: List[str]) -> List[str]:
    """``EntityService._unique_slug`` for many at once: one query for the
    slugs already taken, then ``-2``, ``-3``, ... in memory."""
    from app.services.entity_service import EntityService

    bases = [EntityService.slugify(t) for t in texts]
    if not bases:
        return []
    taken = set((await ctx.db.execute(
        select(Entity.slug).where(
            Entity.organization_id == str(ctx.organization.id),
            or_(*[or_(Entity.slug == b, Entity.slug.like(f"{b}-%")) for b in set(bases)]),
        )
    )).scalars().all())
    out = []
    for base in bases:
        candidate, n = base, 1
        while candidate in taken:
            n += 1
            candidate = f"{base}-{n}"
        taken.add(candidate)
        out.append(candidate)
    return out


async def clone_saved_queries(ctx) -> None:
    part = ctx.part("saved_queries")
    if ctx.opts.saved_queries != "copy":
        return
    rows = (await ctx.db.execute(
        select(Entity)
        .join(entity_links, entity_links.c.entity_id == Entity.id)
        .where(entity_links.c.data_source_id == ctx.source_id, Entity.deleted_at.is_(None))
        .order_by(Entity.created_at, Entity.id)
    )).scalars().unique().all()
    conn_names = [c.name for c in (ctx.source.connections or [])]
    todo = []
    for e in rows:
        code = rewrite_client_keys(e.code, ctx.source.name, ctx.clone.name, conn_names)
        if code is None:
            ctx.skip("saved_queries", e.id, e.title,
                     "Its code reaches the agent's connections in a way that can't be renamed safely. "
                     "Save it again from a conversation on the new agent.")
            continue
        todo.append((e, code))
    if not todo:
        return
    agents: Dict[str, set] = {}
    for eid, ds_id in (await ctx.db.execute(
        select(entity_links.c.entity_id, entity_links.c.data_source_id)
        .where(entity_links.c.entity_id.in_([str(e.id) for e, _ in todo]))
    )).all():
        agents.setdefault(str(eid), set()).add(str(ds_id))
    slugs = await _unique_slugs(ctx, [e.slug or e.title or "query" for e, _ in todo])
    link_rows = []
    for (e, code), slug in zip(todo, slugs):
        copy_id = str(uuid.uuid4())
        ctx.db.add(Entity(id=copy_id, **copy_columns(
            e,
            code=_rewrite_lists(code, ctx),
            owner_id=str(ctx.user.id),
            slug=slug,
            status="draft", published_at=None,
            private_status="draft" if e.private_status else None, global_status=None, reviewed_by_user_id=None,
            # The source's snapshot was materialized with its owner's identity:
            # never hand it to someone else. The first run builds the clone's.
            data={}, applied_params=None, last_refreshed_at=None,
            # Without BOW-source access the step is only provenance. With it,
            # the step's report is what gates that access, so it stays.
            source_step_id=e.source_step_id if e.bow_source_access else None,
        )))
        # Other agents stay: the query's code reads them too, and a run only
        # builds clients for the query's own agents.
        for ds_id in sorted({ctx.clone_id if a == ctx.source_id else a for a in agents.get(str(e.id), ())}):
            link_rows.append({"entity_id": copy_id, "data_source_id": ds_id})
        ctx.entity_map[str(e.id)] = copy_id
        part.copied += 1
    await ctx.db.flush()
    await ctx.db.execute(insert(entity_links), link_rows)


# ── evals ──────────────────────────────────────────────────────────────────

def _remap_prompt(prompt, ctx):
    p = remap_mentions(dict(prompt or {}), ctx)
    if isinstance(p.get("content"), str):
        p["content"] = remap_mention_text(p["content"], ctx)
    # Runtime-only pointers into the source's conversations.
    for k in ("widget_id", "step_id"):
        if k in p:
            p[k] = None
    return p


def _remap_turn(turn, ctx):
    # Follow-up turns are {"prompt": {...}, ...}.
    if isinstance(turn, dict) and isinstance(turn.get("prompt"), dict):
        return {**turn, "prompt": _remap_prompt(turn["prompt"], ctx)}
    return _remap_prompt(turn, ctx)


async def clone_evals(ctx) -> None:
    part = ctx.part("evals")
    if ctx.opts.evals != "copy":
        return
    suites = (await ctx.db.execute(
        select(TestSuite).where(TestSuite.data_source_id == ctx.source_id, TestSuite.deleted_at.is_(None))
        .order_by(TestSuite.created_at, TestSuite.id)
    )).scalars().all()
    if not suites:
        return
    cases_by_suite: Dict[str, List[TestCase]] = {}
    for c in (await ctx.db.execute(
        select(TestCase).where(TestCase.suite_id.in_([str(s.id) for s in suites]), TestCase.deleted_at.is_(None))
        .order_by(TestCase.created_at, TestCase.id)
    )).scalars().all():
        cases_by_suite.setdefault(str(c.suite_id), []).append(c)

    for s in suites:
        keep = []
        for c in cases_by_suite.get(str(s.id), []):
            agents = [str(a) for a in (c.data_source_ids_json or [])]
            if c.status == TEST_CASE_STATUS_ARCHIVED:
                ctx.skip("evals", c.id, c.name, "Archived.")
            elif not agents:
                ctx.skip("evals", c.id, c.name, "Has no agent, so it already runs on every agent, the new one too.")
            elif ctx.source_id not in agents:
                # Copying it would run it on agents the clone isn't, with no
                # change from the original: a duplicate, not a copy.
                ctx.skip("evals", c.id, c.name, "Doesn't run on this agent.")
            else:
                keep.append((c, agents))
        if not keep:
            ctx.skip("evals", s.id, s.name, "No case left to copy, so the suite isn't created.")
            continue
        copy_id = str(uuid.uuid4())
        ctx.db.add(TestSuite(id=copy_id, **copy_columns(s, data_source_id=ctx.clone_id)))
        for c, agents in keep:
            ctx.db.add(TestCase(id=str(uuid.uuid4()), **copy_columns(
                c, suite_id=copy_id,
                data_source_ids_json=[ctx.clone_id if a == ctx.source_id else a for a in agents],
                prompt_json=_remap_prompt(c.prompt_json, ctx),
                additional_turns_json=[_remap_turn(t, ctx) for t in c.additional_turns_json]
                if isinstance(c.additional_turns_json, list) else c.additional_turns_json,
                source_completion_id=None, source_agent_execution_id=None, source_feedback_id=None,
            )))
            part.copied += 1
    await ctx.db.flush()
