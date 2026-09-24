"""Running a saved query on one of its agents.

A saved query (Entity) is shared with one or more agents and runs against the
agent it is run FROM. This module is the one place that decides:

* which agent a run is made from (`pick_target`),
* what code runs there and with which clients (`prepare_run`) — the stored
  code is agent-free (see entity_code) and is rendered for that agent,
* where each agent's shared result lives (`snapshot_of` / `store_snapshot`):
  the origin agent's in Entity.data as before, every other agent's in
  entity_agent_snapshots,
* what happens to a query when one of its agents goes away
  (`on_agent_removed`).

Every run path (entity page, preview, describe_entity, load_entity) goes
through here so none of them can fall back to merging every agent's clients —
the behaviour that made a query shared with jtlv and jtlv2 always read jtlv.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.services import entity_code as ec


def attached_agents(entity) -> list:
    return list(getattr(entity, "data_sources", None) or [])


def origin_id(entity) -> Optional[str]:
    """The query's origin agent id. Rows saved before origins existed (or
    whose origin was removed) fall back to their first attached agent."""
    oid = getattr(entity, "origin_data_source_id", None)
    agents = attached_agents(entity)
    ids = [str(a.id) for a in agents]
    if oid and str(oid) in ids:
        return str(oid)
    return ids[0] if ids else None


def code_mode(entity) -> str:
    """The stored classification, or a fresh one for rows never classified."""
    mode = getattr(entity, "code_mode", None)
    if mode:
        return mode
    if not attached_agents(entity):
        return ec.MODE_BOUND  # agentless: runs on the agents its code names
    return ec.templatize(getattr(entity, "code", "") or "", [ec.agent_info(a) for a in attached_agents(entity)]).mode


def apply_code(entity, code: str, agents: Iterable, preferred_origin_id: Optional[str] = None) -> ec.Templated:
    """Store `code` on the entity agent-free when possible, and set its mode
    and origin. `agents` are the agents its keys may name."""
    agents = list(agents)
    if not agents:
        # A query saved from an Auto report has no agent rows: its code keeps
        # the agent names it was written with, and runs on exactly those
        # (prepare_run) — there is no agent to take the name out for.
        entity.code = code or ""
        entity.code_mode = None
        entity.origin_data_source_id = None
        return ec.Templated(code=entity.code, mode=ec.MODE_BOUND)
    result = ec.templatize(code or "", [ec.agent_info(a) for a in agents])
    entity.code = result.code
    entity.code_mode = result.mode
    ids = [str(a.id) for a in agents]
    if (
        preferred_origin_id and str(preferred_origin_id) in ids
        and result.mode in ec.SHAREABLE_MODES
    ):
        # An existing (or explicitly chosen) origin stays: the editor shows
        # each agent its own keys, so code saved from another agent's view
        # names THAT agent — which says where it was edited, not who owns it.
        entity.origin_data_source_id = str(preferred_origin_id)
    elif result.origin_id:
        entity.origin_data_source_id = result.origin_id
    elif preferred_origin_id and str(preferred_origin_id) in ids:
        entity.origin_data_source_id = str(preferred_origin_id)
    elif result.mode == ec.MODE_BOUND and result.agent_ids:
        entity.origin_data_source_id = next(i for i in ids if i in result.agent_ids)
    else:
        entity.origin_data_source_id = ids[0] if ids else None
    return result


def pick_target(entity, data_source_id: Optional[str] = None, run_agent_ids: Optional[Iterable[str]] = None):
    """The agent a run of `entity` is made from.

    Explicit request > the origin agent when it is among the agents in play >
    the first of the query's agents in play > the origin agent. None only for
    a query attached to no agent at all. An explicitly requested agent the
    query is not shared with is a typed error, never a silent substitute.
    """
    agents = attached_agents(entity)
    by_id = {str(a.id): a for a in agents}
    if data_source_id:
        target = by_id.get(str(data_source_id))
        if target is None:
            raise AppError.bad_request(
                ErrorCode.ENTITY_AGENT_NOT_ATTACHED,
                "This query is not shared with that agent.",
                data_source_id=str(data_source_id),
            )
        return target
    oid = origin_id(entity)
    in_play = [str(i) for i in (run_agent_ids or [])]
    if in_play:
        if oid in in_play:
            return by_id[oid]
        for i in in_play:
            if i in by_id:
                return by_id[i]
    return by_id.get(oid) if oid else None


@dataclass
class PreparedRun:
    code: str
    ds_clients: Dict[str, Any]
    target_id: Optional[str]
    errors: List[str]


async def _clients_for(db, agents: list, user, *, tolerate_errors: bool) -> tuple[dict, list]:
    from app.services.data_source_service import DataSourceService
    svc = DataSourceService()
    clients: dict = {}
    errors: list = []
    for ds in agents:
        try:
            clients.update(await svc.construct_clients(db, ds, current_user=user))
        except Exception as e:
            if not tolerate_errors:
                raise
            errors.append(str(getattr(e, "detail", None) or e))
    return clients, errors


async def prepare_run(
    db: AsyncSession, entity, target, user, *, code: Optional[str] = None,
    mode: Optional[str] = None,
) -> PreparedRun:
    """The code and clients one run of `entity` on `target` uses.

    `code` overrides the stored code (an unsaved edit); it is classified
    against the query's agents first so a pasted concrete key works too.
    """
    agents = attached_agents(entity)
    if code is not None:
        t = ec.templatize(code, [ec.agent_info(a) for a in agents])
        code, mode = t.code, t.mode
    else:
        code = getattr(entity, "code", "") or ""
        mode = mode or code_mode(entity)

    if not agents:
        # A query on BOW data alone reaches it through the "bow" client
        # install_entity_client adds; nothing else may be built for it.
        if getattr(entity, "bow_source_access", None):
            return PreparedRun(code=code, ds_clients={}, target_id=None, errors=[])
        return await _prepare_agentless(db, entity, code, user)
    if mode == ec.MODE_UNRESOLVED:
        # Classified before its agents allowed a repair (or before repairs
        # existed): try again against the agents it has now.
        retry = ec.templatize(code, [ec.agent_info(a) for a in agents])
        code, mode = retry.code, retry.mode
    if mode == ec.MODE_UNRESOLVED:
        ec.check_runnable_on(code, mode, ec.agent_info(target) if target is not None else ec.AgentInfo(id="", name=""))
    if mode == ec.MODE_BOUND:
        named = ec.templatize(code, [ec.agent_info(a) for a in agents]).agent_ids
        needed = [a for a in agents if str(a.id) in named] or agents
        clients, errors = await _clients_for(db, needed, user, tolerate_errors=True)
        if not clients and errors:
            raise ValueError("; ".join(errors[:2]))
        return PreparedRun(code=code, ds_clients=clients, target_id=str(target.id) if target is not None else None, errors=errors)

    if target is None:
        raise AppError.bad_request(ErrorCode.ENTITY_NO_AGENT, "Choose an agent to run this query on.")
    rendered = ec.render(code, ec.agent_info(target))
    clients, _ = await _clients_for(db, [target], user, tolerate_errors=False)
    return PreparedRun(code=rendered, ds_clients=clients, target_id=str(target.id), errors=[])


async def _prepare_agentless(db: AsyncSession, entity, code: str, user) -> PreparedRun:
    """A query attached to no agent (saved from an Auto report) runs on the
    agents its code names by key — resolved among the organization's agents,
    each built with the caller's own credentials — and on nothing else.

    It used to be handed every agent of the organization; code that names no
    agent (it iterates `ds_clients`) would then read all of them. Such code
    now has nothing to run on and says so.
    """
    from app.models.data_source import DataSource
    org_agents = list((await db.execute(
        select(DataSource).where(
            DataSource.organization_id == str(entity.organization_id),
            DataSource.deleted_at.is_(None),
        )
    )).scalars().unique().all())
    t = ec.templatize(code or "", [ec.agent_info(a) for a in org_agents], repair=False)
    if t.mode == ec.MODE_UNRESOLVED:
        ec.check_runnable_on(code, t.mode, ec.AgentInfo(id="", name=""))
    named = [a for a in org_agents if str(a.id) in t.agent_ids]
    keys, reaches_clients = ec.client_keys(code or "")
    if not named and not reaches_clients and not [k for k in keys if k not in ec.NEUTRAL_KEYS]:
        # Code that reads no data source (pure pandas, BOW only) needs none.
        return PreparedRun(code=code or "", ds_clients={}, target_id=None, errors=[])
    if not named:
        raise AppError.bad_request(
            ErrorCode.ENTITY_NO_AGENT,
            "This query is not attached to any agent and its code names none, so there is nothing to run it against.",
        )
    clients, errors = await _clients_for(db, named, user, tolerate_errors=True)
    if not clients and errors:
        raise ValueError("; ".join(errors[:2]))
    return PreparedRun(code=code or "", ds_clients=clients, target_id=None, errors=errors)


def render_for(entity, target) -> str:
    """The entity's code as it runs on `target` — what leaves the query when
    it is copied into a report step or shown to the model. Never raises:
    code that cannot run there is returned as stored."""
    code = getattr(entity, "code", "") or ""
    if target is None:
        return code
    try:
        return ec.render(code, ec.agent_info(target))
    except AppError:
        return code


# ---------------------------------------------------------------------------
# Per-agent shared results
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    data: dict
    applied_params: Optional[dict]
    last_refreshed_at: Optional[datetime]
    # Why this agent has no rows: its last run failed (e.g. the connection was
    # unreachable when the query was shared with it). None when it succeeded
    # or never ran.
    error: Optional[str] = None


def _is_origin(entity, data_source_id: Optional[str]) -> bool:
    if data_source_id is None:
        return True
    # Only a per-agent query has a result per agent. One that reads several
    # agents together (bound), cannot run (unresolved) or has no agent has ONE
    # result — Entity.data — whichever agent it is opened or refreshed from.
    if getattr(entity, "code_mode", None) not in ec.SHAREABLE_MODES:
        return True
    # The stored origin first: a row being created has no loaded `data_sources`
    # to fall back on (and touching them there would lazy-load in async).
    oid = getattr(entity, "origin_data_source_id", None)
    if oid:
        return str(data_source_id) == str(oid)
    return str(data_source_id) == (origin_id(entity) or "")


async def snapshot_of(db: AsyncSession, entity, data_source_id: Optional[str]) -> Snapshot:
    if _is_origin(entity, data_source_id):
        return Snapshot(
            data=getattr(entity, "data", None) or {},
            applied_params=getattr(entity, "applied_params", None),
            last_refreshed_at=getattr(entity, "last_refreshed_at", None),
        )
    from app.models.entity_agent_snapshot import EntityAgentSnapshot
    row = (await db.execute(
        select(EntityAgentSnapshot).where(
            EntityAgentSnapshot.entity_id == str(entity.id),
            EntityAgentSnapshot.data_source_id == str(data_source_id),
        )
    )).scalars().first()
    if row is None:
        return Snapshot(data={}, applied_params=None, last_refreshed_at=None)
    if row.status != "success":
        # The last run failed; the last good rows (if any) are still served,
        # with the reason — the same as the origin, whose failed refresh keeps
        # Entity.data.
        return Snapshot(
            data=row.data or {}, applied_params=row.applied_params,
            last_refreshed_at=row.last_refreshed_at, error=row.status_reason or "run failed",
        )
    return Snapshot(data=row.data or {}, applied_params=row.applied_params, last_refreshed_at=row.last_refreshed_at)


async def store_snapshot(
    db: AsyncSession, entity, data_source_id: Optional[str], data: Optional[dict],
    applied_params: Optional[dict], *, error: Optional[str] = None,
) -> None:
    """Record a run's shared result for one agent (caller commits). A failed
    run records the attempt but never overwrites the last good rows."""
    now = datetime.utcnow()
    if _is_origin(entity, data_source_id):
        if error is None:
            entity.data = data
            entity.applied_params = dict(applied_params) if applied_params else None
        entity.last_refreshed_at = now
        return
    import uuid
    from app.models.entity_agent_snapshot import EntityAgentSnapshot
    # One statement, not select-then-insert: two refreshes of the same agent
    # at once must not both insert and trip the (entity, agent) unique key.
    if error is None:
        values = {
            "data": data, "applied_params": dict(applied_params) if applied_params else None,
            "status": "success", "status_reason": None,
        }
    else:
        values = {"status": "error", "status_reason": error[:2000]}
    values["last_refreshed_at"] = now
    values["updated_at"] = now
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(EntityAgentSnapshot).values(
        id=str(uuid.uuid4()), entity_id=str(entity.id), data_source_id=str(data_source_id),
        organization_id=str(entity.organization_id), created_at=now,
        **({"data": {}, "status": "error"} | values if error is not None else values),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[EntityAgentSnapshot.entity_id, EntityAgentSnapshot.data_source_id],
        set_=values,
    )
    await db.execute(stmt)


async def drop_snapshots(db: AsyncSession, entity_id: str, data_source_ids: Optional[Iterable[str]] = None) -> None:
    """Forget non-origin results — all of them (the code changed), or those
    of agents that left the query (caller commits)."""
    from app.models.entity_agent_snapshot import EntityAgentSnapshot
    from app.models.entity_user_result import EntityUserResult
    snap = delete(EntityAgentSnapshot).where(EntityAgentSnapshot.entity_id == str(entity_id))
    slices = delete(EntityUserResult).where(EntityUserResult.entity_id == str(entity_id))
    if data_source_ids is not None:
        ids = [str(i) for i in data_source_ids]
        if not ids:
            return
        snap = snap.where(EntityAgentSnapshot.data_source_id.in_(ids))
        slices = slices.where(EntityUserResult.data_source_id.in_(ids))
    await db.execute(snap)
    await db.execute(slices)


async def hand_origin_to(
    db: AsyncSession, entity, new_origin_id: Optional[str], *, previous_origin_id: Optional[str] = None,
) -> None:
    """Make `new_origin_id` the query's origin, moving its shared result into
    Entity.data (caller commits). None leaves the query with no agent."""
    from app.models.entity_agent_snapshot import EntityAgentSnapshot
    if new_origin_id is None:
        entity.origin_data_source_id = None
        return
    old_origin = str(previous_origin_id) if previous_origin_id else origin_id(entity)
    if old_origin == str(new_origin_id):
        entity.origin_data_source_id = str(new_origin_id)
        return
    old_rows = (getattr(entity, "data", None), getattr(entity, "applied_params", None),
                getattr(entity, "last_refreshed_at", None))
    row = (await db.execute(
        select(EntityAgentSnapshot).where(
            EntityAgentSnapshot.entity_id == str(entity.id),
            EntityAgentSnapshot.data_source_id == str(new_origin_id),
        )
    )).scalars().first()
    if row is not None and row.data:
        entity.data = row.data or {}
        entity.applied_params = row.applied_params
        entity.last_refreshed_at = row.last_refreshed_at
    else:
        # The new origin never ran: the old origin's rows are not its rows.
        entity.data = {}
        entity.applied_params = None
        entity.last_refreshed_at = None
    if row is not None:
        await db.delete(row)
        await db.flush()
    entity.origin_data_source_id = str(new_origin_id)
    # The old origin keeps its rows when it stays on the query.
    still_attached = {str(a.id) for a in attached_agents(entity)}
    if old_origin and old_origin in still_attached and old_rows[0]:
        keep = EntityAgentSnapshot(
            entity_id=str(entity.id), data_source_id=old_origin,
            organization_id=str(entity.organization_id),
            data=old_rows[0], applied_params=old_rows[1], last_refreshed_at=old_rows[2],
            status="success",
        )
        db.add(keep)


async def on_agent_removed(db: AsyncSession, data_source_id: str) -> None:
    """An agent is being deleted: every query it was the origin of passes to
    its next agent; results kept for it are dropped (caller commits, and
    deletes the association rows as before)."""
    from app.models.entity import Entity, entity_data_source_association
    ds_id = str(data_source_id)
    entity_ids = [r[0] for r in (await db.execute(
        select(entity_data_source_association.c.entity_id).where(
            entity_data_source_association.c.data_source_id == ds_id
        )
    )).all()]
    if not entity_ids:
        return
    from sqlalchemy.orm import selectinload
    entities = (await db.execute(
        select(Entity).options(selectinload(Entity.data_sources)).where(Entity.id.in_(entity_ids))
    )).scalars().all()
    for entity in entities:
        if origin_id(entity) == ds_id:
            remaining = [str(a.id) for a in attached_agents(entity) if str(a.id) != ds_id]
            await hand_origin_to(db, entity, remaining[0] if remaining else None, previous_origin_id=ds_id)
            try:
                from app.ee.audit.service import audit_service
                await audit_service.log(
                    db=db, organization_id=str(entity.organization_id),
                    action="entity.origin_reassigned", user_id=None,
                    resource_type="entity", resource_id=str(entity.id),
                    details={"from": ds_id, "to": remaining[0] if remaining else None},
                    commit=False,
                )
            except Exception:
                pass
    from app.models.entity_agent_snapshot import EntityAgentSnapshot
    await db.execute(delete(EntityAgentSnapshot).where(EntityAgentSnapshot.data_source_id == ds_id))


def validate_sharing(result: ec.Templated, agents: Iterable) -> None:
    """Refuse to share a query with an agent it cannot run on — the moment
    the agent is added, not on the first run from it."""
    agents = list(agents)
    if not agents:
        return
    if result.mode == ec.MODE_UNRESOLVED:
        raise AppError.bad_request(
            ErrorCode.ENTITY_UNRESOLVED_CODE,
            "This query's code names an agent or connection that is not among the selected agents.",
        )
    if result.mode == ec.MODE_BOUND:
        extra = [a for a in agents if str(a.id) not in result.agent_ids]
        if extra:
            raise AppError.bad_request(
                ErrorCode.ENTITY_NOT_SHAREABLE,
                f'This query reads specific agents together, so it cannot also be shared with "{extra[0].name}".',
                agent=str(extra[0].name),
            )
        return
    for a in agents:
        ec.check_runnable_on(result.code, result.mode, ec.agent_info(a))


async def load_agents(db: AsyncSession, ids: Iterable[str]) -> list:
    """DataSource rows (connections loaded) for `ids`, in the given order."""
    from app.models.data_source import DataSource
    ids = [str(i) for i in ids]
    if not ids:
        return []
    rows = (await db.execute(select(DataSource).where(DataSource.id.in_(ids)))).scalars().unique().all()
    by_id = {str(r.id): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def log_repair(
    db: AsyncSession, entity, result: ec.Templated, user_id: Optional[str], *, commit: bool = True,
) -> None:
    """Record keys a save repaired (entity_code._repair_type), so what changed
    in the stored code — and what it was — can always be traced."""
    if not getattr(result, "repaired", None):
        return
    try:
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db=db, organization_id=str(entity.organization_id),
            action="entity.code_repaired", user_id=user_id,
            resource_type="entity", resource_id=str(entity.id),
            details={"repaired": dict(result.repaired), "title": entity.title},
            commit=commit,
        )
    except Exception:
        pass
