"""Custom tables — admin-authored SQL materialized to encrypted local artifacts.

Ownership note: a custom table is a **connection-level** object even though the
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
import os
import re
import socket
import threading
import time
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from fastapi import HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scheduler import scheduler
from app.core.telemetry import telemetry
from app.data_sources.fast import artifacts, extractor, incremental, loader, rls
from app.data_sources.fast import events as table_events
from app.ee.license import has_feature
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, KIND_TABLE, ConnectionTable
from app.models.custom_table_run import MODE_FULL as RUN_MODE_FULL
from app.models.custom_table_run import MODE_INCREMENTAL as RUN_MODE_INCREMENTAL
from app.models.custom_table_run import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    TRIGGER_CREATE,
    TRIGGER_DEFINITION,
    TRIGGER_MANUAL,
    TRIGGER_RESUME,
    TRIGGER_SCHEDULE,
)
from app.services import custom_table_runs as runs
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
#
# PostHog graduated the same way, and the live run rewrote its design. Against
# a real project the query API rejected OFFSET outright for personal API keys —
# the only credential this connector has — so the paging loop it was built with
# could not have worked at all, and the 50,000-row response ceiling truncates
# without a word. Extraction is windowed and count-verified instead
# (fast/posthog_source.py); 181,086 events materialized in 13 requests, matching
# the project's own count and per-event totals exactly.
#
# PostHog is also the connector this feature helps most. Every agent query today
# spends a rate-limited API call to return at most a few hundred rows;
# accelerated, an admin's HogQL query is materialized once per schedule and the
# agent gets unrestricted local SQL over the result.
#
# Power BI is the third connector with a native source (fast/powerbi_source.py),
# and the one where the design came entirely from a live tenant: the
# executeQueries endpoint truncates at 100,000 rows or 1,000,000 values with
# HTTP 200 and no flag, and DAX's WINDOW refuses to page a base table ("may have
# duplicate rows"), so extraction windows over a numeric or date column and is
# verified against COUNTROWS. A custom table there is a DAX query, and the
# semantic model it targets is resolved from the tables it references (or
# pinned explicitly); agents then get plain SQL over the local copy instead of
# writing DAX against a rate-limited API.
#
# ClickHouse has a native source (fast/clickhouse_source.py) and was verified
# against a live 24.8 server holding 20M rows. The run found two things a fake
# would not have: an abandoned Arrow stream keeps the client's HTTP session
# locked, and Arrow carries Enum columns as bare integer codes.
VERIFIED_TYPES = {"postgresql", "mariadb", "mysql", "sqlite", "snowflake",
                  "bigquery", "mssql", "oracledb", "posthog", "powerbi",
                  "clickhouse"}
UNVERIFIED_TYPES = {"ms_fabric", "sybase"}
# Connectors whose query is a JSON request with a time window rather than SQL
# (fast/sources.py RequestSource). A client opts in by declaring
# INCREMENTAL_WINDOW. Aria Operations was run against the repo's mock Suite
# API (tools/aria_operations), not a live appliance.
#
# Kept apart from ACCELERABLE_TYPES on purpose: that list promises things only
# a query language can give — a cost estimate before running, a table preview
# to start a custom table from — and code and tests rely on that promise.
REQUEST_TYPES = {"aria_operations"}
ACCELERABLE_TYPES = VERIFIED_TYPES | UNVERIFIED_TYPES


def supports_incremental(conn_type) -> bool:
    """Every cacheable connector can refresh incrementally. Whether it can
    also *filter at the source* is the client's to say
    (`incremental.filters_at_source`); one that cannot still keeps its table
    current the same way, it just runs its whole query to do so."""
    return is_accelerable_type(conn_type)


_QUERYABLE: dict = {}


def _is_queryable(kind: str) -> bool:
    """Whether a connection type's client can run a query at all."""
    if kind not in _QUERYABLE:
        try:
            from app.data_sources.clients.base import Capability
            from app.schemas.data_source_registry import REGISTRY, resolve_client_class

            key = next((k for k in REGISTRY if k.lower() == kind), None)
            cls = resolve_client_class(key) if key else None
            _QUERYABLE[kind] = bool(
                cls is not None
                and Capability.QUERY in (getattr(cls, "capabilities", None) or ())
                and callable(getattr(cls, "execute_query", None))
            )
        except Exception:
            _QUERYABLE[kind] = False
    return _QUERYABLE[kind]


def is_accelerable_type(conn_type) -> bool:
    """Whether a stored `Connection.type` is offered acceleration.

    Any connector that can run a query: the ones in ACCELERABLE_TYPES through
    a source built for them, the rest through the connector's own
    `execute_query` (fast/sources.py QuerySource).

    Case-insensitive because the registry is not: SQL Server is registered as
    "MSSQL" — the registry's one uppercase key — so a literal membership test
    against these lowercase sets silently excluded every SQL Server connection.
    """
    kind = (conn_type or "").lower()
    return kind in ACCELERABLE_TYPES or kind in REQUEST_TYPES or _is_queryable(kind)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def job_id_for(cq_id: str) -> str:
    return f"custom_query_{cq_id}"


# Recorded as last_refresh_error when a request is cancelled mid-extraction, so
# the error badge carries a reason and a cancelled refresh is distinguishable
# from a source failure in history. Phrased like the extractor's own abort
# messages, which are stored the same way.
CANCELLED_REFRESH_ERROR = (
    "Refresh was cancelled before it completed; the previous data is still being served."
)


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


# How long one run keeps loading windows before it pauses and hands the rest
# to the next run. Each window still has the extractor's own ceiling.
LOAD_RUN_BUDGET_SECONDS = extractor.DEFAULT_MAX_SECONDS
# Runs in a row a load may fail before it is abandoned: restarted from scratch
# for a first load, or — when a table is already being served — set aside so
# incremental refreshes carry on while the rebuild waits for another day.
MAX_LOAD_FAILURES = 3
FULL_REFRESH_RETRY_AFTER = timedelta(days=1)
# A load whose part file is on another host is left to that host for this
# long after its last sign of progress, then started over here.
LOAD_OWNER_GRACE = timedelta(minutes=45)


class _SourceError(Exception):
    """Something the source (or reading from it) raised, to be shown to the
    admin as the refresh's error."""


