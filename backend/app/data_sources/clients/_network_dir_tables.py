"""Derived SQL relations for a scoped network-directory connection."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients._pcap_parquet import FRAME_SCHEMA, convert_capture
from app.data_sources.clients.progress import IndexingCancelled
from app.data_sources.fast.parquet_crypto import decryption_properties, encrypt_parquet
from app.services import storage

logger = logging.getLogger(__name__)
EXTENSIONS = {"pcap", "pcapng", "qvd"}
STATUS_SCHEMA = pa.schema([(key, pa.string()) for key in ("file_id", "table_name", "status", "reason")])
CACHE_VERSION = 2


def _digest(*parts):
    return hashlib.sha256(b"\0".join(os.fsencode(str(s)) for s in parts)).hexdigest()


def cache_directory(owner):
    return storage.UPLOADS_ROOT / "network_dir_tables" / _digest(owner)


def forget_versions(owner, prior_catalog, current):
    """Remove only versions known to the preceding catalog, never an in-flight writer's files."""
    directory = cache_directory(owner)
    for entry in (prior_catalog or {}).values():
        meta = entry.get("network_dir") if isinstance(entry, dict) else None
        version = meta.get("cache_version") if isinstance(meta, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version) or version in current:
            continue
        for suffix in (".parquet", ".error"):
            path = directory / (version + suffix)
            storage.discard(path)
            path.unlink(missing_ok=True)


def clear_cache(owner):
    directory = cache_directory(owner)
    storage.discard_tree(directory)
    if directory.exists():
        shutil.rmtree(directory)


