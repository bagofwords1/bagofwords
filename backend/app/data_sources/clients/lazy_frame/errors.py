"""Error types raised across the lazy (out-of-core) query path."""

from __future__ import annotations

from app.errors.app_error import AppError
from app.errors.codes import ErrorCode


class ResultTooLargeError(AppError):
    """Raised when a streamed result exceeds the configured row/byte budget.

    Mirrors QueryTimeoutError: the surrounding handler surfaces it to the
    planner so the agent can narrow the query (LIMIT / filters / aggregation).
    """

    def __init__(self, *, rows: int, byte_estimate: int, limit_desc: str) -> None:
        message = (
            f"Query result exceeded the streaming budget ({limit_desc}) after "
            f"{rows} rows (~{byte_estimate} bytes). Narrow the query with a "
            "LIMIT, tighter filters, or aggregation."
        )
        super().__init__(
            ErrorCode.QUERY_RESULT_TOO_LARGE,
            message,
            status_code=413,
            params={"rows": int(rows), "byte_estimate": int(byte_estimate)},
        )
        self.rows = int(rows)
        self.byte_estimate = int(byte_estimate)


class LazyComputeTimeoutError(AppError):
    """Raised when downstream DuckDB work exceeds its wall-clock budget."""

    def __init__(self, timeout_seconds: float) -> None:
        message = (
            f"Lazy result computation exceeded {timeout_seconds:g}s. Narrow the "
            "query, apply filters/aggregation before materializing, or use LIMIT."
        )
        super().__init__(
            ErrorCode.QUERY_TIMEOUT,
            message,
            status_code=408,
            params={"timeout_seconds": timeout_seconds},
        )
        self.timeout_seconds = timeout_seconds


class QueryAbandonedError(RuntimeError):
    """The caller timed out and abandoned this stream; stop consuming."""

    def __init__(self) -> None:
        super().__init__(
            "Lazy stream aborted: the caller timed out and abandoned this query."
        )
