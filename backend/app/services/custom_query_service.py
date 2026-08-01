"""Custom queries — admin-authored SQL materialized to encrypted local artifacts.

Ownership note: a custom query is a **connection-level** object even though the
UI lets you create one from an agent's tables page. The artifact is one physical
file shared by every agent that activates it — if it were per-agent, N agents
would extract the same data N times on N schedules, multiplying exactly the
source load the feature exists to remove.

Gated on `manage_connection` (same class of action as reindex) and on
`auth_policy == 'system_only'`: with per-user credentials, one shared copy would
collapse every user's row visibility into whoever ran the refresh. RLS lands in
phase 2 and is what will unlock `user_required`.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scheduler import scheduler
from app.data_sources.fast import artifacts, extractor, rls
from app.ee.license import has_feature
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, KIND_TABLE, ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.user import User

logger = logging.getLogger(__name__)

# Connector types where acceleration is offered. Everything else simply does not
# show the option — extraction is `execute_query`-shaped, so adding a type is
# dialect + streaming verification rather than a new interface.
#
# The split is deliberately visible. Every type here has an extraction source
# (`fast/sources.py`) and is unit-tested; only the first group has been run
# against a live server end to end.
#
# The unverified two are why this still ships behind `enable_custom_queries`.
# Fabric speaks T-SQL through an Entra token, so it borrows the SQL Server
# dialect without ever having proven that Fabric's SQL analytics endpoint
# supports SHOWPLAN_XML or permits KILL — and its SQL port is unreachable from
# the build environment, so that cannot be checked here. Sybase SQL Anywhere
# has a native pyodbc extraction source (fast/sybase_source.py) whose
# GRAPHICAL_PLAN estimator and SQLCancel-over-FreeTDS early-stop are untested
# against a live engine — no SQL Anywhere instance was reachable either.
#
# SQL Server and Oracle graduated once they were run against real engines
# (SQL Server 2022 and Oracle Free 23ai in Docker). That run was worth doing:
# SHOWPLAN_XML and EXPLAIN PLAN both worked first time, but SQL Server
# cancellation did not — KILL is rejected inside SQLAlchemy's implicit
# transaction, so every cancel had been failing silently. A fake could not have
# caught it.
VERIFIED_TYPES = {"postgresql", "mariadb", "mysql", "sqlite", "snowflake",
                  "bigquery", "mssql", "oracledb"}
UNVERIFIED_TYPES = {"ms_fabric", "sybase"}
ACCELERABLE_TYPES = VERIFIED_TYPES | UNVERIFIED_TYPES


def is_accelerable_type(conn_type) -> bool:
    """Whether a stored `Connection.type` is offered acceleration.

    Case-insensitive because the registry is not: SQL Server is registered as
    "MSSQL" — the registry's one uppercase key — so a literal membership test
    against these lowercase sets silently excluded every SQL Server connection.
    """
    return (conn_type or "").lower() in ACCELERABLE_TYPES

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# One refresh at a time per connection: a refresh is a full scan against the
# source, and the whole point is to be gentle with it.
_refresh_locks: dict[str, asyncio.Lock] = {}


def _lock_for(connection_id: str) -> asyncio.Lock:
    if connection_id not in _refresh_locks:
        _refresh_locks[connection_id] = asyncio.Lock()
    return _refresh_locks[connection_id]


def job_id_for(cq_id: str) -> str:
    return f"custom_query_{cq_id}"


def next_run_at(cq_id: str):
    """When the next scheduled refresh fires, read off the shared job store.

    Module-level so callers that only need this one fact — the agent's schema
    context, for instance — do not have to construct the service (which builds a
    ConnectionService) to ask.

    APScheduler's store is the application database, not process memory, so this
    is the same answer in every worker and replica. That matters: it is what the
    settings UI shows, and the agent quoting a different number than the screen
    the admin is looking at would be worse than quoting none.
    """
    try:
        job = scheduler.get_job(job_id_for(cq_id))
        return getattr(job, "next_run_time", None) if job else None
    except Exception:
        return None


class CustomQueryService:
    def __init__(self):
        from app.services.connection_service import ConnectionService

        self.connection_service = ConnectionService()

    # -- guards ------------------------------------------------------------

    @staticmethod
    async def ensure_enabled(db: AsyncSession, organization) -> None:
        """Beta gate. Off by default — an org opts in from settings."""
        try:
            settings_obj = await organization.get_settings(db)
            cfg = settings_obj.get_config("enable_custom_queries") if settings_obj else None
            on = bool(getattr(cfg, "value", False))
        except Exception:
            on = False
        if not on:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Custom queries are in beta and disabled for this organization. "
                    "An admin can enable them in Settings."
                ),
            )

    @staticmethod
    def ensure_rls_licensed() -> None:
        """Enterprise gate on AUTHORING row policies — not on enforcing them.

        Query acceleration (custom queries) is a community feature; row-level
        security on top of it is enterprise. The asymmetry is deliberate:
        enabling, editing or previewing a policy requires the license, but a
        policy that is already saved keeps filtering even if the license
        lapses — an expired license must fail closed, never widen anyone's
        row visibility. Disabling a policy is likewise allowed without a
        license, so a lapsed org is not stuck with a filter it can no longer
        administer.
        """
        if not has_feature("rls"):
            raise HTTPException(
                status_code=402,
                detail=(
                    "Row-level security requires an enterprise license. "
                    "Set BOW_LICENSE_KEY to enable."
                ),
            )

    @staticmethod
    async def _org_timezone(db: AsyncSession, organization) -> str:
        """IANA timezone the org schedules in, mirroring scheduled prompts.

        A "daily at 03:00" the admin set should fire at 03:00 their time, not
        the server's — the same expectation every other schedule in the product
        already meets.
        """
        try:
            settings_obj = await organization.get_settings(db)
            cfg = getattr(settings_obj, "config", None) or {}
            tz = cfg.get("timezone") if isinstance(cfg, dict) else None
            return tz or "UTC"
        except Exception:
            return "UTC"

    @staticmethod
    def ensure_accelerable(connection: Connection) -> None:
        if not is_accelerable_type(connection.type):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Custom queries are not available for '{connection.type}' "
                    f"connections yet."
                ),
            )
        if getattr(connection, "auth_policy", "system_only") != "system_only":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Custom queries require a connection with shared (system) "
                    "credentials. On a per-user connection, one materialized copy "
                    "cannot represent each user's row visibility — row-level "
                    "security support is planned."
                ),
            )

    @staticmethod
    def validate_name(name: str) -> str:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Name must start with a letter or underscore and contain only "
                    "letters, numbers and underscores (max 63 chars)."
                ),
            )
        return name

    async def _ensure_name_free(
        self, db: AsyncSession, connection_id: str, name: str, exclude_id: str = None
    ) -> None:
        """Uniqueness is enforced here rather than by a DB constraint — existing
        installs may already hold duplicate introspected rows.

        Case-INSENSITIVELY, which an exact match is not enough for. A custom
        query named `album` alongside a source table `Album` is two relations
        the agent cannot tell apart: it sees both in its schema context under
        names that differ only in case, and every by-name lookup around them
        (usage stats, the cached-relation badge) folds case, so one relation
        starts wearing the other's numbers. Rejecting the name is the only
        place that ambiguity can be stopped cheaply.
        """
        q = select(ConnectionTable).where(
            ConnectionTable.connection_id == connection_id,
            func.lower(ConnectionTable.name) == (name or "").lower(),
            ConnectionTable.deleted_at.is_(None),
        )
        rows = (await db.execute(q)).scalars().all()
        for r in rows:
            if exclude_id and r.id == exclude_id:
                continue
            what = "a table" if r.kind == KIND_TABLE else "another custom query"
            clash = (
                f"'{name}' already exists on this connection ({what})."
                if r.name == name
                else f"'{r.name}' already exists on this connection ({what}), and "
                     f"names that differ only in capitalisation are treated as the same."
            )
            raise HTTPException(status_code=409, detail=clash)

    # -- read --------------------------------------------------------------

    async def list_custom_queries(
        self, db: AsyncSession, connection_id: str
    ) -> list[ConnectionTable]:
        q = (
            select(ConnectionTable)
            .where(
                ConnectionTable.connection_id == connection_id,
                ConnectionTable.kind == KIND_BOW,
                ConnectionTable.deleted_at.is_(None),
            )
            .order_by(ConnectionTable.created_at.desc())
        )
        return list((await db.execute(q)).scalars().all())

    async def count_custom_queries(self, db: AsyncSession, connection_id: str) -> int:
        return len(await self.list_custom_queries(db, connection_id))

    async def get_custom_query(
        self, db: AsyncSession, connection_id: str, cq_id: str
    ) -> ConnectionTable:
        q = select(ConnectionTable).where(
            ConnectionTable.id == cq_id,
            ConnectionTable.connection_id == connection_id,
            ConnectionTable.kind == KIND_BOW,
            ConnectionTable.deleted_at.is_(None),
        )
        row = (await db.execute(q)).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Custom query not found")
        return row

    # -- authoring ---------------------------------------------------------

    async def preview(
        self,
        db: AsyncSession,
        connection: Connection,
        definition_sql: str,
        current_user: User = None,
    ) -> dict:
        """Run the admin's SQL bounded to 100 rows and return columns + rows.

        Also returns the source's own estimate for the *unbounded* query, so the
        modal can show what materializing it would actually cost.
        """
        self.ensure_accelerable(connection)
        if not (definition_sql or "").strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")

        client = await self.connection_service.construct_client(
            db, connection, current_user
        )

        def _run():
            est = extractor.estimate(client, definition_sql)
            cols, rows = extractor.preview(
                client, definition_sql, extractor.PREVIEW_ROW_LIMIT
            )
            return est, cols, rows

        try:
            est, cols, rows = await asyncio.to_thread(_run)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Query failed: {e}")

        refused = None
        try:
            extractor.check_budget(est)
        except extractor.ExtractionRefused as e:
            refused = str(e)

        return {
            "columns": cols,
            "rows": [[_jsonable(v) for v in r] for r in rows],
            "row_limit": extractor.PREVIEW_ROW_LIMIT,
            "truncated": len(rows) >= extractor.PREVIEW_ROW_LIMIT,
            "estimated_rows": est.rows,
            "estimated_bytes": est.total_bytes,
            "scan_bytes": est.scan_bytes,
            "estimate_supported": est.supported,
            "estimate_note": est.note,
            "budget_error": refused,
        }

    async def create(
        self,
        db: AsyncSession,
        connection: Connection,
        *,
        name: str,
        definition_sql: str,
        description: str = None,
        refresh_schedule_mode: str = "interval",
        refresh_interval_minutes: int = 60,
        refresh_at_time: str = None,
        current_user: User = None,
        organization=None,
        activate_for_datasource_id: str = None,
    ) -> ConnectionTable:
        self.ensure_accelerable(connection)
        name = self.validate_name(name)
        await self._ensure_name_free(db, connection.id, name)

        if not (definition_sql or "").strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")

        cq = ConnectionTable(
            name=name,
            connection_id=connection.id,
            kind=KIND_BOW,
            definition_sql=definition_sql,
            description=description,
            refresh_schedule_mode=refresh_schedule_mode,
            refresh_interval_minutes=refresh_interval_minutes,
            refresh_at_time=refresh_at_time,
            columns=[],
            pks=[],
            fks=[],
            no_rows=0,
            last_refresh_status="pending",
        )
        db.add(cq)
        await db.commit()
        await db.refresh(cq)

        # Materialize immediately so the relation is usable without waiting for
        # the first scheduled tick.
        await self.refresh(db, connection, cq, current_user=current_user)
        await self._activate_for_agents(
            db, connection, cq, activate_for_datasource_id=activate_for_datasource_id
        )
        self._schedule(connection.id, cq, timezone=await self._org_timezone(db, organization))
        return cq

    async def _activate_for_agents(
        self, db: AsyncSession, connection: Connection, cq: ConnectionTable,
        activate_for_datasource_id: str = None,
    ) -> None:
        """Create a per-agent row for the relation, ACTIVE only where asked.

        Same model as regular tables: every agent gets a row, activation is the
        admin's per-agent choice. Only the agent the query was created from is
        switched on — enabling it everywhere would silently widen what other
        agents can query, and a relation nobody asked for still costs context.
        """
        existing = (
            await db.execute(
                select(DataSourceTable).where(
                    DataSourceTable.connection_table_id == str(cq.id)
                )
            )
        ).scalars().all()
        have = {str(r.datasource_id) for r in existing}

        for ds in (connection.data_sources or []):
            if str(ds.id) in have:
                continue
            db.add(
                DataSourceTable(
                    name=cq.name,
                    datasource_id=str(ds.id),
                    connection_table_id=str(cq.id),
                    is_active=(
                        activate_for_datasource_id is not None
                        and str(ds.id) == str(activate_for_datasource_id)
                    ),
                    columns=cq.columns or [],
                    pks=[],
                    fks=[],
                    no_rows=cq.no_rows or 0,
                )
            )
        await db.commit()

    async def update(
        self,
        db: AsyncSession,
        connection: Connection,
        cq: ConnectionTable,
        *,
        name: str = None,
        definition_sql: str = None,
        description: str = None,
        refresh_schedule_mode: str = None,
        refresh_interval_minutes: int = None,
        refresh_at_time: str = None,
        current_user: User = None,
        organization_timezone: str = "UTC",
    ) -> ConnectionTable:
        sql_changed = False
        if name is not None and name != cq.name:
            name = self.validate_name(name)
            await self._ensure_name_free(db, connection.id, name, exclude_id=cq.id)
            cq.name = name
            sql_changed = True
        if definition_sql is not None and definition_sql != cq.definition_sql:
            cq.definition_sql = definition_sql
            sql_changed = True
        if description is not None:
            cq.description = description
        if refresh_schedule_mode is not None:
            cq.refresh_schedule_mode = refresh_schedule_mode
        if refresh_interval_minutes is not None:
            cq.refresh_interval_minutes = refresh_interval_minutes
        if refresh_at_time is not None:
            cq.refresh_at_time = refresh_at_time

        await db.commit()
        await db.refresh(cq)

        if sql_changed:
            await self.refresh(db, connection, cq, current_user=current_user)
        self._schedule(connection.id, cq, timezone=organization_timezone or "UTC")
        return cq

    async def delete(
        self, db: AsyncSession, connection: Connection, cq: ConnectionTable
    ) -> dict:
        """Soft-delete the relation, drop its agent activations, remove the file."""
        self._unschedule(cq.id)

        # Agent activations must go too, or the relation lingers in contexts.
        rows = (
            await db.execute(
                select(DataSourceTable).where(
                    DataSourceTable.connection_table_id == cq.id
                )
            )
        ).scalars().all()
        for r in rows:
            await db.delete(r)

        artifacts.delete_artifact(cq.artifact_path)
        name = cq.name
        cq.deleted_at = datetime.utcnow()
        cq.artifact_path = None
        cq.artifact_key_enc = None
        await db.commit()
        return {"success": True, "message": f"Custom query '{name}' deleted"}

    # -- refresh -----------------------------------------------------------

    async def refresh(
        self,
        db: AsyncSession,
        connection: Connection,
        cq: ConnectionTable,
        current_user: User = None,
    ) -> ConnectionTable:
        """Rebuild the artifact. A failure keeps the previous one serving."""
        async with _lock_for(connection.id):
            cq.last_refresh_status = "running"
            await db.commit()

            client = await self.connection_service.construct_client(
                db, connection, current_user
            )
            old_path = cq.artifact_path

            def _run():
                est = extractor.estimate(client, cq.definition_sql)
                extractor.check_budget(est)
                return extractor.extract_to_artifact(
                    client, cq.definition_sql, cq.name, connection.id
                )

            try:
                res = await asyncio.to_thread(_run)
            except Exception as e:
                cq.last_refresh_status = "error"
                cq.last_refresh_error = str(e)[:2000]
                await db.commit()
                logger.error(
                    "custom_query.refresh.failed",
                    extra={"custom_query": cq.name, "error": str(e)},
                )
                raise HTTPException(status_code=400, detail=str(e))

            cq.artifact_path = res.artifact_path
            cq.artifact_key_enc = artifacts.encrypt_key(res.artifact_key)
            cq.artifact_bytes = res.artifact_bytes
            cq.columns = res.columns
            cq.no_rows = res.row_count
            cq.last_refreshed_at = datetime.utcnow()
            cq.last_refresh_status = "ok"
            cq.last_refresh_error = None
            cq.last_refresh_ms = res.elapsed_ms
            await db.commit()
            await db.refresh(cq)

            # DataSourceTable carries legacy copies of name/columns/no_rows that
            # the schema context reads; leaving them stale would show agents the
            # previous shape of the relation.
            await self._sync_activations(db, cq)

            # Only once the swap is committed is the old file safe to remove.
            if old_path and old_path != res.artifact_path:
                artifacts.delete_artifact(old_path)

            logger.info(
                "custom_query.refresh.ok",
                extra={
                    "custom_query": cq.name,
                    "rows": res.row_count,
                    "ms": res.elapsed_ms,
                },
            )
            return cq

    async def _sync_activations(self, db: AsyncSession, cq: ConnectionTable) -> None:
        rows = (
            await db.execute(
                select(DataSourceTable).where(
                    DataSourceTable.connection_table_id == str(cq.id)
                )
            )
        ).scalars().all()
        for r in rows:
            r.name = cq.name
            r.columns = cq.columns or []
            r.no_rows = cq.no_rows or 0
        if rows:
            await db.commit()

    async def scheduled_refresh(self, connection_id: str, cq_id: str) -> None:
        """APScheduler entry point. Owns its own session."""
        from app.dependencies import async_session_maker

        async with async_session_maker() as db:
            conn = (
                await db.execute(
                    select(Connection).where(Connection.id == connection_id)
                )
            ).scalar_one_or_none()
            cq = (
                await db.execute(
                    select(ConnectionTable).where(
                        ConnectionTable.id == cq_id,
                        ConnectionTable.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if not conn or not cq:
                self._unschedule(cq_id)
                return
            try:
                await self.refresh(db, conn, cq)
            except Exception as e:
                logger.error(
                    "custom_query.scheduled_refresh.failed",
                    extra={"custom_query": cq_id, "error": str(e)},
                )

    def _job_id(self, cq_id: str) -> str:
        return job_id_for(cq_id)

    def _schedule(self, connection_id: str, cq: ConnectionTable, timezone: str = "UTC") -> None:
        self._unschedule(cq.id)
        try:
            if cq.refresh_schedule_mode == "time" and cq.refresh_at_time:
                hh, mm = cq.refresh_at_time.split(":")
                scheduler.add_job(
                    func=self.scheduled_refresh,
                    trigger="cron",
                    hour=int(hh),
                    minute=int(mm),
                    # "Daily at 03:00" means 03:00 where the admin lives.
                    timezone=timezone or "UTC",
                    id=self._job_id(cq.id),
                    args=[connection_id, cq.id],
                    replace_existing=True,
                    jitter=300,
                )
            else:
                minutes = int(cq.refresh_interval_minutes or 60)
                scheduler.add_job(
                    func=self.scheduled_refresh,
                    trigger="interval",
                    minutes=max(1, minutes),
                    id=self._job_id(cq.id),
                    args=[connection_id, cq.id],
                    replace_existing=True,
                    # Jitter so replicas don't stampede the source on the same tick.
                    jitter=min(300, max(1, minutes) * 6),
                )
        except Exception as e:
            logger.error(
                "custom_query.schedule_failed",
                extra={"custom_query": cq.id, "error": str(e)},
            )

    def next_run_at(self, cq_id: str):
        """When the next scheduled refresh fires."""
        return next_run_at(cq_id)

    def _unschedule(self, cq_id: str) -> None:
        try:
            scheduler.remove_job(job_id=self._job_id(cq_id))
        except (JobLookupError, Exception):
            pass

    # -- serving -----------------------------------------------------------

    @staticmethod
    def build_fast_client(
        rows: list[ConnectionTable],
        connection_name: str = "",
        identity: Optional[rls.Identity] = None,
    ) -> Optional[FastQueryClient]:
        """Assemble a FastQueryClient from ACTIVATED custom queries.

        Two authorization boundaries meet here, and they are different things.

        *Which relations* the agent may name is the caller's filtering: a
        relation absent from `rows` cannot be named in DuckDB at all.

        *Which rows* it sees is `identity`. It is a required argument in
        practice — passing None means ANONYMOUS, and ANONYMOUS gets nothing
        from any relation with `rls_enabled`. That has to be the default,
        because the alternative (None means trusted) turns every background
        path that forgot to thread a user into a full-table leak.
        """
        who = identity or rls.ANONYMOUS
        relations = []
        for r in rows:
            if not r.artifact_path or not r.artifact_key_enc:
                continue
            try:
                key = artifacts.decrypt_key(r.artifact_key_enc)
            except Exception as e:
                logger.error(
                    "custom_query.key_decrypt_failed",
                    extra={"custom_query": r.name, "error": str(e)},
                )
                continue
            relations.append(
                FastRelation(
                    name=r.name,
                    artifact_path=r.artifact_path,
                    artifact_key=key,
                    columns=r.columns or [],
                    as_of=r.last_refreshed_at.isoformat(timespec="minutes")
                    if r.last_refreshed_at
                    else None,
                    row_count=r.no_rows or 0,
                    description=r.description,
                    rls_filter=CustomQueryService.compile_rls(r, who),
                )
            )
        if not relations:
            return None
        return FastQueryClient(relations, connection_name=connection_name)

    # -- row-level security ----------------------------------------------

    async def set_rls(
        self,
        db: AsyncSession,
        cq: ConnectionTable,
        *,
        rls_enabled: bool,
        rls_mode: str = rls.MODE_ATTRIBUTE,
        rls_policy: Optional[dict] = None,
        rls_default_deny: bool = True,
    ) -> ConnectionTable:
        """Store a row policy, rejecting one that cannot mean what it says.

        Save-time validation is not the enforcement boundary — the relation's
        columns can change under a saved policy, and `compile_policy` denies in
        that case. It exists so an admin learns about a typo when they press
        Save rather than by wondering why a report came back empty.
        """
        if rls_enabled:
            # Enterprise-only to enable or edit; turning OFF stays open so a
            # lapsed license never traps an org behind its own policy.
            self.ensure_rls_licensed()
            columns = [
                c.get("name") for c in (cq.columns or []) if isinstance(c, dict)
            ]
            try:
                rls.validate_policy(rls_policy, rls_mode, columns)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        cq.rls_enabled = bool(rls_enabled)
        cq.rls_mode = rls_mode if rls_enabled else None
        cq.rls_policy = rls_policy if rls_enabled else None
        cq.rls_default_deny = bool(rls_default_deny)
        await db.commit()
        await db.refresh(cq)
        return cq

    async def preview_as_user(
        self,
        db: AsyncSession,
        organization,
        cq: ConnectionTable,
        target_user_id: str,
        *,
        overrides: Optional[dict] = None,
        limit: int = 50,
    ) -> dict:
        """Show the rows `target_user_id` would get, and why.

        The single best safeguard against a policy that reads correctly and
        behaves wrongly: an admin can see one specific person's slice before
        anyone depends on it. `overrides` lets that happen against an *unsaved*
        policy, so the check comes before the commit rather than after.

        This reads the materialized artifact through exactly the same
        `FastQueryClient` path an agent uses. A separate "simulation" that
        applied the filter differently would be able to disagree with reality,
        which is the one thing a preview must not do.
        """
        self.ensure_rls_licensed()

        from app.models.user import User as UserModel
        from app.services.rls_identity_service import resolve_identity

        target = (await db.execute(
            select(UserModel).where(UserModel.id == str(target_user_id))
        )).scalars().first()
        if target is None:
            raise HTTPException(status_code=404, detail="No such member")

        if not cq.artifact_path:
            raise HTTPException(
                status_code=400,
                detail="This query has not been materialized yet — refresh it first.",
            )

        identity = await resolve_identity(db, target, str(organization.id))

        # Apply the unsaved policy to an in-memory copy, never to the row.
        probe = _RlsProbe(cq, overrides or {})
        rls_filter = self.compile_rls(probe, identity)

        client = self.build_fast_client([cq], connection_name="")
        if client is None:
            raise HTTPException(
                status_code=400, detail="This query has no readable artifact."
            )
        client.relations[0].rls_filter = rls_filter

        safe = cq.name.replace('"', '""')
        try:
            df = await asyncio.to_thread(
                client.execute_query, f'SELECT * FROM "{safe}" LIMIT {int(limit)}'
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Preview failed: {e}")

        f = rls_filter
        return {
            "columns": [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns],
            "rows": [
                [_jsonable(v) for v in row]
                for row in df.astype(object).where(df.notna(), None).values.tolist()
            ],
            "row_limit": limit,
            "filtered": bool(f is not None and not f.unrestricted),
            "denied": bool(f is not None and f.deny_all),
            "column": getattr(f, "column", None),
            "allowed_values": list(getattr(f, "allowed_values", None) or []),
            "reason": getattr(f, "reason", "") or "no policy",
        }

    @staticmethod
    def compile_rls(row: ConnectionTable, identity: rls.Identity) -> Optional[rls.Filter]:
        """The filter to apply to `row` for `identity`, or None if unprotected.

        Deliberately NOT license-checked: a saved policy is enforced whether or
        not the enterprise license is still active. Skipping the filter on a
        lapsed license would turn a billing event into a data leak.
        """
        if not getattr(row, "rls_enabled", False):
            return None
        return rls.compile_policy(
            row.rls_policy,
            identity,
            available_columns=[
                c.get("name") for c in (row.columns or []) if isinstance(c, dict)
            ],
            default_deny=bool(getattr(row, "rls_default_deny", True)),
            mode=getattr(row, "rls_mode", None) or rls.MODE_ATTRIBUTE,
        )


class _RlsProbe:
    """A stand-in that answers RLS questions from unsaved values.

    `preview_as_user` must be able to try a policy the admin has not committed.
    Mutating the ORM row and rolling back would work until something else in
    the session flushed first; a read-only shim cannot.
    """

    __slots__ = ("_row", "_over")

    def __init__(self, row, overrides: dict):
        self._row = row
        self._over = {k: v for k, v in (overrides or {}).items() if v is not None}

    def __getattr__(self, name):
        if name in self._over:
            return self._over[name]
        return getattr(self._row, name)


def _jsonable(v):
    """Preview rows go straight to JSON; Decimal/datetime/bytes need coercing."""
    import datetime as _dt
    import decimal

    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)


custom_query_service = CustomQueryService()