@dataclass
class Source:
    file_id: str
    path: Path
    table_name: str
    artifact: Path
    too_large: bool

    @classmethod
    def resolve(cls, client, file_id):
        if not client._allowed(file_id) or (not client.recursive and len(Path(file_id).parts) > 1):
            raise ValueError("File is outside the current directory selection")
        path = client._resolve(file_id, must_exist=True)
        if not path.is_file() or not client._allowed(path.name):
            raise ValueError("Resolved file is outside the current directory selection")
        stat = path.stat()
        owner = getattr(client, "_bow_connection_id", None) or str(client._root())
        version = _digest(CACHE_VERSION, file_id, path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        artifact = cache_directory(owner) / f"{version}.parquet"
        table_name = "packets"
        if file_id.lower().endswith(".qvd"):
            stem = re.sub(r"[^a-z0-9_]+", "_", Path(file_id).stem.lower()).strip("_")
            table_name = f"qvd_{stem[:48]}_{_digest(file_id)[:12]}"
        return cls(
            file_id, path, table_name, artifact, bool(client.max_file_bytes and stat.st_size > client.max_file_bytes)
        )

    def schema(self):
        return pq.read_schema(self.artifact, decryption_properties=decryption_properties())

    def status(self):
        if self.too_large:
            return "failed", "Exceeds the connection file size limit"
        if storage.ensure_local(self.artifact, fresh=False):
            return "ready", None
        failure = self.artifact.with_suffix(".error")
        if storage.ensure_local(failure, fresh=False):
            return "failed", failure.read_text()
        return "pending", None

    def prepare(self, client, checkpoint):
        if self.too_large or storage.ensure_local(self.artifact, fresh=False):
            return
        self.artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A private staging path per attempt. Readers only see completed versions.
        with TemporaryDirectory(dir=self.artifact.parent) as work:
            result = Path(work) / "result.parquet"
            try:
                if self.table_name == "packets":
                    convert_capture(self.path, self.file_id, result, checkpoint)
                else:
                    from app.data_sources.clients.qvd_client import QVDClient

                    def check_cancel():
                        try:
                            checkpoint()
                        except IndexingCancelled:
                            return True
                        return False

                    with TemporaryDirectory() as scratch:
                        plain = Path(scratch) / "qvd.parquet"
                        QVDClient(str(self.path)).convert_to_parquet(str(self.path), plain, cancel_check=check_cancel)
                        encrypt_parquet(plain, result)
                if Source.resolve(client, self.file_id).artifact != self.artifact:
                    return  # source changed during conversion; the new version is pending
                os.replace(result, self.artifact)
                storage.persist(self.artifact)
                previous_error = self.artifact.with_suffix(".error")
                if previous_error.exists():
                    storage.discard(previous_error)
                    previous_error.unlink(missing_ok=True)
            except IndexingCancelled:
                raise
            except Exception as exc:
                if isinstance(exc, FileNotFoundError) and exc.filename == "tshark":
                    reason = "TShark is not installed"
                else:
                    reason = "Conversion failed; see the indexing log"
                logger.warning("Could not index %s: %s", self.file_id, exc)
                error = Path(work) / "error"
                error.write_text(reason)
                failure = self.artifact.with_suffix(".error")
                os.replace(error, failure)
                storage.persist(failure)


def columns(schema):
    if not len(schema):
        return []
    with duckdb.connect() as con:
        relation = con.from_arrow(schema.empty_table())
        return [TableColumn(name=n, dtype=str(t)) for n, t in zip(relation.columns, relation.types)]


def catalog_relations(schemas_by_table):
    """Only virtual relations carry this marker. Physical files keep their catalog identity."""
    schemas = {"indexed_files": STATUS_SCHEMA}
    for name, file_schemas in schemas_by_table.items():
        if name == "packets":
            schemas[name] = pa.unify_schemas([FRAME_SCHEMA, *file_schemas])
        else:
            schemas[name] = file_schemas[0] if file_schemas else pa.schema([])
    descriptions = {
        "indexed_files": "Coverage of packet captures and QVDs. Check status before reporting totals.",
        "packets": "One row per frame. Discover all decoded fields with SELECT column_name FROM duckdb_columns() WHERE table_name='packets' AND column_name LIKE 'dns.%'. Quote dotted fields; protocol values are lists.",
    }
    return [
        Table(
            name=name,
            columns=columns(schema),
            pks=[],
            fks=[],
            description=descriptions.get(name, "Indexed QVD table."),
            metadata_json={"network_dir_relation": True},
        )
        for name, schema in schemas.items()
    ]


def attach_catalog(client, selected, files):
    """Bind exactly the agent's selected connection relations and indexed file inventory."""
    client._indexed_files = tuple(files)
    client._network_dir_relations = {name for name, _, _ in selected}
    client._catalog_capped = False
    client._packet_schema = FRAME_SCHEMA
    for name, cols, meta in selected:
        if name == "indexed_files":
            client._catalog_capped = bool(meta.get("catalog_capped"))
        elif name == "packets":
            fields = [c["name"] for c in cols if c["name"] not in FRAME_SCHEMA.names]
            client._packet_schema = pa.schema([*FRAME_SCHEMA, *(pa.field(f, pa.list_(pa.string())) for f in fields)])


def reads_as_statements(text):
    """Whether DuckDB reads `text` as SQL statements rather than a file name.
    It is parsed on a locked engine: DuckDB opens what some statements name
    while it parses them. Text the locked engine refuses to parse is SQL,
    for the query checks to refuse."""
    from app.data_sources.fast.fast_client import lock_query_session

    with duckdb.connect() as con:
        lock_query_session(con)
        try:
            return bool(con.extract_statements(text))
        except duckdb.ParserException:
            return False
        except duckdb.Error:
            return True


def query_tables(client, sql):
    from app.ai.code_execution.sql_guard import check_sql
    from app.data_sources.fast.fast_client import assert_no_blocked_introspection, lock_query_session

    allowed = getattr(client, "_network_dir_relations", frozenset())
    if not allowed:
        raise PermissionError("Select an indexed table for this agent before querying")
    check_sql(sql, "duckdb")
    assert_no_blocked_introspection(sql)
    with duckdb.connect() as con:
        # Locked before DuckDB reads the statement; the relations below are
        # registered objects, which the lock does not stop.
        lock_query_session(con)
        statements = con.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("Only one read-only SELECT or DESCRIBE is supported")
        status_rows, captures = [], []
        scan = ds.ParquetFragmentScanOptions(decryption_properties=decryption_properties())
        parquet = ds.ParquetFileFormat(default_fragment_scan_options=scan)
        for file_id, version in client._indexed_files:
            try:
                source = Source.resolve(client, file_id)
            except (OSError, ValueError):
                continue
            if version != source.artifact.stem:
                status, reason = "pending", "File changed since this catalog was indexed"
            else:
                status, reason = source.status()
            status_rows.append(dict(file_id=file_id, table_name=source.table_name, status=status, reason=reason))
            if status != "ready" or source.table_name not in allowed:
                continue
            if source.table_name == "packets":
                captures.append(source.artifact)
            else:
                con.register(source.table_name, ds.dataset(source.artifact, format=parquet, schema=source.schema()))
        if "indexed_files" in allowed:
            if client._catalog_capped:
                status_rows.append(
                    dict(file_id=None, table_name=None, status="truncated", reason="Directory catalog limit reached")
                )
            con.register("indexed_files", pa.Table.from_pylist(status_rows, schema=STATUS_SCHEMA))
        if "packets" in allowed:
            schema = client._packet_schema
            con.register(
                "packets", ds.dataset(captures, format=parquet, schema=schema) if captures else schema.empty_table()
            )
        return con.execute(sql).df()
