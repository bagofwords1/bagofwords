"""Column-level usage stats (BOW-215).

Fed from the same places as ``TableStats``:

* every finished step (success and failure): the columns its executed queries
  reference, extracted against the agent's catalog
  (``app/utils/column_extraction.py``), merged with the step's data-model
  lineage, and written in ONE batched upsert per step;
* thumbs up / down on a completion: applied to the columns the completion's
  steps used (``TableUsageEvent.columns``), again one batched upsert.

Dedupe is inherited from ``TableUsageEvent`` (unique per step and table): the
caller only passes tables whose usage event was newly recorded, so a step
counts once per column however many times its usage is emitted.

Read path: :meth:`load_stats` reads the rows of a bounded set of tables in one
query (``WHERE datasource_table_id IN (...)``, served by the unique index).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.column_stats import ColumnStats
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.utils.column_extraction import (
    CatalogTable,
    _index_tables,
    _match_table,
    extract_columns,
    merge_into,
    parse_json_envelope,
    sql_table_references,
)

logger = logging.getLogger(__name__)

# A failed attempt adds this fraction of a success to weighted usage: the
# column was relevant to the question, but a failure is weaker evidence than
# a query that worked. Failures never subtract (see column_ranking).
FAILURE_USAGE_WEIGHT = 0.25

DEFAULT_ROLE_WEIGHTS = {"admin": 1.5, "analyst": 1.2, "viewer": 0.8, "trusted": 1.5}

# Rows per INSERT statement (Postgres caps bind parameters at 32767).
_UPSERT_CHUNK = 500


@dataclass
class TableColumnUsage:
    data_source_id: str
    datasource_table_id: str
    table_name: str
    columns: Set[str]


def _dialect_for_type(conn_type: Optional[str]) -> Optional[str]:
    """sqlglot dialect for a connection type (via its client class)."""
    if not conn_type:
        return None
    try:
        from app.ai.code_execution.sql_guard import CLIENT_DIALECTS
        from app.schemas.data_source_registry import resolve_client_class

        return CLIENT_DIALECTS.get(resolve_client_class(conn_type).__name__)
    except Exception:
        return None


def _query_dialect(query) -> Optional[str]:
    """Dialect from the client that ran the query, when the capture carries it
    (``identity_bind.CapturedQuery.client_type``)."""
    ct = getattr(query, "client_type", None)
    if not ct:
        return None
    from app.ai.code_execution.sql_guard import CLIENT_DIALECTS

    return CLIENT_DIALECTS.get(ct)


class ColumnUsageService:
    def __init__(self, role_weights: Optional[dict] = None):
        self.role_weights = role_weights or dict(DEFAULT_ROLE_WEIGHTS)

    # ------------------------------------------------------------ resolve
    async def columns_for_step(
        self,
        db: AsyncSession,
        data_source_ids: Sequence[str],
        queries: Iterable,
        lineage: Optional[Dict[str, Iterable[str]]] = None,
    ) -> Dict[str, TableColumnUsage]:
        """Columns a step used, keyed by DataSourceTable id, validated against
        the catalog. ``queries`` are the step's executed queries (SQL text or
        JSON envelopes); ``lineage`` is {table id: columns} from the step's
        data model (regex over output-column sources, kept as a fallback for
        sources whose queries cannot be parsed)."""
        ds_ids = [str(d) for d in dict.fromkeys(data_source_ids or []) if d and not str(d).startswith("builtin:")]
        queries = [q for q in (queries or []) if isinstance(q, (str, dict)) and q]
        lineage = {str(k): set(v or []) for k, v in (lineage or {}).items() if k}
        if not ds_ids or (not queries and not lineage):
            return {}

        # 1. Names (cheap: no column JSON) and connection types of every table.
        name_rows = (await db.execute(
            select(DataSourceTable.id, DataSourceTable.name, DataSourceTable.datasource_id, Connection.type)
            .outerjoin(ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id)
            .outerjoin(Connection, ConnectionTable.connection_id == Connection.id)
            .where(DataSourceTable.datasource_id.in_(ds_ids), DataSourceTable.deleted_at.is_(None))
        )).all()
        if not name_rows:
            return {}
        stubs = [CatalogTable(key=str(r[0]), name=str(r[1] or ""), columns=[]) for r in name_rows]
        ds_of = {str(r[0]): str(r[2]) for r in name_rows}
        dialects = list(dict.fromkeys(_dialect_for_type(r[3]) for r in name_rows))

        # 2. Which tables the queries touch.
        wanted: Set[str] = set(lineage)
        for q in queries:
            env = parse_json_envelope(q)
            if env is not None:
                if isinstance(env.get("sql"), str):
                    refs = sql_table_references(env["sql"], None)
                    wanted.update(t.key for t in (_match_table(p, stubs) for p in refs) if t)
                elif isinstance(env.get("index"), str):
                    wanted.update(t.key for t in _index_tables(env["index"], stubs))
                continue
            for d in ([_query_dialect(q)] if _query_dialect(q) else dialects):
                refs = sql_table_references(str(q), d)
                hits = [t for t in (_match_table(p, stubs) for p in refs) if t]
                if hits:
                    wanted.update(t.key for t in hits)
                    break
        if not wanted:
            return {}

        # 3. Columns of just those tables.
        col_rows = (await db.execute(
            select(DataSourceTable.id, DataSourceTable.name, DataSourceTable.columns)
            .where(DataSourceTable.id.in_(list(wanted)))
        )).all()
        catalog = [
            CatalogTable(
                key=str(r[0]), name=str(r[1] or ""),
                columns=[c.get("name") for c in (r[2] or []) if isinstance(c, dict) and c.get("name")],
            )
            for r in col_rows
        ]
        by_key = {t.key: t for t in catalog}

        found: Dict[str, Set[str]] = {}
        for q in queries:
            per_dialect = [_query_dialect(q)] if _query_dialect(q) else dialects
            for d in per_dialect:
                cols = extract_columns(q, d, catalog)
                if cols:
                    merge_into(found, cols)
                    break
        for key, cols in lineage.items():
            t = by_key.get(key)
            if t is None:
                continue
            valid = {t.column(c) for c in cols} - {None}
            if valid:
                found.setdefault(key, set()).update(valid)
        return {
            key: TableColumnUsage(ds_of.get(key, ""), key, by_key[key].name, cols)
            for key, cols in found.items() if key in by_key and cols
        }

    # -------------------------------------------------------------- write
    def _role_weight(self, user_role: Optional[str], role_weight: Optional[float]) -> float:
        if role_weight is not None:
            return float(role_weight)
        if user_role:
            return float(self.role_weights.get(str(user_role).lower(), 1.0))
        return 1.0

    async def record_step_usage(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        usages: Sequence[TableColumnUsage],
        success: bool,
        user_role: Optional[str] = None,
        role_weight: Optional[float] = None,
        at: Optional[datetime] = None,
    ) -> int:
        """One batched upsert for every (table, column) the step used.
        Returns the number of column rows touched."""
        w = self._role_weight(user_role, role_weight)
        now = at or datetime.utcnow()
        rows = []
        for u in usages or []:
            for col in sorted(u.columns or []):
                rows.append({
                    "org_id": org_id,
                    "data_source_id": u.data_source_id,
                    "datasource_table_id": u.datasource_table_id,
                    "column_name": col[:1024],
                    "usage_count": 1,
                    "success_count": 1 if success else 0,
                    "failure_count": 0 if success else 1,
                    "weighted_usage_count": w if success else w * FAILURE_USAGE_WEIGHT,
                    "last_used_at": now,
                })
        if not rows:
            return 0
        await self._upsert(db, rows, increment=(
            "usage_count", "success_count", "failure_count", "weighted_usage_count",
        ), latest=("last_used_at",))
        return len(rows)

    async def record_feedback(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        usages: Sequence[TableColumnUsage],
        direction: str,
        user_role: Optional[str] = None,
        role_weight: Optional[float] = None,
        at: Optional[datetime] = None,
    ) -> int:
        """Thumbs up / down on an answer, applied to the columns it used."""
        w = self._role_weight(user_role, role_weight)
        positive = direction == "positive"
        now = at or datetime.utcnow()
        rows = []
        for u in usages or []:
            for col in sorted(u.columns or []):
                rows.append({
                    "org_id": org_id,
                    "data_source_id": u.data_source_id,
                    "datasource_table_id": u.datasource_table_id,
                    "column_name": col[:1024],
                    "pos_feedback_count": 1 if positive else 0,
                    "neg_feedback_count": 0 if positive else 1,
                    "weighted_pos_feedback": w if positive else 0.0,
                    "weighted_neg_feedback": 0.0 if positive else w,
                    "last_feedback_at": now,
                })
        if not rows:
            return 0
        await self._upsert(db, rows, increment=(
            "pos_feedback_count", "neg_feedback_count", "weighted_pos_feedback", "weighted_neg_feedback",
        ), latest=("last_feedback_at",))
        return len(rows)

    async def _upsert(self, db: AsyncSession, rows: List[dict], *, increment: Tuple[str, ...], latest: Tuple[str, ...]) -> None:
        import uuid

        dialect = db.get_bind().dialect.name
        defaults = {
            "usage_count": 0, "success_count": 0, "failure_count": 0, "weighted_usage_count": 0.0,
            "pos_feedback_count": 0, "neg_feedback_count": 0, "weighted_pos_feedback": 0.0,
            "weighted_neg_feedback": 0.0,
        }
        now = datetime.utcnow()
        full = []
        for r in rows:
            row = {**defaults, **r, "id": str(uuid.uuid4()), "created_at": now, "updated_at": now,
                   "updated_at_stats": now}
            row["rank_weight"] = row["weighted_usage_count"] + row["weighted_pos_feedback"] - row["weighted_neg_feedback"]
            full.append(row)
        if dialect in ("postgresql", "sqlite"):
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            # A savepoint: on the agent's shared single-writer session a
            # failed stats write must not take other pending writes with it.
            async with db.begin_nested():
                await self._upsert_chunks(db, insert, full, increment, latest)
        else:  # pragma: no cover - the app database is SQLite or PostgreSQL
            for r in full:
                existing = (await db.execute(select(ColumnStats).where(
                    ColumnStats.datasource_table_id == r["datasource_table_id"],
                    ColumnStats.column_name == r["column_name"],
                ))).scalar_one_or_none()
                if existing is None:
                    db.add(ColumnStats(**r))
                else:
                    for k in increment:
                        setattr(existing, k, (getattr(existing, k) or 0) + r[k])
                    existing.rank_weight = (existing.rank_weight or 0.0) + r["rank_weight"]
                    for k in latest:
                        setattr(existing, k, r[k] or getattr(existing, k))
                    existing.updated_at_stats = now
        await db.commit()

    @staticmethod
    async def _upsert_chunks(db, insert, full, increment, latest) -> None:
        for i in range(0, len(full), _UPSERT_CHUNK):
            stmt = insert(ColumnStats).values(full[i:i + _UPSERT_CHUNK])
            ex = stmt.excluded
            set_ = {k: getattr(ColumnStats, k) + getattr(ex, k) for k in increment}
            # rank_weight moves by this row's net weight delta.
            set_["rank_weight"] = ColumnStats.rank_weight + ex.rank_weight
            for k in latest:
                set_[k] = func.coalesce(getattr(ex, k), getattr(ColumnStats, k))
            set_["updated_at_stats"] = ex.updated_at_stats
            set_["updated_at"] = ex.updated_at
            await db.execute(stmt.on_conflict_do_update(
                index_elements=["datasource_table_id", "column_name"], set_=set_,
            ))

    # --------------------------------------------------------------- read
    async def load_stats(self, db: AsyncSession, table_ids: Iterable[str],
                         per_table_limit: Optional[int] = None) -> Dict[str, Dict[str, object]]:
        """{table id: {lowercased column name: stats row}} for the given
        tables, in one query.

        Rows are plain SQL rows (attribute access by ColumnStats column name),
        not ORM entities: a 5,000-column table can carry thousands of rows and
        building an entity per row is what made the read cost grow with the
        table. ``per_table_limit`` keeps only each table's top rows by
        ``rank_weight`` (net weighted usage), read in index order, so the
        schema build reads a bounded number of rows however much history a
        table accumulates."""
        ids = [str(t) for t in dict.fromkeys(table_ids or []) if t]
        out: Dict[str, Dict[str, object]] = {}
        if not ids:
            return out
        cols = [
            ColumnStats.datasource_table_id, ColumnStats.column_name, ColumnStats.usage_count,
            ColumnStats.success_count, ColumnStats.failure_count, ColumnStats.weighted_usage_count,
            ColumnStats.pos_feedback_count, ColumnStats.neg_feedback_count, ColumnStats.weighted_pos_feedback,
            ColumnStats.weighted_neg_feedback, ColumnStats.last_used_at, ColumnStats.last_feedback_at,
        ]
        if per_table_limit and per_table_limit > 0:
            # One statement: per table, its top rows in index order
            # (ix_colstats_table_rank), glued with UNION ALL. Each branch is a
            # bounded index range scan, so the read does not grow with the
            # rows a table has accumulated.
            branches = [
                select(*cols).where(ColumnStats.datasource_table_id == tid)
                .order_by(ColumnStats.rank_weight.desc()).limit(per_table_limit).subquery()
                for tid in ids
            ]
            parts = [select(*[b.c[c.key] for c in cols]) for b in branches]
            stmt = parts[0] if len(parts) == 1 else union_all(*parts)
        else:
            stmt = select(*cols).where(ColumnStats.datasource_table_id.in_(ids))
        for r in (await db.execute(stmt)).all():
            out.setdefault(str(r.datasource_table_id), {})[str(r.column_name).lower()] = r
        return out

    async def usage_for_step(self, db: AsyncSession, step_id: str) -> List[TableColumnUsage]:
        """Columns a step's successful usage events recorded (feedback target)."""
        from app.models.table_usage_event import TableUsageEvent

        rows = (await db.execute(
            select(TableUsageEvent.data_source_id, TableUsageEvent.datasource_table_id,
                   TableUsageEvent.table_fqn, TableUsageEvent.columns)
            .where(TableUsageEvent.step_id == str(step_id), TableUsageEvent.success == True)  # noqa: E712
        )).all()
        out = []
        for ds_id, table_id, fqn, cols in rows:
            cols = [c for c in (cols or []) if isinstance(c, str) and c]
            if ds_id and table_id and cols:
                out.append(TableColumnUsage(str(ds_id), str(table_id), fqn or "", set(cols)))
        return out