# Raised by our own code with a message already written for the admin, or a
# signal the orchestration acts on: never re-wrapped as a source error.
_PASS_THROUGH = (extractor.ExtractionRefused, extractor.ExtractionAborted,
                 incremental.IncrementalConfigError, loader.SchemaChanged)


def _parse_iso(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _comparable(a, b) -> bool:
    try:
        a < b  # noqa: B015
        return True
    except TypeError:
        return False


class _Run:
    """One refresh in flight: what it was asked, what it found, what it did.

    Worker threads read the plain values captured by `snapshot()`; they never
    touch the ORM row.
    """

    def __init__(self, *, db, connection, cq, trigger, force_full, user_id, current_user):
        self.db, self.connection, self.cq = db, connection, cq
        self.trigger, self.force_full = trigger, force_full
        self.user_id, self.current_user = user_id, current_user
        self.run_id: Optional[str] = loader.new_run_id()
        self.abort = threading.Event()
        self.started = time.monotonic()
        self.client = None
        self.load: Optional[dict] = None
        self.rows_read = 0
        self.windows_done = 0
        self.rendered_query: Optional[str] = None
        self.window_from = None
        # Set once the swap is committed and recorded: nothing that fails
        # afterwards may rewrite the run as failed.
        self.published = False
        # Whether the row was marked "running", and whether a cancellation has
        # already been written back.
        self.started_work = False
        self.cancel_recorded = False
        self._events: list = []

    def snapshot(self) -> None:
        cq = self.cq
        self.table_id = str(cq.id)
        self.connection_id = str(self.connection.id)
        self.name = cq.name
        self.sql = cq.definition_sql or ""
        self.config = incremental.RefreshConfig.from_row(cq)
        self.state = dict(cq.refresh_state) if isinstance(cq.refresh_state, dict) else {}
        self.definition_hash = incremental.definition_hash(
            self.sql, CustomQueryService.target_of(cq), self.config)
        self.previous_path = cq.artifact_path
        self.previous_key = None
        if cq.artifact_path and cq.artifact_key_enc:
            with suppress(Exception):
                self.previous_key = artifacts.decrypt_key(cq.artifact_key_enc)
        self.previous_rows = cq.no_rows if cq.artifact_path else None
        # We write the window predicate ourselves unless the admin placed it
        # (placeholders) or the source takes it as request fields.
        self.at_source = incremental.filters_at_source(self.client)
        from app.data_sources.fast.sources import QuerySource, source_for

        # Read through the connector's plain `execute_query`: no source built
        # for it, so whatever row limit that call applies, applies here.
        self.generic = isinstance(source_for(self.client), QuerySource)
        self.wrapped = (
            self.config.incremental
            and self.at_source
            and not incremental.uses_placeholders(self.sql)
            and incremental.json_window_of(self.client) is None
        )
        checkpoint = incremental.decode(self.state.get("checkpoint"))
        self.cursor_kind = self.config.cursor_kind or incremental.kind_of(checkpoint)

    def note(self, message: str, level: str = "info") -> None:
        self._events.append(runs.event(message, level))

    def take_events(self) -> list:
        events, self._events = self._events, []
        return events

    def chunk_log(self) -> Optional[list]:
        if not self.load:
            return None
        return [
            {"lo": w.get("lo"), "hi": w.get("hi"), "rows": w.get("rows"), "status": w.get("status")}
            for w in self.load.get("windows") or []
        ]

    def over_budget(self) -> bool:
        return time.monotonic() - self.started > LOAD_RUN_BUDGET_SECONDS

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


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
                    "Custom tables are in beta and disabled for this organization. "
                    "An admin can enable them in Settings."
                ),
            )

    @staticmethod
    def ensure_rls_licensed() -> None:
        """Enterprise gate on AUTHORING row policies — not on enforcing them.

        Query acceleration (custom tables) is a community feature; row-level
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
                    "Add a license key in Settings → License (or set BOW_LICENSE_KEY)."
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
                    f"Custom tables are not available for '{connection.type}' "
                    f"connections yet."
                ),
            )
        if getattr(connection, "auth_policy", "system_only") != "system_only":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Custom tables require a connection with shared (system) "
                    "credentials. On a per-user connection, one materialized copy "
                    "cannot represent each user's row visibility — row-level "
                    "security support is planned."
                ),
            )

    # -- extraction targets -------------------------------------------------
    #
    # Most sources need nothing beyond the query text: the connection IS the
    # target. Power BI is addressed per semantic model, and a DAX text only
    # names tables, so the target is either resolved from those references
    # against the connection's catalog (fast/powerbi_source.py) or pinned by
    # the admin. The pin lives in `metadata_json["target"]`, a generic slot so
    # a future per-target connector does not need a schema change.

    @staticmethod
    def target_of(cq: ConnectionTable) -> Optional[dict]:
        meta = cq.metadata_json if isinstance(cq.metadata_json, dict) else {}
        t = meta.get("target")
        return dict(t) if isinstance(t, dict) and t else None

    @staticmethod
    def _metadata_with_target(existing, target: Optional[dict]) -> dict:
        meta = dict(existing) if isinstance(existing, dict) else {}
        clean = {
            k: v for k, v in (target or {}).items()
            if k in ("datasetId", "workspaceId", "datasetName") and v not in (None, "")
        }
        if clean.get("datasetId"):
            meta["target"] = clean
        else:
            meta.pop("target", None)
        return meta

    @staticmethod
    def _needs_catalog(client) -> bool:
        return hasattr(client, "attach_table_metadata") and hasattr(client, "execute_dax_rows")

    async def _prepare_client(
        self, db: AsyncSession, connection: Connection, client, target: Optional[dict]
    ) -> None:
        """Hand a per-target client what it needs to address the source.

        For Power BI that is the connection's indexed catalog (so the DAX's
        table references resolve to a semantic model without a tenant crawl)
        and the admin's explicit pin, when there is one. Everything else is
        left alone.
        """
        if not self._needs_catalog(client):
            return
        rows = (
            await db.execute(
                select(ConnectionTable.name, ConnectionTable.metadata_json).where(
                    ConnectionTable.connection_id == str(connection.id),
                    ConnectionTable.kind == KIND_TABLE,
                    ConnectionTable.deleted_at.is_(None),
                )
            )
        ).all()
        client.attach_table_metadata(
            [{"name": name, "metadata_json": meta} for name, meta in rows]
        )
        client.extraction_target = dict(target) if target else None

    async def list_targets(self, db: AsyncSession, connection: Connection) -> list:
        """The semantic models a custom table on this connection can target.

        Drawn from the indexed catalog rather than a live tenant crawl: it is
        what the admin can already see on the tables page, and it costs no
        rate-limited request. Empty for every connector that is not addressed
        per target.
        """
        if (connection.type or "").lower() != "powerbi":
            return []
        rows = (
            await db.execute(
                select(ConnectionTable.name, ConnectionTable.metadata_json).where(
                    ConnectionTable.connection_id == str(connection.id),
                    ConnectionTable.kind == KIND_TABLE,
                    ConnectionTable.deleted_at.is_(None),
                )
            )
        ).all()
        models: dict = {}
        for name, meta in rows:
            pbi = (meta or {}).get("powerbi") if isinstance(meta, dict) else None
            if not isinstance(pbi, dict) or not pbi.get("datasetId"):
                continue
            ds_id = str(pbi["datasetId"])
            m = models.setdefault(ds_id, {
                "datasetId": ds_id,
                "workspaceId": pbi.get("workspaceId"),
                "workspaceName": pbi.get("workspaceName"),
                "datasetName": pbi.get("datasetName") or name.split("/", 1)[0],
                "tables": [],
            })
            table = str(pbi.get("tableName") or name.split("/", 1)[-1]).strip()
            if table and table not in m["tables"]:
                m["tables"].append(table)
        for m in models.values():
            m["tables"].sort(key=str.lower)
        return sorted(models.values(), key=lambda m: (m["datasetName"] or "").lower())

    @staticmethod
    def validate_name(name: str) -> str:
        name = (name or "").strip()
        if name.lower().startswith(loader.INTERNAL_PREFIX):
            raise HTTPException(
                status_code=400,
                detail=f"Names starting with '{loader.INTERNAL_PREFIX}' are reserved.",
            )
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
            what = "a table" if r.kind == KIND_TABLE else "another custom table"
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
            raise HTTPException(status_code=404, detail="Custom table not found")
        return row

    # -- authoring ---------------------------------------------------------

    async def preview(
        self,
        db: AsyncSession,
        connection: Connection,
        definition_sql: str,
        current_user: User = None,
        target: dict = None,
        refresh_config: dict = None,
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
        await self._prepare_client(db, connection, client, target)
        if incremental.uses_placeholders(definition_sql):
            # Preview what a full load would read: the window wide open.
            hint = incremental.RefreshConfig.from_dict(refresh_config or {})
            try:
                definition_sql = incremental.render(
                    client, definition_sql, hint.cursor_column or "",
                    hint.cursor_kind or incremental.KIND_TIMESTAMP, incremental.Window(),
                )
            except incremental.IncrementalConfigError as e:
                raise HTTPException(status_code=400, detail=str(e))

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
            "filters_at_source": incremental.filters_at_source(client),
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
        target: dict = None,
        refresh_mode: str = None,
        refresh_config: dict = None,
    ) -> ConnectionTable:
        self.ensure_accelerable(connection)
        name = self.validate_name(name)
        await self._ensure_name_free(db, connection.id, name)

        if not (definition_sql or "").strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        refresh_config = dict(refresh_config or {})
        # New tables get the row-drop guard unless the admin said otherwise
        # (an explicit null turns it off); ones that predate it keep
        # refreshing exactly as they did.
        refresh_config.setdefault("row_drop_guard_pct", incremental.DEFAULT_ROW_DROP_GUARD_PCT)
        config = self.refresh_settings(refresh_mode, refresh_config, definition_sql, connection.type)

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
            metadata_json=self._metadata_with_target({}, target),
            refresh_mode=config.mode,
            refresh_config=config.to_dict(),
        )
        db.add(cq)
        await db.commit()
        await db.refresh(cq)

        # Materialize immediately so the relation is usable without waiting for
        # the first scheduled tick.
        await self.refresh(db, connection, cq, current_user=current_user, trigger=TRIGGER_CREATE)
        if cq.deleted_at is not None:
            # Deleted while the first refresh ran: a job for a soft-deleted row
            # would fire until it noticed; activations would linger in contexts.
            return cq
        await self._activate_for_agents(
            db, connection, cq, activate_for_datasource_id=activate_for_datasource_id
        )
        self._schedule(connection.id, cq, timezone=await self._org_timezone(db, organization))

        try:
            await telemetry.capture(
                "custom_query_created",
                {
                    "connection_id": str(connection.id),
                    "connection_type": connection.type,
                    "refresh_schedule_mode": refresh_schedule_mode,
                    "refresh_interval_minutes": refresh_interval_minutes,
                    "refresh_mode": config.mode,
                    "has_description": bool((description or "").strip()),
                },
                user_id=current_user.id if current_user else None,
                org_id=str(organization.id) if organization else str(connection.organization_id),
            )
        except Exception:
            pass

        return cq

    @staticmethod
    def refresh_settings(refresh_mode, refresh_config, definition_sql: str,
                         conn_type: str = None) -> incremental.RefreshConfig:
        """Validated refresh settings from what the admin sent, or a 400."""
        raw = dict(refresh_config or {})
        raw["mode"] = refresh_mode or incremental.MODE_FULL
        config = incremental.RefreshConfig.from_dict(raw)
        try:
            config.validate(definition_sql or "")
        except incremental.IncrementalConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return config

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
        target: dict = ...,
        refresh_mode: str = None,
        refresh_config: dict = None,
    ) -> ConnectionTable:
        sql_changed = False
        before = incremental.RefreshConfig.from_row(cq)
        hash_before = incremental.definition_hash(cq.definition_sql or "", self.target_of(cq), before)
        if target is not ...:
            new_meta = self._metadata_with_target(cq.metadata_json, target)
            if new_meta != (cq.metadata_json or {}):
                cq.metadata_json = new_meta
                sql_changed = True
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
        if refresh_mode is not None or refresh_config is not None:
            merged = {**before.to_dict(), **(refresh_config or {})}
            config = self.refresh_settings(refresh_mode or before.mode, merged, cq.definition_sql,
                                           connection.type)
            cq.refresh_mode, cq.refresh_config = config.mode, config.to_dict()
        else:
            config = self.refresh_settings(before.mode, before.to_dict(), cq.definition_sql, connection.type)
        # Anything that decides which rows belong in the table: the stored
        # checkpoint was taken under the old definition, so rebuild now rather
        # than leave a table that mixes the two until the next schedule tick.
        if incremental.definition_hash(cq.definition_sql or "", self.target_of(cq), config) != hash_before:
            sql_changed = True

        await db.commit()
        await db.refresh(cq)

        if sql_changed:
            await self.refresh(db, connection, cq, current_user=current_user,
                               trigger=TRIGGER_DEFINITION)
        # Delete may have won while the refresh was queued: reload before
        # touching the shared job store (404 if the row is gone). The volume
        # lock is the wrong guard here, it does not reach replicas on another
        # volume; scheduled_refresh is the backstop and unschedules a deleted
        # table on its first tick.
        cq = await self.get_custom_query(db, connection.id, cq.id)
        self._schedule(connection.id, cq, timezone=organization_timezone or "UTC")
        return cq

    async def delete(
        self, db: AsyncSession, connection: Connection, cq: ConnectionTable
    ) -> dict:
        """Soft-delete the relation, drop its agent activations, remove the file.

        Takes no volume lock, so a delete never waits behind another table's
        refresh. A refresh racing this one cannot resurrect the row: its swap
        is a conditional UPDATE on ``deleted_at IS NULL`` and discards the new
        file when it loses.
        """
        await db.refresh(cq)
        self._unschedule(cq.id)
        self._unschedule_resume(cq.id)

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

        old_path = cq.artifact_path
        state = cq.refresh_state if isinstance(cq.refresh_state, dict) else {}
        part_path = (state.get("load") or {}).get("path") if isinstance(state.get("load"), dict) else None
        name = cq.name
        cq.deleted_at = datetime.utcnow()
        cq.artifact_path = None
        cq.artifact_key_enc = None
        cq.refresh_state = None
        await db.commit()
        artifacts.delete_artifact(old_path)
        # A first load still in progress notices the deletion at its next
        # window; if none is running, its file would otherwise wait for GC.
        with artifacts.try_refresh_lock(connection.id) as free:
            if free:
                loader.discard_part(part_path)
                artifacts.forget_table_lock(connection.id, cq.id)
        return {"success": True, "message": f"Custom table '{name}' deleted"}

    # -- refresh -----------------------------------------------------------
    #
    # A refresh is one of three shapes of work, chosen in `_decide`:
    #
    #   single     the whole query in one go (refresh_mode='full')
    #   increment  copy the served artifact forward and fold in the rows since
    #              the checkpoint
    #   load       an incremental table built from scratch, one cursor window
    #              at a time, into a file that survives between windows — the
    #              first load, and every full rebuild after it
    #
    # All three end in `_publish`, the only place the row's artifact, checkpoint
    # and status change, and they change together in one conditional UPDATE.
    #
    # Two locks. The table lock is held for the whole run. The connection's
    # volume lock is held around each unit of work that writes into the
    # artifact directory, and given up between the windows of a load so a
    # sibling table is not kept waiting behind a long first load.

    async def refresh(
        self,
        db: AsyncSession,
        connection: Connection,
        cq: ConnectionTable,
        current_user: User = None,
        *,
        trigger: str = TRIGGER_MANUAL,
        full: bool = False,
    ) -> ConnectionTable:
        """Bring the artifact up to date. A failure keeps the previous one serving.

        `full` rebuilds an incremental table from scratch instead of reading
        only what is new (it changes nothing for a table that is always full).
        """
        run = _Run(
            db=db, connection=connection, cq=cq, trigger=trigger, force_full=full,
            user_id=str(current_user.id) if current_user is not None else None,
            current_user=current_user,
        )
        async with artifacts.table_lock(connection.id, cq.id):
            try:
                await self._drive(run)
            except asyncio.CancelledError:
                # Cancelled between units of work (waiting for the volume,
                # saving progress): nothing is writing, but the row and the
                # run still say "running".
                if run.started_work and not run.cancel_recorded:
                    await self._drain(self._record_cancel(run))
                raise
            except (extractor.ExtractionRefused, extractor.ExtractionAborted,
                    incremental.IncrementalConfigError, loader.SchemaChanged) as e:
                await self._record_failure(run, e)
                raise HTTPException(status_code=400, detail=str(e))
            except _SourceError as e:
                await self._record_failure(run, e.__cause__ or e)
                raise HTTPException(status_code=400, detail=str(e.__cause__ or e))
            except BaseException as e:
                # Not the source: a storage failure around the swap. The row's
                # own state is whatever the failed transaction left, so only
                # the history is closed here and the error propagates as is.
                if not run.published:
                    await runs.finish(run.run_id, status=STATUS_FAILED, error=str(e),
                                      events=run.take_events())
                raise
        return cq

    @staticmethod
    async def _drain(coro) -> None:
        """Run cleanup to completion however many times the request is cancelled."""
        task = asyncio.create_task(coro)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()

    async def _record_cancel(self, run: "_Run") -> None:
        run.cancel_recorded = True
        try:
            await run.db.rollback()
            await run.db.refresh(run.cq)
            if run.cq.deleted_at is None:
                run.cq.last_refresh_status = "error"
                run.cq.last_refresh_error = CANCELLED_REFRESH_ERROR
                await run.db.commit()
        except Exception:
            logger.exception("custom_query.cancelled_status.failed")
        await runs.finish(run.run_id, status=STATUS_CANCELLED, error=CANCELLED_REFRESH_ERROR,
                          events=run.take_events(), chunks=run.chunk_log())

    async def _record_failure(self, run: "_Run", error: BaseException) -> None:
        db, cq = run.db, run.cq
        try:
            await db.rollback()
            await db.refresh(cq)
            if cq.deleted_at is None:
                cq.last_refresh_status = "error"
                cq.last_refresh_error = str(error)[:2000]
                state = cq.refresh_state if isinstance(cq.refresh_state, dict) else {}
                if run.load is not None and isinstance(state.get("load"), dict):
                    # Count it against the load, so one that cannot finish is
                    # not retried on every tick forever.
                    load = {**state["load"], "failures": int(state["load"].get("failures") or 0) + 1}
                    cq.refresh_state = {**state, "load": load}
                await db.commit()
        except Exception:
            logger.exception("custom_query.failure_status.failed")
        logger.error(
            "custom_query.refresh.failed",
            extra={"custom_query": cq.name, "error": str(error)},
        )
        await runs.finish(run.run_id, status=STATUS_FAILED, error=str(error),
                          events=run.take_events(), chunks=run.chunk_log())

    async def _in_thread(self, run: "_Run", work):
        """Run one unit of blocking work without letting go of the volume.

        A cancelled request must not release the volume lock while its thread
        is still writing into that directory. So on cancellation the work is
        told to stop at its next batch boundary and then *waited for*, however
        many more times the request is cancelled meanwhile, and only then does
        the cancellation continue upward.
        """
        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            run.abort.set()

            async def finish_cancelled():
                try:
                    result = await task
                    # It finished anyway: its file was never published.
                    artifacts.delete_artifact(getattr(result, "artifact_path", None))
                except Exception:
                    logger.info("custom_query.refresh.cancelled")
                finally:
                    await self._record_cancel(run)

            await self._drain(finish_cancelled())
            raise

    def _decide(self, run: "_Run") -> str:
        """Which shape of work this refresh is, with the reason logged."""
        config, state, cq = run.config, run.state, run.cq
        if not config.incremental:
            return "single"
        if not run.at_source:
            run.note("This connection cannot filter at the source: the whole query runs on "
                     "each refresh and what is new is taken from its result.")
        serving = bool(cq.artifact_path and cq.artifact_key_enc
                       and incremental.decode(state.get("checkpoint")) is not None
                       and state.get("definition_hash") == run.definition_hash)
        load = state.get("load")
        if isinstance(load, dict) and load.get("definition_hash") == run.definition_hash:
            failures = int(load.get("failures") or 0)
            if run.force_full:
                self._discard_load(run)
                run.note("Full refresh requested; the load in progress was restarted.")
                return "load"
            if failures >= MAX_LOAD_FAILURES:
                self._discard_load(run)
                if serving:
                    run.state["full_refresh_retry_after"] = (
                        datetime.utcnow() + FULL_REFRESH_RETRY_AFTER).isoformat(timespec="seconds")
                    run.note(f"The full rebuild failed {failures} times. Refreshing what is new "
                             f"instead, and trying the rebuild again tomorrow.", "warning")
                else:
                    run.note(f"The load failed {failures} times; starting it over.", "warning")
                    return "load"
            elif self._resumable_load(run) is not None:
                run.note("Continuing the load that was in progress.")
                return "load"
            elif self._load_owned_elsewhere(load):
                return "defer"
            else:
                self._discard_load(run)
        elif isinstance(load, dict):
            self._discard_load(run)         # left over from another definition
        if not cq.artifact_path or not cq.artifact_key_enc:
            run.note("First load.")
            return "load"
        if run.force_full:
            run.note("Full refresh requested.")
            return "load"
        if state.get("definition_hash") != run.definition_hash:
            run.note("The query or its refresh settings changed; rebuilding in full.")
            return "load"
        if incremental.decode(state.get("checkpoint")) is None:
            run.note("No checkpoint yet; rebuilding in full.")
            return "load"
        every = config.full_refresh_every_days
        last_full = _parse_iso(state.get("last_full_refresh_at"))
        retry_after = _parse_iso(run.state.get("full_refresh_retry_after"))
        waiting = retry_after is not None and datetime.utcnow() < retry_after
        # A source that cannot be asked for a range returns only what it has
        # now; rebuilding from that would throw away rows it no longer
        # returns. That stays the admin's call (Full refresh).
        if every and not waiting and run.at_source and (
                last_full is None or datetime.utcnow() - last_full >= timedelta(days=every)):
            run.note(f"Scheduled full refresh (every {every} days): picks up deleted rows.")
            return "load"
        return "increment"

    @staticmethod
    def _resumable_load(run: "_Run") -> Optional[dict]:
        """A paused load that this refresh can pick up, or None.

        Only on the volume it was started on: the part file is local, and
        another replica starts over rather than fetch a half-built file.
        """
        load = run.state.get("load")
        if not isinstance(load, dict) or load.get("definition_hash") != run.definition_hash:
            return None
        path = load.get("path")
        if not path or not os.path.isfile(path) or not load.get("windows"):
            return None
        return load

    @staticmethod
    def _load_owned_elsewhere(load: dict) -> bool:
        """Whether another host is (recently) working on this load.

        Its part file is not here, so resuming is that host's to do; taking
        it over would start the whole load again on every hand-off.
        """
        if not load.get("host") or load.get("host") == socket.gethostname():
            return False
        seen = _parse_iso(load.get("updated_at") or load.get("started_at"))
        return seen is not None and datetime.utcnow() - seen < LOAD_OWNER_GRACE

    def _discard_load(self, run: "_Run") -> None:
        load = run.state.pop("load", None)
        if isinstance(load, dict):
            loader.discard_part(load.get("path"))

    async def _save_state(self, run: "_Run") -> None:
        """Persist the load's progress on the row (never the checkpoint —
        that only moves with a published artifact)."""
        await run.db.execute(
            update(ConnectionTable)
            # By captured id: after a rollback the ORM row is expired, and
            # touching it here would mean a lazy load.
            .where(ConnectionTable.id == run.table_id, ConnectionTable.deleted_at.is_(None))
            .values(refresh_state=dict(run.state) or None)
            .execution_options(synchronize_session=False)
        )
        await run.db.commit()

    async def _drive(self, run: "_Run") -> None:
        db, connection, cq = run.db, run.connection, run.cq

        async with artifacts.refresh_lock(connection.id):
            # A queued caller may have loaded this row before another worker
            # committed its swap. Reload under the volume lock before reading
            # anything off it.
            await db.refresh(cq)
            if cq.deleted_at is not None:
                return
            run.client = await self.connection_service.construct_client(
                db, connection, run.current_user
            )
            await self._prepare_client(db, connection, run.client, self.target_of(cq))
            run.snapshot()
            if run.generic:
                run.note("Read with this connection's own query call, which applies the "
                         "connection's row limits; raise the limit in the query if it has one.")
            decision = self._decide(run)
            if decision == "defer":
                # Another replica holds this load's file; hand the turn back.
                self._schedule_resume(str(connection.id), str(cq.id), delay_seconds=30)
                return

            # Mark the row only once work actually starts: a request cancelled
            # while credentials were being fetched would otherwise leave
            # "running" committed with nothing to write a terminal state.
            cq.last_refresh_status = "running"
            await db.commit()
            run.started_work = True
            # The run keeps its own id even if the history row cannot be
            # written: rows are stamped with it either way.
            await runs.start(
                connection_table_id=str(cq.id), organization_id=str(connection.organization_id),
                trigger=run.trigger,
                mode=RUN_MODE_INCREMENTAL if decision == "increment" else RUN_MODE_FULL,
                triggered_by_user_id=run.user_id,
                inputs={"connection_id": str(connection.id)},
                run_id=run.run_id,
            )

            if decision == "single":
                outcome = await self._in_thread(run, lambda: self._work_single(run))
                await self._publish(run, outcome, full=True)
                return

            if decision == "increment":
                try:
                    outcome = await self._in_thread(run, lambda: self._work_increment(run))
                except loader.SchemaChanged as e:
                    # Stored rows cannot be given the new shape, so the copy
                    # would stop matching the source. Rebuild instead of
                    # failing every scheduled run until someone notices.
                    run.note(f"The query's columns changed ({e}); rebuilding in full.", "warning")
                    await runs.progress(run.run_id, mode=RUN_MODE_FULL, events=run.take_events())
                else:
                    await self._publish(run, outcome, full=False)
                    return

            load = self._resumable_load(run)
            if load is None:
                self._discard_load(run)
                load = await self._in_thread(run, lambda: self._work_plan_load(run))
                run.state["load"] = load
                await self._save_state(run)
            run.load = load
            await runs.progress(run.run_id, events=run.take_events(), chunks=run.chunk_log())

        # One window per hold of the volume lock.
        while True:
            index = next((i for i, w in enumerate(load["windows"]) if w.get("status") != "done"), None)
            if index is None:
                break
            # Always at least one window per run, so a load makes progress
            # however small the budget is.
            if run.windows_done and run.over_budget():
                await self._pause(run)
                return
            await self._yield_to_waiters(connection.id)
            async with artifacts.refresh_lock(connection.id):
                await db.refresh(cq)
                if cq.deleted_at is not None:
                    self._discard_load(run)
                    await runs.finish(run.run_id, status=STATUS_CANCELLED,
                                      error="The table was deleted while it was loading.",
                                      events=run.take_events(), chunks=run.chunk_log())
                    return
                rows, rendered = await self._in_thread(run, lambda i=index: self._work_window(run, i))
                window = load["windows"][index]
                window["status"], window["rows"] = "done", rows
                load["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
                load["failures"] = 0
                load["rows"] = int(load.get("rows") or 0) + rows
                run.rows_read += rows
                run.windows_done += 1
                run.rendered_query = run.rendered_query or rendered
                await self._save_state(run)
            await runs.progress(run.run_id, chunks=run.chunk_log(), rows_read=run.rows_read,
                                rendered_query=run.rendered_query, events=run.take_events())

        async with artifacts.refresh_lock(connection.id):
            await db.refresh(cq)
            if cq.deleted_at is not None:
                self._discard_load(run)
                await runs.finish(run.run_id, status=STATUS_CANCELLED,
                                  error="The table was deleted while it was loading.",
                                  events=run.take_events(), chunks=run.chunk_log())
                return
            try:
                outcome = await self._in_thread(run, lambda: self._work_finalize(run))
            except BaseException:
                # A file that failed its checks is not worth resuming into.
                run.state.pop("load", None)
                try:
                    await db.rollback()
                    await self._save_state(run)
                except Exception:
                    logger.exception("custom_query.load_state.clear_failed")
                raise
            # What this run read; a resumed load's earlier windows are on the
            # runs that loaded them.
            outcome.rows_read = run.rows_read
            outcome.rows_inserted = outcome.row_count
            await self._publish(run, outcome, full=True)

    async def _yield_to_waiters(self, connection_id: str) -> None:
        """Let a refresh queued for this connection's volume go first."""
        deadline = time.monotonic() + 60
        while artifacts.has_waiters(connection_id) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    async def _pause(self, run: "_Run") -> None:
        """Stop a long load between two windows; the next run continues it."""
        cq, load = run.cq, run.load
        done = sum(1 for w in load["windows"] if w.get("status") == "done")
        run.note(f"Paused after {done} of {len(load['windows'])} windows; it continues shortly.")
        cq.last_refresh_status = "partial"
        cq.last_refresh_error = None
        await run.db.commit()
        await runs.finish(run.run_id, status=STATUS_PARTIAL, events=run.take_events(),
                          chunks=run.chunk_log(), rows_read=run.rows_read,
                          rendered_query=run.rendered_query)
        self._schedule_resume(str(run.connection.id), str(cq.id))

    # -- units of work (each runs in a worker thread, off the ORM) ----------

    def _limits(self, run: "_Run") -> loader.Limits:
        return loader.Limits(
            max_rows=extractor.DEFAULT_MAX_ROWS, max_bytes=extractor.DEFAULT_MAX_BYTES,
            deadline=time.monotonic() + extractor.DEFAULT_MAX_SECONDS,
            should_abort=run.abort.is_set, max_seconds=extractor.DEFAULT_MAX_SECONDS,
        )

    def _work_single(self, run: "_Run"):
        try:
            est = extractor.estimate(run.client, run.sql)
            extractor.check_budget(est)
            run.rendered_query = run.sql
            return extractor.extract_to_artifact(
                run.client, run.sql, run.name, run.connection_id,
                should_abort=run.abort.is_set, run_id=run.run_id, config=run.config,
                previous_rows=run.previous_rows,
            )
        except _PASS_THROUGH:
            raise
        except Exception as e:
            raise _SourceError(str(e)) from e

    def _render(self, run: "_Run", window: incremental.Window) -> str:
        return incremental.render(run.client, run.sql, run.config.cursor_column, run.cursor_kind, window)

    def _source(self, run: "_Run", rendered: str):
        src = extractor._source(run.client, rendered)
        if src is None:
            raise incremental.IncrementalConfigError("This connection cannot run queries.")
        return src

    def _work_plan_load(self, run: "_Run") -> dict:
        """Size a load from scratch and cut it into windows. Moves no rows."""
        config = run.config
        try:
            start = incremental.parse_initial(run.cursor_kind, config.initial_value) \
                if config.initial_value else None
            whole = incremental.Window(lo=start, hi=None, include_nulls=run.wrapped and start is not None)
            rendered = self._render(run, whole)
            est = extractor.estimate(run.client, rendered)
            extractor.check_budget(est)

            windows = [incremental.Window(lo=start, hi=None, include_nulls=run.wrapped)]
            total = None
            sql_source = incremental.json_window_of(run.client) is None and \
                incremental.window_writer(run.client) is None
            probe = None if not sql_source else incremental.probe_sql(
                run.sql, incremental.dialect_for(run.client), config.cursor_column, run.cursor_kind, whole)
            if probe is not None:
                try:
                    _cols, rows = self._source(run, probe).preview(probe, 1)
                    lo, hi, total, with_cursor = rows[0][0], rows[0][1], int(rows[0][2] or 0), int(rows[0][3] or 0)
                except Exception as e:
                    # Sizing is an optimisation; a source that cannot answer
                    # it loads as one window.
                    run.note(f"Could not size the load up front ({str(e)[:200]}); loading as one window.",
                             "warning")
                else:
                    if total > extractor.DEFAULT_MAX_ROWS:
                        raise extractor.ExtractionRefused(
                            f"This query returns {total:,} rows, over the "
                            f"{extractor.DEFAULT_MAX_ROWS:,}-row limit for a custom table. "
                            f"Narrow it with a WHERE clause or a later start value."
                        )
                    if start is not None and incremental.kind_of(lo) == incremental.kind_of(start):
                        lo = max(lo, start) if _comparable(lo, start) else lo
                    windows = self._plan_windows(run, lo, hi, total)
                    if start is not None and windows:
                        first = windows[0]
                        windows[0] = incremental.Window(lo=start, hi=first.hi, include_nulls=first.include_nulls)
                    missing = total - with_cursor
                    run.note(
                        f"Source has {total:,} rows"
                        + (f" from {lo} to {hi}" if lo is not None else "")
                        + (f"; {missing:,} have no '{config.cursor_column}' value" if missing else "")
                        + f". Loading in {len(windows)} window(s)."
                    )
        except _PASS_THROUGH:
            raise
        except Exception as e:
            raise _SourceError(str(e)) from e

        part_path, key = loader.new_part(run.connection_id)
        return {
            "path": str(part_path),
            "key_enc": artifacts.encrypt_key(key),
            "definition_hash": run.definition_hash,
            "started_at": datetime.utcnow().isoformat(timespec="seconds"),
            "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "failures": 0,
            "source_rows": total,
            "rows": 0,
            "windows": [{**w.to_json(), "status": "pending", "rows": None} for w in windows],
        }

    def _plan_windows(self, run: "_Run", lo, hi, total: int) -> list:
        """Windows for a load of `total` rows spanning [lo, hi]."""
        whole = [incremental.Window(lo=None, hi=None, include_nulls=run.wrapped)]
        if not incremental.can_bound_above(run.sql):
            if total > incremental.CHUNK_TARGET_ROWS:
                run.note("The query has {{since}} but no {{until}}, so it loads as one window. "
                         "Add `AND <column> < {{until}}` to load it in chunks.", "warning")
            return whole
        dialect = incremental.dialect_for(run.client)
        column, kind = run.config.cursor_column, run.cursor_kind

        def count_between(a, b) -> int:
            sql = incremental.count_sql(run.sql, dialect, column, kind, incremental.Window(lo=a, hi=b))
            _cols, rows = self._source(run, sql).preview(sql, 1)
            return int(rows[0][0] or 0)

        try:
            return incremental.plan_by_density(lo, hi, total, count_between, include_nulls=run.wrapped)
        except Exception as e:
            run.note(f"Could not measure where the rows are ({str(e)[:200]}); "
                     f"cutting the range evenly instead.", "warning")
            return incremental.plan_chunks(lo, hi, total, include_nulls=run.wrapped)

    def _work_window(self, run: "_Run", index: int) -> tuple:
        load = run.load
        window = incremental.Window.from_json(load["windows"][index])
        try:
            rendered = self._render(run, window)
            rows = loader.load_window(
                self._source(run, rendered), rendered, load["path"],
                artifacts.decrypt_key(load["key_enc"]), run.name, run.run_id, self._limits(run),
                batch_rows=extractor.BATCH_ROWS, rows_before=int(load.get("rows") or 0),
                window=index,
            )
            return rows, rendered
        except _PASS_THROUGH:
            raise
        except Exception as e:
            raise _SourceError(str(e)) from e

    def _work_finalize(self, run: "_Run"):
        load = run.load
        try:
            return loader.finalize(
                load["path"], artifacts.decrypt_key(load["key_enc"]), run.name, run.config,
                previous_rows=run.previous_rows, run_id=run.run_id,
            )
        except BaseException:
            loader.discard_part(load["path"])
            raise

    def _work_increment(self, run: "_Run"):
        checkpoint = incremental.decode(run.state.get("checkpoint"))
        window = incremental.incremental_window(checkpoint, run.config)
        window = incremental.align_window(run.client, run.sql, window, run.config)
        run.window_from = window.lo
        try:
            rendered = self._render(run, window)
            run.rendered_query = rendered
            est = extractor.estimate(run.client, rendered)
            extractor.check_budget(est)
            return loader.apply_increment(
                self._source(run, rendered), rendered, run.previous_path, run.previous_key,
                run.name, run.connection_id, run.run_id, run.config, window, self._limits(run),
                batch_rows=extractor.BATCH_ROWS, previous_rows=run.previous_rows, wrapped=run.wrapped,
                filter_locally=not run.at_source,
            )
        except _PASS_THROUGH:
            raise
        except Exception as e:
            raise _SourceError(str(e)) from e

    # -- publish -----------------------------------------------------------

    def _next_state(self, run: "_Run", outcome, full: bool) -> Optional[dict]:
        """The row's refresh state once `outcome` is the served artifact."""
        if not run.config.incremental:
            return None
        previous = incremental.decode(run.state.get("checkpoint"))
        checkpoint = incremental.cap_checkpoint(outcome.max_cursor, datetime.utcnow())
        if not full and previous is not None and (
            checkpoint is None or (_comparable(previous, checkpoint) and previous > checkpoint)
        ):
            # An increment never moves the checkpoint backwards.
            checkpoint = previous
        last_full = datetime.utcnow().isoformat(timespec="seconds") if full else run.state.get(
            "last_full_refresh_at")
        new_state = {
            "checkpoint": incremental.encode(checkpoint),
            "definition_hash": run.definition_hash,
            "last_full_refresh_at": last_full,
        }
        if not full and run.state.get("full_refresh_retry_after"):
            new_state["full_refresh_retry_after"] = run.state["full_refresh_retry_after"]
        return new_state

    async def _publish(self, run: "_Run", res, *, full: bool) -> None:
        db, cq = run.db, run.cq
        old_path = cq.artifact_path
        new_state = self._next_state(run, res, full)
        try:
            # Conditional on the row still being live: delete takes no
            # lock, so it may have soft-deleted this table mid-extraction.
            # Swapping a reference onto a deleted row would leak the new
            # file forever (GC protects every referenced path).
            swap = await db.execute(
                update(ConnectionTable)
                .where(
                    ConnectionTable.id == cq.id,
                    ConnectionTable.deleted_at.is_(None),
                )
                .values(
                    artifact_path=res.artifact_path,
                    artifact_key_enc=artifacts.encrypt_key(res.artifact_key),
                    artifact_bytes=res.artifact_bytes,
                    columns=res.columns,
                    no_rows=res.row_count,
                    last_refreshed_at=datetime.utcnow(),
                    last_refresh_status="ok",
                    last_refresh_error=None,
                    last_refresh_ms=run.elapsed_ms(),
                    # The checkpoint moves in the same statement as the
                    # artifact it describes — together, or not at all.
                    refresh_state=new_state,
                )
                .execution_options(synchronize_session=False)
            )
            if swap.rowcount != 1:
                await db.rollback()
                artifacts.delete_artifact(res.artifact_path)
                await db.refresh(cq)
                logger.info(
                    "custom_query.refresh.superseded_by_delete",
                    extra={"custom_query": cq.name},
                )
                await runs.finish(run.run_id, status=STATUS_CANCELLED,
                                  error="The table was deleted while it was refreshing.",
                                  events=run.take_events(), chunks=run.chunk_log())
                return
            await db.commit()
        except BaseException:
            # A commit error can be ambiguous (e.g. a lost acknowledgement).
            # Only discard the new file after a fresh DB read proves it is
            # unreferenced. If verification fails, leave it for daily GC.
            try:
                await db.rollback()
                referenced = (await db.execute(
                    select(ConnectionTable.id).where(
                        ConnectionTable.artifact_path == res.artifact_path
                    ).limit(1)
                )).first()
                if referenced is None:
                    artifacts.delete_artifact(res.artifact_path)
            except Exception:
                logger.exception("custom_query.failed_swap.cleanup_failed")
            raise

        for text_ in res.notes:
            run.note(text_, "warning")
        await runs.finish(
            run.run_id, status=STATUS_SUCCESS, events=run.take_events(), chunks=run.chunk_log(),
            rows_read=res.rows_read, rows_inserted=res.rows_inserted,
            rows_updated=res.rows_updated, rows_deleted=res.rows_deleted,
            row_count=res.row_count, artifact_bytes=res.artifact_bytes,
            rendered_query=run.rendered_query, schema_fingerprint=res.schema_fingerprint,
            window_from=incremental.encode(run.window_from),
            window_to=(new_state or {}).get("checkpoint"),
        )
        run.published = True

        # DataSourceTable carries legacy copies of name/columns/no_rows that
        # the schema context reads; leaving them stale would show agents the
        # previous shape of the relation.
        try:
            await db.refresh(cq)
            await self._sync_activations(db, cq)
        finally:
            # The swap is committed even if activation metadata sync fails.
            if old_path and old_path != res.artifact_path:
                artifacts.delete_artifact(old_path)

        table_events.emit(table_events.TABLE_PUBLISHED, {
            "table_id": str(cq.id), "connection_id": str(run.connection.id),
            "run_id": run.run_id, "rows": res.row_count,
            "schema_fingerprint": res.schema_fingerprint,
        })
        logger.info(
            "custom_query.refresh.ok",
            extra={
                "custom_query": cq.name,
                "rows": res.row_count,
                "ms": run.elapsed_ms(),
            },
        )

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

    async def scheduled_refresh(self, connection_id: str, cq_id: str,
                                trigger: str = TRIGGER_SCHEDULE) -> None:
        """APScheduler entry point. Owns its own session.

        The job store is shared, so every worker fires this tick; the claim
        keeps the losers from queueing behind the winner's full scan (or
        repeating it when it is fast).
        """
        from app.core.scheduler import claim_scheduled_run
        from app.dependencies import async_session_maker

        claim = self._resume_job_id(cq_id) if trigger == TRIGGER_RESUME else self._job_id(cq_id)
        if not await asyncio.to_thread(claim_scheduled_run, claim):
            return
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
                await self.refresh(db, conn, cq, trigger=trigger)
            except Exception as e:
                logger.error(
                    "custom_query.scheduled_refresh.failed",
                    extra={"custom_query": cq_id, "error": str(e)},
                )

    def _job_id(self, cq_id: str) -> str:
        return job_id_for(cq_id)

    @staticmethod
    def _resume_job_id(cq_id: str) -> str:
        return f"custom_query_resume_{cq_id}"

    def _schedule_resume(self, connection_id: str, cq_id: str, delay_seconds: int = 5) -> None:
        """Continue a paused load soon, rather than at the table's next
        scheduled refresh (which may be a day away)."""
        try:
            scheduler.add_job(
                func=self.scheduled_refresh,
                trigger="date",
                run_date=datetime.utcnow() + timedelta(seconds=delay_seconds),
                timezone="UTC",
                id=self._resume_job_id(cq_id),
                args=[connection_id, cq_id],
                kwargs={"trigger": TRIGGER_RESUME},
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except Exception as e:
            logger.error("custom_query.resume_schedule_failed",
                         extra={"custom_query": cq_id, "error": str(e)})

    def _unschedule_resume(self, cq_id: str) -> None:
        try:
            scheduler.remove_job(job_id=self._resume_job_id(cq_id))
        except (JobLookupError, Exception):
            pass

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
        """Assemble a FastQueryClient from ACTIVATED custom tables.

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
