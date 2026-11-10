"""Whether a viewer's stored result still holds what a run would read now.

A viewer's result (step_user_results) is served back to them by the query
run's cache, the release filter run's cache, the conversation's and the
release's step reads, and counts as their own run for a withheld export. Its
rows depend on more than the step:

- the identity mode it ran under (executed_as), and for a creator-mode run
  whose credentials those were (the owner);
- the auth policy of each connection behind the step's sources;
- each accelerated (FAST) relation those sources hold: the extract it read
  and the relation's row policy;
- the row filter that policy compiles to for this viewer, which reads their
  email, groups, roles and profile attributes;
- the viewer's identity itself when the step is identity-scoped (identity
  params, its own or through what it loads), since a /d read serves the
  latest row whatever values it ran with.

A run stamps all of that before it executes (compute_stamp), and a read
compares the stamp with the current one (viewer_result_current), alongside
the floor every cache already applied (the step and its loaded upstreams
refreshed after the run). A stamp computed before the run can only be older
than what the run read, so a change racing the run makes the row stale, never
current.

The sources are the ones the security rules count for that step
(viewer_data_policy.step_run_data_source_ids, or
dashboard_data.release_copy_data_source_ids for a release copy). This module
only decides freshness: which rows a reader may see at all, and under whose
credentials a run executes, stay with those rules.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional

from sqlalchemy import select

# The memo of the identities a session resolved, per (viewer, org), and the
# listener that drops it when a transaction ends, live where the memo is
# first filled: importing that module registers the listener.
from app.services.viewer_data_policy import IDENTITY_MEMO

logger = logging.getLogger(__name__)

STAMP_VERSION = 1


@dataclass(frozen=True)
class SourceStamp:
    stamp: str
    refs: str


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _tokens(refs: str | None) -> list[str]:
    return (refs or "").split()


def _filter_digest(f) -> Any:
    if f is None:
        return None
    return {"deny": bool(f.deny_all), "column": f.column,
            "values": sorted(f.allowed_values) if f.allowed_values is not None else None}


def _identity_digest(identity) -> str:
    return _digest({
        "user": identity.user_id, "email": identity.email,
        "profile": identity.profile_attributes or {},
        "groups": sorted(identity.group_ids), "group_names": sorted(identity.group_names),
        "roles": sorted(identity.role_ids), "role_names": sorted(identity.role_names),
    })


def _relation_version(ct) -> str:
    """The extract a run reads (its file is replaced on every refresh) and
    the row policy defined on it, as one short token."""
    extract = ct.artifact_path or ""
    policy = {"on": bool(ct.rls_enabled), "mode": ct.rls_mode, "policy": ct.rls_policy,
              "deny": bool(ct.rls_default_deny)}
    return f"{_digest(extract)[:16]}.{_digest(policy)[:16]}"


async def _identity(db, user_id: str, organization_id: str):
    """resolve_identity for this viewer, once per transaction (the same memo
    viewer_data_policy._default_params_fingerprint keeps; its
    _forget_identities drops it when the transaction ends). The viewer is
    read as columns: a session that holds them loaded (a request's caller)
    would otherwise hand back the email it loaded then."""
    memo = db.info.setdefault(IDENTITY_MEMO, {})
    key = (str(user_id), str(organization_id))
    if key not in memo:
        from app.models.user import User
        from app.services.rls_identity_service import resolve_identity

        viewer = (await db.execute(select(User.id, User.email).where(User.id == str(user_id)))).first()
        memo[key] = await resolve_identity(db, viewer, str(organization_id))
    return memo[key]


async def _step_sources(db, step, *, report_id=None, release=None) -> list[str]:
    if release is not None:
        from app.services.dashboard_data import release_copy_data_source_ids

        return list(await release_copy_data_source_ids(db, release, step.code))
    if not report_id:
        return []
    from app.services.viewer_data_policy import step_run_data_source_ids

    return list(await step_run_data_source_ids(db, str(report_id), step.code) or [])


def _relation_columns():
    """What a stamp reads of a relation, selected as plain columns: a
    long-lived session that loaded the relation earlier would otherwise
    hand back its old extract and policy."""
    from app.models.connection_table import ConnectionTable

    return (ConnectionTable.id, ConnectionTable.artifact_path, ConnectionTable.rls_enabled,
            ConnectionTable.rls_mode, ConnectionTable.rls_policy, ConnectionTable.rls_default_deny,
            ConnectionTable.columns)


async def compute_stamp(db, step, *, user_id: str, organization_id: str, executed_as: str,
                        report_id=None, release=None, tainted: Optional[bool] = None,
                        identity=None) -> SourceStamp:
    """The stamp of a run of `step` for `user_id` under `executed_as`, from
    the current state of its sources. `report_id` for a conversation's step,
    `release` for a published dashboard's copy. `tainted`: whether the step
    is identity-scoped, when the caller already asked. `identity`: the
    viewer's identity the run bound its params from, when it already
    resolved it (otherwise resolved here)."""
    from app.models.connection import Connection
    from app.models.connection_table import ConnectionTable, KIND_BOW
    from app.models.datasource_table import DataSourceTable
    from app.models.domain_connection import domain_connection
    from app.services.custom_query_service import CustomQueryService
    from app.services.identity_taint import step_identity_scope

    ds_ids = await _step_sources(db, step, report_id=report_id, release=release)
    connections: list = []
    relations: list = []
    if ds_ids:
        connections = sorted({(str(i), p or "") for i, p in (await db.execute(
            select(Connection.id, Connection.auth_policy)
            .join(domain_connection, domain_connection.c.connection_id == Connection.id)
            .where(domain_connection.c.data_source_id.in_(ds_ids), Connection.deleted_at.is_(None))
        )).all()})
        # One per relation, however many of the sources activate it.
        relations = sorted({str(ct.id): ct for ct in (await db.execute(
            select(*_relation_columns())
            .join(DataSourceTable, DataSourceTable.connection_table_id == ConnectionTable.id)
            .where(DataSourceTable.datasource_id.in_(ds_ids), DataSourceTable.is_active.is_(True),
                   DataSourceTable.deleted_at.is_(None), ConnectionTable.kind == KIND_BOW,
                   ConnectionTable.deleted_at.is_(None))
        )).all()}.values(), key=lambda ct: str(ct.id))

    if tainted is None:
        try:
            tainted, _ = await step_identity_scope(db, step)
        except Exception:
            tainted = True  # unknown: stamp the identity too (closed)
    if identity is None and (tainted or any(ct.rls_enabled for ct in relations)):
        identity = await _identity(db, user_id, organization_id)

    rel_parts = [{"id": str(ct.id), "v": _relation_version(ct),
                  "f": _filter_digest(CustomQueryService.compile_rls(ct, identity)) if ct.rls_enabled else None}
                 for ct in relations]
    payload = {
        "v": STAMP_VERSION,
        "mode": executed_as or "viewer",
        # Whose credentials a creator-mode run used: the owner now.
        "owner": await _owner_id(db, report_id=report_id, release=release) if executed_as == "creator" else None,
        "c": connections,
        "r": rel_parts,
        "i": _identity_digest(identity) if tainted and identity is not None else None,
    }
    refs = [f"c:{cid}={policy}" for cid, policy in connections]
    refs += [f"r:{part['id']}@{part['v']}" for part in rel_parts]
    if payload["i"] is not None:
        refs.append("i")
    return SourceStamp(stamp=_digest(payload), refs=" ".join(refs))


async def _owner_id(db, *, report_id=None, release=None) -> Optional[str]:
    if release is not None:
        from app.models.artifact import Artifact

        owner = (await db.execute(
            select(Artifact.owner_id).where(Artifact.id == str(release.artifact_id))
        )).scalar_one_or_none()
    elif report_id:
        from app.models.report import Report

        owner = (await db.execute(select(Report.user_id).where(Report.id == str(report_id)))).scalar_one_or_none()
    else:
        owner = None
    return str(owner) if owner else None


async def _scope_and_floor(db, step):
    """(identity-scoped?, the latest of the step's and its loaded upstreams'
    refresh) -- the floor every viewer cache applied before stamps."""
    from app.services.identity_taint import step_identity_scope

    floor = getattr(step, "updated_at", None)
    try:
        tainted, upstream = await step_identity_scope(db, step)
    except Exception:
        tainted, upstream = True, None  # unknown: stamp the identity too (closed)
    if upstream is not None and (floor is None or upstream > floor):
        floor = upstream
    return tainted, floor


async def _share_mode(db, *, report_id=None, release=None) -> str:
    if release is not None:
        from app.models.artifact import Artifact

        mode = (await db.execute(
            select(Artifact.shared_run_identity).where(Artifact.id == str(release.artifact_id))
        )).scalar_one_or_none()
    else:
        from app.models.report import Report

        mode = (await db.execute(
            select(Report.shared_run_identity).where(Report.id == str(report_id))
        )).scalar_one_or_none() if report_id else None
    return mode if mode in ("viewer", "creator") else "viewer"


def _legacy_insensitive(refs: str) -> bool:
    """A row stored before stamps (vrstamp01) is accepted only where nothing
    the stamp tracks is in play now: no accelerated relation, every
    connection system_only, no identity-scoped step."""
    for token in _tokens(refs):
        if token == "i" or token.startswith("r:"):
            return False
        if token.startswith("c:") and not token.endswith("=system_only"):
            return False
    return True


class CurrentStamps:
    """viewer_result_current for many stored results in one go. What a run
    reads now -- the step's scope and floor, the dashboard's share mode, the
    stamp -- is worked out once per step, conversation or release, identity
    mode and viewer, however many stored slices (parameter sets) share them;
    each result is then held against it. For one request's reads: nothing
    is kept past it."""

    def __init__(self, db):
        self.db = db
        self._scopes: dict = {}
        self._modes: dict = {}
        self._stamps: dict = {}

    async def stamp(self, step, *, user_id: str, organization_id: str, executed_as: str, report_id=None,
                    release=None, tainted: Optional[bool] = None) -> SourceStamp:
        """compute_stamp, once per key."""
        key = (str(step.id), str(report_id or ""), str(release.id) if release is not None else "",
               executed_as, str(user_id), str(organization_id))
        if key not in self._stamps:
            self._stamps[key] = await compute_stamp(
                self.db, step, user_id=user_id, organization_id=organization_id, executed_as=executed_as,
                report_id=report_id, release=release, tainted=tainted)
        return self._stamps[key]

    async def current(self, row, step, *, report_id=None, release=None) -> bool:
        """viewer_result_current."""
        if row is None:
            return False
        if str(step.id) not in self._scopes:
            self._scopes[str(step.id)] = await _scope_and_floor(self.db, step)
        tainted, floor = self._scopes[str(step.id)]
        if floor is not None and row.last_run_at is not None and row.last_run_at < floor:
            return False
        if row.executed_as == "creator":
            mode_key = (str(report_id or ""), str(release.id) if release is not None else "")
            if mode_key not in self._modes:
                self._modes[mode_key] = await _share_mode(self.db, report_id=report_id, release=release)
            if self._modes[mode_key] != "creator":
                return False
        organization_id = release.organization_id if release is not None else row.organization_id
        current = await self.stamp(step, user_id=row.user_id, organization_id=organization_id,
                                   executed_as=row.executed_as, report_id=report_id, release=release,
                                   tainted=tainted)
        if row.source_stamp is None:
            return _legacy_insensitive(current.refs)
        return row.source_stamp == current.stamp


async def viewer_result_current(db, row, step, *, report_id=None, release=None) -> bool:
    """Whether a viewer's stored successful result may still be served or
    reused: no newer step or loaded upstream than the run, a creator-mode run
    only while the dashboard still runs as its creator, and the stamp of the
    sources as they are now equal to the one stored. `report_id` for a
    conversation's step, `release` for a release copy. For many results at
    once, CurrentStamps."""
    return await CurrentStamps(db).current(row, step, report_id=report_id, release=release)


# Ids per IN list: far below the drivers' bind-parameter cap (32,767 on
# asyncpg, 32,766 on SQLite).
ID_CHUNK = 1000


def id_chunks(ids: Iterable) -> Iterable[list]:
    """`ids` in lists of at most ID_CHUNK, for IN clauses."""
    ids = list(ids)
    for start in range(0, len(ids), ID_CHUNK):
        yield ids[start:start + ID_CHUNK]


def wait_stamp() -> dict:
    """The stamp columns of a wait for input: none (never judged)."""
    return {"source_stamp": None, "source_refs": None}


async def stamp_columns(db, step, *, user_id: str, organization_id: str, executed_as: str,
                        report_id=None, release=None, identity=None) -> dict:
    """compute_stamp as the columns store_viewer_result writes."""
    s = await compute_stamp(db, step, user_id=user_id, organization_id=organization_id,
                            executed_as=executed_as, report_id=report_id, release=release, identity=identity)
    return {"source_stamp": s.stamp, "source_refs": s.refs}


async def stamps_before_clients(db, targets, load: Callable[[Any], Awaitable[Any]], *, user_id: str,
                                organization_id: str, executed_as: str, report_id=None, release=None) -> dict:
    """stamp_columns of each step a batch run (/r, /d) is about to run, by
    step id, taken before it builds any client: a change landing while the
    clients are built or earlier steps run leaves the later results stale,
    never current. `targets`: (step id, code) pairs; a step without code is
    not stamped. `load(step_id)` loads the step (None: not stamped).
    A stamp that fails is logged and left out: the step's run stamps it then,
    or fails on the same cause. A failed SQL statement here is worse on
    Postgres: it aborts the request's transaction, so the rest of the run
    fails with it (stored results included)."""
    stamps: dict = {}
    for step_id, code in targets:
        if not code or not str(code).strip():
            continue
        try:
            step = await load(step_id)
            if step is not None:
                stamps[str(step_id)] = await stamp_columns(
                    db, step, user_id=user_id, organization_id=organization_id, executed_as=executed_as,
                    report_id=report_id, release=release)
        except Exception as e:
            logger.warning("Viewer run: could not stamp step %s before its clients were built: %s", step_id, e)
    return stamps


__all__ = [
    "SourceStamp", "compute_stamp", "stamp_columns", "stamps_before_clients", "wait_stamp",
    "viewer_result_current", "CurrentStamps", "id_chunks",
]
