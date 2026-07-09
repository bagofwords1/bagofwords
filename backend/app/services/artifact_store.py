# Path: backend/app/services/artifact_store.py
"""Investigation Artifact Store — spill / slice / retention.

Large tool results (create_data, step reruns; later execute_mcp/custom_api)
are persisted as ONE ENCRYPTED DUCKDB FILE PER ARTIFACT on shared storage
(an NFS mount in production, any directory in dev), with a handle row
(`InvestigationArtifact`) in the backend DB as the control plane.

Key properties (see sandbox-feedback-loop-investigation-artifact-store.md):
- Atomic publish: tmp write -> fsync file -> rename within the mount ->
  fsync dir. The handle row is inserted only after the file is durable, so a
  `published` handle always points at a complete, attachable file.
- Per-artifact data key (AES-256-GCM via DuckDB native encryption), wrapped
  by the master Fernet key. Nulling `wrapped_key` crypto-shreds the payload.
- Write-once: reruns create a NEW artifact and link lineage via
  `superseded_by`; a published payload is never mutated.
- Slicing is DuckDB SQL over the artifact: page / regex match / column
  projection / time_range / SELECT-only free-form SQL — always bounded.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models.investigation_artifact import InvestigationArtifact
from app.settings.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (env-overridable; org settings gate the feature itself)
# ---------------------------------------------------------------------------

# Spill when the serialized full result exceeds this many bytes, even if the
# row cap did not truncate it.
ARTIFACT_FLOOR_BYTES = int(os.environ.get("BOW_ARTIFACT_FLOOR_BYTES", 512_000))

# Hard cap on rows returned by any single slice call (bounded page, D12).
SLICE_MAX_ROWS = int(os.environ.get("BOW_ARTIFACT_SLICE_MAX_ROWS", 500))

# Default page size when the caller does not specify a limit.
SLICE_DEFAULT_ROWS = 100

# Default retention when the org has no step_retention_days setting.
DEFAULT_RETENTION_DAYS = 14

# Grace period before an on-disk file with no handle row is swept (a crashed
# publish leaves either a .tmp or, at worst, a renamed file with no handle).
ORPHAN_GRACE_SECONDS = int(os.environ.get("BOW_ARTIFACT_ORPHAN_GRACE_S", 3600))


def artifact_store_root() -> str:
    """Root directory of the artifact store (NFS mount in production)."""
    return os.environ.get(
        "BOW_ARTIFACT_STORE_PATH",
        os.path.join(os.getcwd(), "uploads", "artifacts"),
    )


# ---------------------------------------------------------------------------
# SELECT-only jail for free-form slice SQL
# ---------------------------------------------------------------------------

_FORBIDDEN_SQL = re.compile(
    r"(?is)\b("
    r"attach|detach|copy|install|load|pragma|create|insert|update|delete|drop|"
    r"alter|export|import|call|set|reset|vacuum|checkpoint|begin|commit|"
    r"rollback|transaction|grant|revoke|use|"
    r"read_csv|read_parquet|read_json|read_text|read_blob|sniff_csv|glob|"
    r"getenv|current_setting|duckdb_settings|force"
    r")\b"
)


def validate_slice_sql(sql: str) -> Optional[str]:
    """Return an error string if `sql` is not a safe single SELECT, else None."""
    if not sql or not sql.strip():
        return "empty sql"
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        return "multiple statements are not allowed"
    head = stripped.lstrip("( \n\t").lower()
    if not (head.startswith("select") or head.startswith("with")):
        return "only SELECT (or WITH ... SELECT) statements are allowed"
    m = _FORBIDDEN_SQL.search(stripped)
    if m:
        return f"forbidden keyword in slice sql: {m.group(1)!r}"
    return None


# ---------------------------------------------------------------------------
# Storage backend (directory semantics — NFS mount or local dir)
# ---------------------------------------------------------------------------

class LocalDirStorage:
    """Directory-backed artifact storage with atomic publish.

    Works identically on a local dir and an NFS mount: tmp files live under
    `<root>/.tmp`, published payloads under
    `<root>/artifacts/{org}/{yyyy}/{mm}/{artifact_id}.duckdb`, and publish is
    a same-filesystem rename (atomic on one mount). No locking anywhere —
    artifacts are write-once, read-only-many.
    """

    def __init__(self, root: Optional[str] = None):
        self.root = root or artifact_store_root()

    # -- paths ------------------------------------------------------------
    def tmp_path(self, artifact_id: str) -> str:
        d = os.path.join(self.root, ".tmp")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{artifact_id}.duckdb.tmp")

    def ref_for(self, organization_id: str, artifact_id: str, when: Optional[datetime] = None) -> str:
        when = when or datetime.utcnow()
        return os.path.join(
            "artifacts", str(organization_id), f"{when:%Y}", f"{when:%m}", f"{artifact_id}.duckdb"
        )

    def abs_path(self, storage_ref: str) -> str:
        # storage_ref is always relative to the root; refuse anything else.
        if os.path.isabs(storage_ref) or ".." in storage_ref.split(os.sep):
            raise ValueError(f"invalid storage_ref: {storage_ref!r}")
        return os.path.join(self.root, storage_ref)

    # -- atomic publish ----------------------------------------------------
    def publish(self, tmp_path: str, storage_ref: str) -> None:
        final = self.abs_path(storage_ref)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        # fsync the payload before rename so the rename never exposes a
        # partially-flushed file, then fsync the directory entry.
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, final)
        dfd = os.open(os.path.dirname(final), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def delete(self, storage_ref: str) -> bool:
        try:
            os.remove(self.abs_path(storage_ref))
            return True
        except FileNotFoundError:
            return False

    def exists(self, storage_ref: str) -> bool:
        return os.path.exists(self.abs_path(storage_ref))

    def sweep_orphan_tmp(self, older_than_s: int = ORPHAN_GRACE_SECONDS) -> int:
        """Delete stale .tmp files from crashed publishes. Returns count."""
        d = os.path.join(self.root, ".tmp")
        if not os.path.isdir(d):
            return 0
        cutoff = datetime.utcnow().timestamp() - older_than_s
        n = 0
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except OSError:
                continue
        return n


# ---------------------------------------------------------------------------
# Spill result passed from producers (tool process) to handle persistence
# ---------------------------------------------------------------------------

@dataclass
class SpillResult:
    """Everything needed to insert the handle row after the file is durable."""
    artifact_id: str
    storage_ref: str
    wrapped_key: str
    content_sha256: str
    row_count: int
    byte_size: int
    schema_json: List[Dict[str, str]] = field(default_factory=list)
    ts_column: Optional[str] = None
    producer: str = "create_data"
    content_type: str = "table"
    source_meta: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "storage_ref": self.storage_ref,
            "wrapped_key": self.wrapped_key,
            "content_sha256": self.content_sha256,
            "row_count": self.row_count,
            "byte_size": self.byte_size,
            "schema_json": self.schema_json,
            "ts_column": self.ts_column,
            "producer": self.producer,
            "content_type": self.content_type,
            "source_meta": self.source_meta,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SpillResult":
        return cls(**{k: payload.get(k) for k in (
            "artifact_id", "storage_ref", "wrapped_key", "content_sha256",
            "row_count", "byte_size", "schema_json", "ts_column",
            "producer", "content_type", "source_meta",
        )})


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class ArtifactStoreService:
    def __init__(self, storage: Optional[LocalDirStorage] = None):
        self.storage = storage or LocalDirStorage()

    # -- feature gate / floor ----------------------------------------------
    @staticmethod
    def enabled(organization_settings) -> bool:
        if organization_settings is None:
            return True
        try:
            return bool(organization_settings.get_config("enable_artifact_store").value)
        except Exception:
            return True

    @staticmethod
    def should_spill(total_rows: int, stored_rows: int, approx_bytes: int) -> bool:
        """Dual-write floor (D6): spill when the stored copy is truncated, or
        the full result is large even under the row cap."""
        if total_rows > stored_rows:
            return True
        return approx_bytes > ARTIFACT_FLOOR_BYTES

    # -- key management ------------------------------------------------------
    @staticmethod
    def _fernet() -> Fernet:
        return Fernet(settings.bow_config.encryption_key)

    @classmethod
    def _mint_key(cls) -> tuple[str, str]:
        raw = secrets.token_hex(32)  # 64 hex chars -> 256-bit key
        wrapped = cls._fernet().encrypt(raw.encode()).decode()
        return raw, wrapped

    @classmethod
    def unwrap_key(cls, wrapped_key: str) -> str:
        raw = cls._fernet().decrypt(wrapped_key.encode()).decode()
        if not re.fullmatch(r"[0-9a-f]{64}", raw):
            raise ValueError("unwrapped artifact key has unexpected format")
        return raw

    # -- write path ----------------------------------------------------------
    @staticmethod
    def detect_ts_column(df: pd.DataFrame) -> Optional[str]:
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                return str(col)
        return None

    def spill_dataframe_sync(
        self,
        df: pd.DataFrame,
        *,
        organization_id: str,
        producer: str = "create_data",
        content_type: str = "table",
        source_meta: Optional[Dict[str, Any]] = None,
    ) -> SpillResult:
        """Write the full df as an encrypted DuckDB artifact. Blocking; run via
        `spill_dataframe` (thread) from async code.

        Raises on any failure — the caller MUST surface a loud
        `artifact_spill_failed` (never a silent drop).
        """
        artifact_id = str(uuid.uuid4())
        raw_key, wrapped = self._mint_key()
        ts_column = self.detect_ts_column(df)
        tmp = self.storage.tmp_path(artifact_id)
        meta = dict(source_meta or {})

        con = duckdb.connect()
        try:
            con.execute(f"ATTACH '{tmp}' AS art (ENCRYPTION_KEY '{raw_key}')")
            con.register("df_src", df)
            if ts_column is not None:
                # Time-sorted write -> zone-map row-group skipping for
                # time_range slices (D14).
                con.execute(f'CREATE TABLE art.data AS SELECT * FROM df_src ORDER BY "{ts_column}"')
            else:
                con.execute("CREATE TABLE art.data AS SELECT * FROM df_src")
            con.execute("CREATE TABLE art._meta (producer VARCHAR, source_meta JSON, created_at TIMESTAMP)")
            con.execute(
                "INSERT INTO art._meta VALUES (?, ?, now())",
                [producer, json.dumps(meta, default=str)],
            )
            con.execute("DETACH art")
        finally:
            try:
                con.close()
            except Exception:
                pass

        storage_ref = self.storage.ref_for(organization_id, artifact_id)
        self.storage.publish(tmp, storage_ref)

        final = self.storage.abs_path(storage_ref)
        sha = hashlib.sha256()
        with open(final, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)

        return SpillResult(
            artifact_id=artifact_id,
            storage_ref=storage_ref,
            wrapped_key=wrapped,
            content_sha256=sha.hexdigest(),
            row_count=int(len(df)),
            byte_size=os.path.getsize(final),
            schema_json=[{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns],
            ts_column=ts_column,
            producer=producer,
            content_type=content_type,
            source_meta=meta,
        )

    async def spill_dataframe(self, df: pd.DataFrame, **kwargs) -> SpillResult:
        return await asyncio.to_thread(self.spill_dataframe_sync, df, **kwargs)

    # -- handle persistence ---------------------------------------------------
    async def persist_handle(
        self,
        db,
        spill: SpillResult,
        *,
        organization_id: str,
        report_id: Optional[str] = None,
        step_id: Optional[str] = None,
        query_id: Optional[str] = None,
        tool_execution_id: Optional[str] = None,
        retention_days: Optional[int] = None,
        commit: bool = True,
    ) -> InvestigationArtifact:
        """Insert the handle row (file is already durable) and chain rerun
        lineage: the previous published artifact for the same step/query gets
        `superseded_by` = new id. Write-once — nothing is ever overwritten."""
        artifact = InvestigationArtifact(
            id=spill.artifact_id,
            organization_id=str(organization_id),
            report_id=str(report_id) if report_id else None,
            step_id=str(step_id) if step_id else None,
            query_id=str(query_id) if query_id else None,
            tool_execution_id=str(tool_execution_id) if tool_execution_id else None,
            producer=spill.producer,
            content_type=spill.content_type,
            schema_json=spill.schema_json,
            ts_column=spill.ts_column,
            row_count=spill.row_count,
            byte_size=spill.byte_size,
            storage_ref=spill.storage_ref,
            format="duckdb",
            content_sha256=spill.content_sha256,
            wrapped_key=spill.wrapped_key,
            status="published",
            expires_at=datetime.utcnow() + timedelta(days=retention_days or DEFAULT_RETENTION_DAYS),
        )

        # Rerun lineage (D10): supersede the latest published artifact bound
        # to the same step (in-place rerun) or query (new step per run).
        prev = None
        if step_id or query_id:
            conds = []
            if step_id:
                conds.append(InvestigationArtifact.step_id == str(step_id))
            if query_id:
                conds.append(InvestigationArtifact.query_id == str(query_id))
            from sqlalchemy import or_
            res = await db.execute(
                select(InvestigationArtifact)
                .where(
                    InvestigationArtifact.organization_id == str(organization_id),
                    InvestigationArtifact.status == "published",
                    InvestigationArtifact.superseded_by.is_(None),
                    InvestigationArtifact.id != spill.artifact_id,
                    or_(*conds),
                )
                .order_by(InvestigationArtifact.created_at.desc())
                .limit(1)
            )
            prev = res.scalar_one_or_none()
        if prev is not None:
            prev.superseded_by = spill.artifact_id
            db.add(prev)

        db.add(artifact)
        if commit:
            await db.commit()
            await db.refresh(artifact)
        return artifact

    # -- read path ------------------------------------------------------------
    async def get_artifact(
        self, db, organization_id: str, artifact_id: str
    ) -> Optional[InvestigationArtifact]:
        res = await db.execute(
            select(InvestigationArtifact).where(
                InvestigationArtifact.id == str(artifact_id),
                InvestigationArtifact.organization_id == str(organization_id),
                InvestigationArtifact.deleted_at.is_(None),
            )
        )
        return res.scalar_one_or_none()

    async def latest_for_step(
        self, db, organization_id: str, *, step_id: Optional[str] = None, query_id: Optional[str] = None
    ) -> Optional[InvestigationArtifact]:
        conds = []
        if step_id:
            conds.append(InvestigationArtifact.step_id == str(step_id))
        if query_id:
            conds.append(InvestigationArtifact.query_id == str(query_id))
        if not conds:
            return None
        from sqlalchemy import or_
        res = await db.execute(
            select(InvestigationArtifact)
            .where(
                InvestigationArtifact.organization_id == str(organization_id),
                InvestigationArtifact.status == "published",
                or_(*conds),
            )
            .order_by(InvestigationArtifact.created_at.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()

    def _attach_readonly(self, artifact: InvestigationArtifact) -> duckdb.DuckDBPyConnection:
        if artifact.status != "published":
            raise ValueError(f"artifact {artifact.id} is {artifact.status} (payload unavailable)")
        if not artifact.wrapped_key:
            raise ValueError(
                f"artifact {artifact.id} has been shredded per retention policy — payload is unrecoverable"
            )
        key = self.unwrap_key(artifact.wrapped_key)
        path = self.storage.abs_path(artifact.storage_ref)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"artifact payload missing for {artifact.id} (published handle without file — this is a bug)"
            )
        con = duckdb.connect()
        con.execute(f"ATTACH '{path}' AS art (ENCRYPTION_KEY '{key}', READ_ONLY)")
        # Defense-in-depth: after the (external) attach, cut off any further
        # filesystem/extension access so free-form slice SQL cannot escape,
        # then lock the configuration. Best-effort: validation is the primary
        # wall; these SETs harden it where the DuckDB build allows.
        for stmt in (
            "SET enable_external_access=false",
            "SET lock_configuration=true",
        ):
            try:
                con.execute(stmt)
            except Exception:
                logger.debug("artifact slice hardening skipped: %s", stmt)
        con.execute("CREATE TEMP VIEW data AS SELECT * FROM art.data")
        return con

    def slice_sync(
        self,
        artifact: InvestigationArtifact,
        *,
        offset: int = 0,
        limit: Optional[int] = None,
        match: Optional[str] = None,
        match_column: Optional[str] = None,
        columns: Optional[List[str]] = None,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
        sql: Optional[str] = None,
        allow_llm_see_data: bool = True,
    ) -> Dict[str, Any]:
        """Run one bounded slice against the artifact payload.

        Returns {columns, rows, total_matches, offset, limit, next_offset,
        truncated, sql_used} — rows omitted when privacy mode is on (counts
        and row numbers only, never raw cells).
        """
        page_limit = min(int(limit or SLICE_DEFAULT_ROWS), SLICE_MAX_ROWS)
        offset = max(int(offset or 0), 0)
        schema_cols = [c.get("name") for c in (artifact.schema_json or []) if c.get("name")]

        con = self._attach_readonly(artifact)
        try:
            if sql:
                err = validate_slice_sql(sql)
                if err:
                    raise ValueError(f"slice sql rejected: {err}")
                if not allow_llm_see_data:
                    # Privacy mode: free-form SQL restricted to aggregates —
                    # heuristic: require an aggregate function and forbid
                    # SELECT * (raw cell exfiltration).
                    low = sql.lower()
                    if not re.search(r"\b(count|sum|avg|min|max|stddev|median|approx_)\w*\s*\(", low) or "select *" in low:
                        raise ValueError(
                            "slice sql rejected: organization policy (allow_llm_see_data=off) "
                            "permits aggregate-only SQL (COUNT/SUM/AVG/...), not raw rows"
                        )
                inner = sql.strip().rstrip(";")
                total = None  # totals are not meaningful for arbitrary SQL
                cur = con.execute(f"SELECT * FROM ({inner}) __t LIMIT {page_limit + 1} OFFSET {offset}")
                out_cols = [d[0] for d in cur.description]
                fetched = cur.fetchall()
                sql_used = inner
            else:
                where, params = [], []
                if match:
                    re.compile(match)  # fail fast on invalid regex
                    if match_column:
                        if schema_cols and match_column not in schema_cols:
                            raise ValueError(f"unknown match_column {match_column!r}")
                        target = f'CAST("{match_column}" AS VARCHAR)'
                    else:
                        cols = schema_cols or []
                        if not cols:
                            cur = con.execute("SELECT * FROM data LIMIT 0")
                            cols = [d[0] for d in cur.description]
                        target = "concat_ws(' ', " + ", ".join(
                            f'COALESCE(CAST("{c}" AS VARCHAR), \'\')' for c in cols
                        ) + ")"
                    where.append(f"regexp_matches({target}, ?)")
                    params.append(match)
                if time_from or time_to:
                    if not artifact.ts_column:
                        raise ValueError("time_range requested but artifact has no detected timestamp column")
                    if time_from:
                        where.append(f'"{artifact.ts_column}" >= CAST(? AS TIMESTAMP)')
                        params.append(time_from)
                    if time_to:
                        where.append(f'"{artifact.ts_column}" <= CAST(? AS TIMESTAMP)')
                        params.append(time_to)

                proj = "*"
                if columns:
                    bad = [c for c in columns if schema_cols and c not in schema_cols]
                    if bad:
                        raise ValueError(f"unknown columns: {bad}")
                    proj = ", ".join(f'"{c}"' for c in columns)

                where_sql = (" WHERE " + " AND ".join(where)) if where else ""
                # NOTE: structured slices target art.data directly — `rowid`
                # (deterministic page order; data was written time-sorted when
                # a ts column exists) is a base-table pseudocolumn and is not
                # visible through the TEMP VIEW used for free-form SQL.
                total = con.execute(f"SELECT count(*) FROM art.data{where_sql}", params).fetchone()[0]
                cur = con.execute(
                    f"SELECT {proj} FROM art.data{where_sql} ORDER BY rowid LIMIT {page_limit + 1} OFFSET {offset}",
                    params,
                )
                out_cols = [d[0] for d in cur.description]
                fetched = cur.fetchall()
                sql_used = f"SELECT {proj} FROM data{where_sql} ORDER BY rowid"

            has_more = len(fetched) > page_limit
            fetched = fetched[:page_limit]

            result: Dict[str, Any] = {
                "artifact_id": str(artifact.id),
                "columns": out_cols,
                "total_matches": int(total) if total is not None else None,
                "offset": offset,
                "limit": page_limit,
                "next_offset": offset + page_limit if has_more else None,
                "truncated": bool(has_more),
                "sql_used": sql_used,
                "row_count_total": int(artifact.row_count),
            }
            if allow_llm_see_data:
                def _json_safe(v):
                    # Slice results travel through SSE/JSON (tool events,
                    # observations) — emit only JSON-native types.
                    if v is None or isinstance(v, (bool, int, float, str)):
                        return v
                    if isinstance(v, (bytes, bytearray)):
                        return v.hex()
                    if isinstance(v, datetime):
                        return v.isoformat()
                    return str(v)

                result["rows"] = [[_json_safe(v) for v in row] for row in fetched]
            else:
                result["rows_hidden"] = True
                result["returned_rows"] = len(fetched)
                result["note"] = (
                    "Row-level data hidden by organization policy (allow_llm_see_data off): "
                    "counts and structure only."
                )
            return result
        finally:
            try:
                con.close()
            except Exception:
                pass

    async def slice(self, artifact: InvestigationArtifact, **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self.slice_sync, artifact, **kwargs)

    # -- retention -------------------------------------------------------------
    async def purge_expired(self, db, *, now: Optional[datetime] = None, commit: bool = True) -> int:
        """Crypto-shred + delete payloads for expired, unheld, uncited
        artifacts. Walks HANDLES, never the filesystem first. Returns count."""
        now = now or datetime.utcnow()
        res = await db.execute(
            select(InvestigationArtifact).where(
                InvestigationArtifact.status == "published",
                InvestigationArtifact.expires_at.isnot(None),
                InvestigationArtifact.expires_at < now,
                InvestigationArtifact.legal_hold.is_(False),
                InvestigationArtifact.cited.is_(False),
            )
        )
        n = 0
        for artifact in res.scalars().all():
            artifact.wrapped_key = None            # crypto-shred first
            artifact.status = "tombstoned"
            self.storage.delete(artifact.storage_ref)
            db.add(artifact)
            n += 1
        if n and commit:
            await db.commit()
        self.storage.sweep_orphan_tmp()
        return n
