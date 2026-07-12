"""Budgets and limits for the lazy path (policy; storage mechanics live in
`storage.py`)."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ResultTooLargeError
from .storage import LocalSpillStorage, SpillStorage


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _materialization_limits() -> tuple[int, int]:
    """Operator ceilings shared by every public materialization surface."""
    max_rows = max(0, _env_int("BOW_LAZY_RESULT_MATERIALIZE_CAP", 1_000_000))
    max_bytes = max(
        0,
        _env_int(
            "BOW_LAZY_RESULT_MATERIALIZE_MAX_BYTES",
            512 * 1024 * 1024,
        ),
    )
    return max_rows, max_bytes


class StreamConfig:
    """Caps for streaming ingest. Generous defaults; tune via env.

    Holds the *policy* (row/byte/aggregate budgets) and applies it against a
    `SpillStorage` backend, which owns the *mechanics* of where spill bytes
    live (today: a secured local directory)."""

    def __init__(self, storage: SpillStorage | None = None) -> None:
        self.chunksize = _env_int("BOW_LAZY_CHUNKSIZE", 50_000)
        self.max_rows = _env_int("BOW_LAZY_MAX_ROWS", 50_000_000)
        self.max_bytes = _env_int("BOW_LAZY_MAX_BYTES", 8 * 1024 * 1024 * 1024)
        # Aggregate guards: per-query budgets don't bound N concurrent
        # queries. dir_max_bytes caps the whole spill root; min_free_bytes
        # keeps a floor of free disk so one tenant's spill can't ENOSPC the
        # box for everyone.
        self.dir_max_bytes = _env_int("BOW_LAZY_DIR_MAX_BYTES", 32 * 1024 * 1024 * 1024)
        self.min_free_bytes = _env_int("BOW_LAZY_MIN_FREE_BYTES", 1024 * 1024 * 1024)
        self.storage = storage or LocalSpillStorage.from_env()
        self.storage.sweep_stale()

    @property
    def root(self) -> Path:
        return self.storage.root

    def new_spill_path(self) -> Path:
        return self.storage.new_spill_path()

    def limit_desc(self) -> str:
        return f"max_rows={self.max_rows}, max_bytes={self.max_bytes}"

    def check_capacity(self, full: bool = False) -> None:
        """Enforce aggregate spill-root and free-space limits.

        ``full=True`` sums every file under the dedicated spill root, including
        Parquet parts and DuckDB temp state. Consumers call it both before and
        after growth so a query cannot start below the cap and then silently
        push the root over it.
        """
        free = self.storage.free_bytes()
        if free is None:
            return  # never let the guard itself break a query
        if free < self.min_free_bytes:
            raise ResultTooLargeError(
                rows=0, byte_estimate=0,
                limit_desc=f"spill disk free space below floor ({free} < {self.min_free_bytes})",
            )
        if not full:
            return
        total = self.storage.used_bytes()
        if total is None:
            return
        if total > self.dir_max_bytes:
            raise ResultTooLargeError(
                rows=0, byte_estimate=int(total),
                limit_desc=f"aggregate spill dir over budget (dir_max_bytes={self.dir_max_bytes})",
            )

    def remaining_capacity_bytes(self) -> int:
        """Best available DuckDB temp allowance under aggregate/free-space caps.

        DuckDB otherwise defaults to 90% of the whole filesystem. Even if a
        stat call fails, the configured aggregate cap remains an upper bound.
        """
        total = self.storage.used_bytes()
        if total is None:
            total = 0
        remaining = self.dir_max_bytes - total
        free = self.storage.free_bytes()
        if free is not None:
            remaining = min(remaining, free - self.min_free_bytes)
        if remaining <= 0:
            raise ResultTooLargeError(
                rows=0,
                byte_estimate=max(0, int(total)),
                limit_desc=(
                    "no spill capacity remains under aggregate/free-space budget "
                    f"(dir_max_bytes={self.dir_max_bytes}, "
                    f"min_free_bytes={self.min_free_bytes})"
                ),
            )
        return int(remaining)
