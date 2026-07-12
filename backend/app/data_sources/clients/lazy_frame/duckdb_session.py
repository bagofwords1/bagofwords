"""Hardened DuckDB connections for LazyFrame compute, plus the in-process
reservation ledger that keeps concurrent connections' temp-spill maxima
disjoint."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import suppress
from pathlib import Path

from .config import StreamConfig, _env_int
from .errors import ResultTooLargeError

logger = logging.getLogger(__name__)

_spill_reservation_lock = threading.Lock()
_spill_reserved_bytes: dict[Path, int] = {}
_spill_reservation_counts: dict[Path, int] = {}


def _reserve_duckdb_temp(config: StreamConfig) -> tuple[Path, int]:
    """Atomically reserve one connection's potential DuckDB spill growth.

    A capacity snapshot alone races: several connections can all observe the
    same remaining bytes and each configure DuckDB to consume the full amount.
    Reservations make those live maxima disjoint within this server process.
    The physical root scan still guards capacity shared with other processes.
    """
    root = config.root.resolve()
    max_connections = max(
        1,
        _env_int("BOW_LAZY_MAX_CONCURRENT_COMPUTES", 8),
    )
    per_connection_limit = min(
        max(0, int(config.max_bytes)),
        max(0, int(config.dir_max_bytes)),
    )
    if per_connection_limit <= 0:
        raise ResultTooLargeError(
            rows=0,
            byte_estimate=0,
            limit_desc="DuckDB temp spill budget is zero",
        )

    with _spill_reservation_lock:
        remaining = config.remaining_capacity_bytes()
        already_reserved = _spill_reserved_bytes.get(root, 0)
        active = _spill_reservation_counts.get(root, 0)
        available = remaining - already_reserved
        slots = max(1, max_connections - active)
        budget = min(per_connection_limit, available // slots)
        if budget <= 0:
            raise ResultTooLargeError(
                rows=0,
                byte_estimate=max(0, int(already_reserved)),
                limit_desc=(
                    "no unreserved DuckDB spill capacity remains "
                    f"(dir_max_bytes={config.dir_max_bytes})"
                ),
            )
        _spill_reserved_bytes[root] = already_reserved + int(budget)
        _spill_reservation_counts[root] = active + 1
        return root, int(budget)


def _release_duckdb_temp(root: Path, budget: int) -> None:
    with _spill_reservation_lock:
        remaining = _spill_reserved_bytes.get(root, 0) - int(budget)
        if remaining > 0:
            _spill_reserved_bytes[root] = remaining
        else:
            _spill_reserved_bytes.pop(root, None)
        active = _spill_reservation_counts.get(root, 0) - 1
        if active > 0:
            _spill_reservation_counts[root] = active
        else:
            _spill_reservation_counts.pop(root, None)


class _ReservedDuckDBConnection:
    """Connection proxy that releases its spill reservation exactly once."""

    def __init__(self, inner, reservation_root: Path, reservation_bytes: int):
        self._inner = inner
        self._reservation_root = reservation_root
        self._reservation_bytes = reservation_bytes
        self._closed = False
        self._close_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._inner.close()
            finally:
                _release_duckdb_temp(
                    self._reservation_root,
                    self._reservation_bytes,
                )


def _open_duckdb(allowed_dirs, config: StreamConfig | None = None):
    """Hardened DuckDB connection for LazyFrame compute.

    The relation is exposed to sandboxed LLM-generated code via .sql(), so an
    unrestricted connection would be a filesystem read/write escape hatch
    (read_csv('/etc/passwd'), COPY TO ...) around the Python sandbox's
    open/os bans — and each connection defaults to ~80% of system RAM.
    Confinement: file access limited to the spill dir(s), explicit memory
    budget (BOW_LAZY_DUCKDB_MEM, default 2GB), DuckDB's own out-of-core temp
    state under the spill root, and the configuration locked so generated SQL
    can't SET any of it back."""
    import duckdb

    config = config or StreamConfig()
    config.check_capacity(full=True)
    reservation_root, temp_budget = _reserve_duckdb_temp(config)
    try:
        con = duckdb.connect(database=":memory:")
    except BaseException:
        _release_duckdb_temp(reservation_root, temp_budget)
        raise

    def _q(s) -> str:
        return str(s).replace("'", "''")

    dirs = sorted({str(Path(d)) for d in allowed_dirs})
    try:
        quoted = ", ".join(f"'{_q(d)}'" for d in dirs)
        con.execute(f"SET allowed_directories=[{quoted}]")
        con.execute(
            f"SET memory_limit='{_q(os.environ.get('BOW_LAZY_DUCKDB_MEM') or '2GB')}'"
        )
        con.execute(f"SET max_temp_directory_size='{temp_budget}B'")
        if dirs:
            tmp_dir = Path(dirs[0]) / "duckdb_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory='{_q(tmp_dir)}'")
        con.execute("SET enable_external_access=false")
        con.execute("SET lock_configuration=true")
    except Exception as exc:
        # This connection is exposed through LazyFrame.sql() to generated code.
        # Returning it without every confinement knob would turn a config/version
        # problem into a filesystem sandbox escape, so fail closed.
        with suppress(Exception):
            con.close()
        _release_duckdb_temp(reservation_root, temp_budget)
        raise RuntimeError("DuckDB filesystem confinement could not be configured") from exc
    return _ReservedDuckDBConnection(con, reservation_root, temp_budget)
