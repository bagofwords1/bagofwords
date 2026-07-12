import asyncio
import contextvars
import inspect
import io
import os
import sys
import ast
import re
import threading
import time as _time
import pandas as pd
import numpy as np
import datetime
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from typing import Dict, Any, Tuple, List, Optional, Callable, Coroutine

from app.ai.http.safe_client import SafeHttpClient

# `redirect_stdout` mutates the *global* sys.stdout. When the code-exec
# thread pool runs N executions concurrently, the enter/exit ordering
# can leave sys.stdout pointing at a sibling thread's already-closed
# StringIO buffer. The next print/df.info()/etc. inside ANY thread then
# raises ValueError("I/O operation on closed file"). Serializing the
# redirect_stdout window with a lock keeps the (very brief) duration of
# the redirect race-free; user code executes inside the lock but it's
# already CPU-bound by the GIL, so wall-clock impact is negligible.
_STDOUT_REDIRECT_LOCK = threading.Lock()
from app.schemas.organization_settings_schema import OrganizationSettingsConfig, FeatureState
from app.services.usage_policy_service import UsageLimitContext, usage_policy_service
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.ai.context.builders.code_context_builder import CodeContextBuilder
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.ai.code_execution.loadables import extract_loadable_refs
from app.core.otel import get_tracer
from opentelemetry.trace import StatusCode
from app.errors.app_error import AppError
from app.errors.codes import ErrorCode

# Hard fallback when neither connection nor org settings define a value.
DEFAULT_QUERY_TIMEOUT_SECONDS = 60

_tracer = get_tracer(__name__)

import logging
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)


def _is_sqlite_lock_error(exc: Exception) -> bool:
    """True for SQLite's single-writer lock timeout (dev/sandbox databases)."""
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message

# Dedicated thread pool for user code execution.
# Keeps code-exec threads isolated from the default asyncio executor so that
# stuck DB/network calls in generated code cannot starve other server operations.
# When all workers are occupied, new submissions queue; the idle-timeout in the
# tool runner will cancel queued futures (via Future.cancel()) before they start,
# preventing unbounded queue growth.
_CODE_EXEC_POOL = ThreadPoolExecutor(
    max_workers=min(8, (os.cpu_count() or 4) * 2),
    thread_name_prefix="bow_code_exec",
)


# =============================================================================
# Security Exceptions
# =============================================================================

class CodeSecurityError(Exception):
    """Base exception for code security violations."""
    pass


class UnsafePythonError(CodeSecurityError):
    """Raised when Python code contains dangerous constructs."""
    pass


class UnsafeSQLError(CodeSecurityError):
    """Raised when SQL query contains dangerous operations."""
    pass


class QueryTimeoutError(AppError):
    """Raised when a wrapped client.execute_query exceeds its wall-clock budget.

    Caught by the surrounding exception handler in generate_and_execute_stream_v2
    and surfaced to the planner via captured_timings -> observation.db_message.
    The underlying DB query may keep running on the server until the connection
    is closed; we just stop waiting for it.
    """

    def __init__(self, timeout_seconds: int, sql: Optional[str] = None) -> None:
        message = (
            f"Query exceeded {timeout_seconds}s timeout. "
            f"Run multiple smaller queries instead of one large scan — "
            f"each execute_query call gets its own {timeout_seconds}s budget. "
            "Use LIMIT, narrower filters, or aggregation."
        )
        super().__init__(
            ErrorCode.QUERY_TIMEOUT,
            message,
            status_code=408,
            params={"timeout_seconds": int(timeout_seconds)},
        )
        self.timeout_seconds = int(timeout_seconds)
        self.sql = sql


def resolve_query_timeout(client, organization_settings) -> int:
    """Per-connection timeout resolution.

    Connection.config['query_timeout_seconds'] (stashed onto the client as
    `_bow_connection_query_timeout`) wins. Otherwise the org default; otherwise
    the hard fallback. A connection setting can only tighten the budget — values
    <= 0 are ignored at every layer.
    """
    conn_value = getattr(client, "_bow_connection_query_timeout", None)
    if isinstance(conn_value, (int, float)) and conn_value > 0:
        return int(conn_value)
    org_value = _org_setting_value(organization_settings, "query_timeout_seconds")
    if isinstance(org_value, (int, float)) and org_value > 0:
        return int(org_value)
    return DEFAULT_QUERY_TIMEOUT_SECONDS


class LazyQueriesDisabledError(RuntimeError):
    """Raised when generated code calls execute_query_lazy but the org has not
    opted into the lazy (out-of-core) query path."""

    def __init__(self):
        super().__init__(
            "Lazy (out-of-core) queries are disabled for this organization. "
            "Use execute_query instead, or enable 'Lazy (out-of-core) queries' "
            "in organization settings."
        )


def _org_setting_value(organization_settings, key: str, default=None):
    """Read one org setting, unwrapping FeatureConfig. Any failure yields the
    default — settings lookups must never break query execution."""
    if organization_settings is None:
        return default
    try:
        org_cfg = organization_settings.get_config(key)
        return org_cfg.value if hasattr(org_cfg, "value") else org_cfg
    except Exception:
        return default


def resolve_lazy_enabled(organization_settings) -> bool:
    """Opt-in resolution for the lazy query path. Defaults to disabled: only an
    explicit truthy `enable_lazy_queries` org setting turns it on."""
    return bool(_org_setting_value(organization_settings, "enable_lazy_queries", False))


# =============================================================================
# AST-based Python Code Validation
# =============================================================================

# Modules that should never be imported
FORBIDDEN_MODULES = frozenset({
    'os', 'subprocess', 'sys', 'shutil', 'importlib', 'builtins',
    'code', 'pty', 'socket', 'requests', 'urllib', 'urllib3', 'http',
    'httpx', 'aiohttp', 'httplib2', 'curl_cffi', 'ftplib',
    'telnetlib', 'smtplib', 'poplib', 'imaplib', 'nntplib',
    'multiprocessing', 'threading', 'concurrent', 'asyncio',
    'ctypes', 'cffi', 'pickle', 'shelve', 'marshal',
    'tempfile', 'pathlib', 'glob', 'fnmatch',
    'signal', 'resource', 'sysconfig', 'platform',
    'webbrowser', 'antigravity', 'this',
})

# Built-in functions that should never be called
FORBIDDEN_BUILTINS = frozenset({
    'eval', 'exec', 'compile', 'open', 'input',
    '__import__', 'globals', 'locals', 'vars',
    'getattr', 'setattr', 'delattr', 'hasattr',
    'breakpoint', 'exit', 'quit',
    'memoryview', 'bytearray',
})

# Attribute access patterns that indicate sandbox escape attempts
FORBIDDEN_ATTRIBUTES = frozenset({
    '__class__', '__bases__', '__mro__', '__subclasses__',
    '__globals__', '__code__', '__closure__', '__func__',
    '__self__', '__dict__', '__builtins__', '__import__',
    '__loader__', '__spec__', '__path__', '__file__',
    '__cached__', '__annotations__',
})


class CodeSecurityVisitor(ast.NodeVisitor):
    """AST visitor that checks for dangerous code patterns."""

    def __init__(self):
        self.errors: List[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            module_name = alias.name.split('.')[0]
            if module_name in FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden import: '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            module_name = node.module.split('.')[0]
            if module_name in FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden import: 'from {node.module}'")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        # Check for forbidden built-in calls like eval(), exec(), open()
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_BUILTINS:
                self.errors.append(f"Forbidden function call: '{node.func.id}()'")

        # Check for __import__('os') style calls
        if isinstance(node.func, ast.Name) and node.func.id == '__import__':
            self.errors.append("Forbidden function call: '__import__()'")

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        # Check for direct access to forbidden attributes like obj.__class__
        if node.attr in FORBIDDEN_ATTRIBUTES:
            self.errors.append(f"Forbidden attribute access: '{node.attr}'")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        # Check string literals for dangerous SQL operations. Uses the
        # structural regex so prose like "create a chart" or "update the
        # description" isn't flagged — we only match keywords that appear in
        # real SQL context (CREATE TABLE, DELETE FROM, UPDATE x SET, ...).
        if isinstance(node.value, str) and len(node.value) > 5:
            match = _FORBIDDEN_SQL_IN_STRING_REGEX.search(node.value)
            if match:
                snippet = node.value[:50].replace('\n', ' ')
                self.errors.append(
                    f"Forbidden SQL operation '{match.group()}' in string: \"{snippet}...\""
                )
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr):
        # Check f-string parts for dangerous SQL using the same structural
        # regex — prose inside f-strings shouldn't trip the validator either.
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                match = _FORBIDDEN_SQL_IN_STRING_REGEX.search(part.value)
                if match:
                    snippet = part.value[:50].replace('\n', ' ')
                    self.errors.append(
                        f"Forbidden SQL operation '{match.group()}' in f-string: \"{snippet}...\""
                    )
        self.generic_visit(node)


def validate_python_code(code: str) -> None:
    """
    Validate Python code for security issues using AST analysis.

    Raises:
        UnsafePythonError: If the code contains dangerous constructs.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        # Let syntax errors pass through - they'll fail at exec() time
        # with a more descriptive error
        return

    visitor = CodeSecurityVisitor()
    visitor.visit(tree)

    if visitor.errors:
        raise UnsafePythonError(
            f"Code contains forbidden constructs: {'; '.join(visitor.errors)}"
        )


# =============================================================================
# SQL Query Validation
# =============================================================================

# SQL keywords that indicate write/modify operations
FORBIDDEN_SQL_PATTERNS = [
    r'\bINSERT\b',
    r'\bUPDATE\b',
    r'\bDELETE\b',
    r'\bDROP\b',
    r'\bTRUNCATE\b',
    r'\bALTER\b',
    r'\bCREATE\b',
    r'\bGRANT\b',
    r'\bREVOKE\b',
    r'\bEXEC\b',
    r'\bEXECUTE\b',
    r'\bMERGE\b',
    r'\bCALL\b',
    r'\bREPLACE\b',
    r'\bLOAD\b',
    r'\bINTO\s+OUTFILE\b',
    r'\bINTO\s+DUMPFILE\b',
]

# Pre-compile regex for performance
_FORBIDDEN_SQL_REGEX = re.compile(
    '|'.join(FORBIDDEN_SQL_PATTERNS),
    re.IGNORECASE
)

# Structural SQL-write patterns — used when scanning Python string literals so
# prose like "the user wants to create a chart" or "delete outdated rows from
# the description" doesn't trigger the bare-verb match above. Each pattern
# requires the keyword to sit next to a syntactic partner that real SQL always
# has (TABLE/VIEW/INTO/FROM/SET/...), which prose basically never does.
_FORBIDDEN_SQL_IN_STRING_PATTERNS = [
    r'\bCREATE\s+(OR\s+REPLACE\s+)?(TEMP(ORARY)?\s+)?(TABLE|VIEW|INDEX|DATABASE|SCHEMA|FUNCTION|PROCEDURE|TRIGGER|SEQUENCE|ROLE|USER|MATERIALIZED)\b',
    r'\bDROP\s+(TABLE|VIEW|INDEX|DATABASE|SCHEMA|FUNCTION|PROCEDURE|TRIGGER|SEQUENCE|COLUMN|CONSTRAINT|ROLE|USER)\b',
    r'\bALTER\s+(TABLE|VIEW|INDEX|DATABASE|SCHEMA|COLUMN|SEQUENCE|ROLE|USER)\b',
    r'\bTRUNCATE\s+(TABLE\s+)?\w+',
    r'\bINSERT\s+INTO\b',
    r'\bUPDATE\s+[\w.`"\[\]]+(\s+AS\s+\w+|\s+\w+)?\s+SET\b',
    r'\bDELETE\s+FROM\b',
    r'\bMERGE\s+INTO\b',
    r'\bREPLACE\s+INTO\b',
    r'\bGRANT\s+[\w,\s*]+\s+ON\b',
    r'\bREVOKE\s+[\w,\s*]+\s+(ON|FROM)\b',
    r'\bEXEC(UTE)?\s+(\w+\.)*\w+',
    r'\bCALL\s+\w+\s*\(',
    r'\bLOAD\s+DATA\b',
    r'\bINTO\s+OUTFILE\b',
    r'\bINTO\s+DUMPFILE\b',
]
_FORBIDDEN_SQL_IN_STRING_REGEX = re.compile(
    '|'.join(_FORBIDDEN_SQL_IN_STRING_PATTERNS),
    re.IGNORECASE,
)


def estimate_result_size_bytes(result: Any) -> int:
    """Best-effort size of the result payload exposed to generated code."""
    if result is None:
        return 0
    if isinstance(result, bytes):
        return len(result)
    if isinstance(result, str):
        return len(result.encode("utf-8"))
    if isinstance(result, pd.DataFrame):
        try:
            return len(result.to_json(orient="records", date_format="iso").encode("utf-8"))
        except Exception:
            return int(result.memory_usage(deep=True).sum())
    try:
        return len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return sys.getsizeof(result)


def validate_sql_query(query: str) -> None:
    """
    Validate SQL query to ensure it's read-only.

    Raises:
        UnsafeSQLError: If the query contains write/modify operations.
    """
    if not isinstance(query, str):
        return

    match = _FORBIDDEN_SQL_REGEX.search(query)
    if match:
        raise UnsafeSQLError(
            f"SQL query contains forbidden operation: '{match.group()}'. "
            "Only SELECT queries are allowed."
        )


# =============================================================================
# Query Capturing Wrapper (captures queries passed to execute_query)
# =============================================================================

class QueryCapturingClientWrapper:
    """Wrapper around a database client that captures all queries passed to execute_query.

    Works with any client that has an execute_query method (SQL, MongoDB, etc.).
    Optionally accumulates per-query wall-clock timing into captured_timings.
    Enforces a per-query wall-clock timeout: if the underlying call doesn't return
    in `query_timeout_seconds`, raises QueryTimeoutError. The orphan thread is left
    daemon so it doesn't block process exit; the DB-side query may continue until
    the connection is closed.
    """

    def __init__(
        self,
        original_client,
        captured_queries: List[str],
        captured_timings: List[dict],
        usage_context: Optional[UsageLimitContext] = None,
        client_key: Optional[str] = None,
        query_timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS,
        lazy_enabled: bool = False,
    ):
        self._original = original_client
        self._captured_queries = captured_queries
        self._captured_timings = captured_timings
        self._usage_context = usage_context
        self._client_key = client_key
        self._lazy_enabled = bool(lazy_enabled)
        self._query_timeout_seconds = (
            int(query_timeout_seconds)
            if isinstance(query_timeout_seconds, (int, float)) and query_timeout_seconds > 0
            else DEFAULT_QUERY_TIMEOUT_SECONDS
        )

    def execute_query(self, query: str, *args, **kwargs):
        """Intercept execute_query calls to capture the query string and wall-clock duration."""
        return self._run_instrumented(query, args, kwargs, method="execute_query", lazy=False)

    def execute_query_lazy(self, query: str, *args, **kwargs):
        """Out-of-core variant: same capture/quota/timeout treatment as
        execute_query, but the underlying call streams the result to a Parquet
        spill and returns a LazyFrame instead of a materialized DataFrame.

        Rows/bytes are metered from the spill file's Parquet metadata —
        uncompressed columnar size, so lazy and eager queries draw comparably
        on the org byte quota — never by materializing the frame, which would
        defeat the point of the lazy path. If the stream exceeds its budget,
        ResultTooLargeError propagates to the caller after being recorded in
        captured_timings.

        Opt-in: gated by the `enable_lazy_queries` org setting. Defining the
        method unconditionally and raising when disabled (rather than not
        defining it) keeps the call from falling through __getattr__ to the
        raw client, which would bypass capture, quotas, and the timeout.
        """
        if not self._lazy_enabled:
            raise LazyQueriesDisabledError()
        return self._run_instrumented(query, args, kwargs, method="execute_query_lazy", lazy=True)

    async def aexecute_query_lazy(self, query: str, *args, **kwargs):
        """Async variant. Must be defined here for the same reason as
        execute_query_lazy: the raw client grew aexecute_query_lazy too, and
        letting the call fall through __getattr__ would bypass the opt-in
        gate, capture, quotas, and the timeout."""
        return await asyncio.to_thread(self.execute_query_lazy, query, *args, **kwargs)

    def _meter_result(self, result, lazy: bool):
        """(rows, result_bytes) for quota + telemetry. Lazy metering must not
        silently degrade to zero: result_bytes <= 0 skips quota consumption
        entirely, so a swallowed metering error would let data flow unmetered.
        spill_stats reads rows+bytes in one Parquet-metadata pass; fallbacks
        degrade independently and failures are logged loudly."""
        if not lazy:
            rows = len(result) if hasattr(result, '__len__') else None
            return rows, estimate_result_size_bytes(result)
        rows = None
        result_bytes = 0
        unmeterable = False
        try:
            rows, result_bytes, disk_bytes = result.spill_stats()
            # spill_stats swallows per-file errors and returns zeros, so the
            # sentinel for "unmeterable" is zero DISK bytes: even a genuinely
            # empty result has a Parquet header on disk. Zero disk means the
            # spill vanished or is unreadable — not an empty result.
            unmeterable = disk_bytes <= 0
        except Exception:
            logger.warning(
                "Could not read LazyFrame spill stats; metering piecewise", exc_info=True
            )
            try:
                rows = result.row_count()
            except Exception:
                logger.warning("Could not read LazyFrame row count for metering", exc_info=True)
            try:
                result_bytes = result.byte_size()
                unmeterable = result_bytes <= 0
            except Exception:
                unmeterable = True
        if unmeterable:
            # With quota enforcement active, an unmeterable result must not
            # pass for free — _consume_data_bytes_quota skips consumption at
            # result_bytes <= 0, which would let arbitrary data through an
            # org's byte limit. Without a quota context (dev/tests), log and
            # continue.
            if self._usage_context is not None and self._usage_context.session_maker is not None:
                raise RuntimeError(
                    "Could not meter the lazy query result (spill file missing or "
                    "unreadable); refusing to return it unmetered while usage "
                    "limits are enforced."
                )
            logger.warning("LazyFrame byte metering failed; result is uncharged")
        return rows, result_bytes

    def _run_instrumented(self, query, args, kwargs, *, method: str, lazy: bool):
        """Shared capture/quota/span/timeout/timing core for the eager and lazy
        paths — one implementation so the two can't drift."""
        from app.data_sources.clients.lazy_frame import ResultTooLargeError

        if isinstance(query, str):
            self._captured_queries.append(query)
        idx = len(self._captured_timings)
        base = {
            "index": idx,
            "rows": None,
            "sql": query[:500] if isinstance(query, str) else None,
            **({"lazy": True} if lazy else {}),
        }
        _q_start = _time.monotonic()
        with _tracer.start_as_current_span(f"datasource.{method}") as span:
            span.set_attribute("datasource.type", type(self._original).__name__)
            span.set_attribute("datasource.query_timeout_seconds", self._query_timeout_seconds)
            try:
                self._consume_query_quota(query)
                result = self._call_with_timeout(query, args, kwargs, method=method)
                _q_ms = (_time.monotonic() - _q_start) * 1000.0
                try:
                    rows, result_bytes = self._meter_result(result, lazy)
                    self._consume_data_bytes_quota(query, result_bytes, rows)
                except BaseException:
                    # Metering failure or quota rejection: the caller never
                    # receives the result, so reclaim its spill now rather
                    # than leaving multi-GB files to GC / the stale sweep.
                    if lazy:
                        close = getattr(result, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                logger.debug("Failed to close rejected lazy result", exc_info=True)
                    raise
                if rows is not None:
                    span.set_attribute("datasource.result_rows", rows)
                span.set_attribute("datasource.result_bytes", result_bytes)
                self._captured_timings.append({
                    **base,
                    "query_ms": round(_q_ms, 1),
                    "rows": rows,
                    "result_bytes": result_bytes,
                })
                return result
            except QueryTimeoutError as e:
                _q_ms = (_time.monotonic() - _q_start) * 1000.0
                self._captured_timings.append({
                    **base,
                    "query_ms": round(_q_ms, 1),
                    "error": str(e)[:200],
                    "error_type": "timeout",
                    "timeout_seconds": self._query_timeout_seconds,
                })
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise
            except ResultTooLargeError as e:
                _q_ms = (_time.monotonic() - _q_start) * 1000.0
                self._captured_timings.append({
                    **base,
                    "query_ms": round(_q_ms, 1),
                    "rows": getattr(e, "rows", None),
                    "error": str(e)[:200],
                    "error_type": "result_too_large",
                })
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise
            except Exception as e:
                _q_ms = (_time.monotonic() - _q_start) * 1000.0
                self._captured_timings.append({
                    **base,
                    "query_ms": round(_q_ms, 1),
                    "error": str(e)[:200],
                })
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise

    def _call_with_timeout(self, query, args, kwargs, method: str = "execute_query"):
        """Run the original client's `method` in a daemon thread; abandon it on timeout.

        Threading is intentional rather than asyncio.wait_for: we're already
        inside a sync code-exec worker (user code is run via exec()), so we
        cannot await. ThreadPoolExecutor would risk pool exhaustion when many
        long queries pile up, hence a fresh per-call daemon thread.

        Abandonment is not cancellation: the underlying driver call cannot be
        interrupted, so a timed-out lazy stream keeps its data source
        connection until the stream finishes or hits its row/byte budget.
        What we CAN reclaim is the result: if the abandoned call eventually
        returns a closeable (a LazyFrame with a multi-GB spill), the runner
        closes it immediately instead of leaving it for the stale-file sweep.
        """
        holder: Dict[str, Any] = {}
        abandoned = threading.Event()

        def runner():
            try:
                # Register the abandonment Event for this thread: lazy chunk
                # consumers poll it between chunks, so a timed-out stream stops
                # consuming the source and disk instead of running to budget.
                from app.data_sources.clients.lazy_frame import set_cancel_event

                set_cancel_event(abandoned)
            except Exception:
                pass
            try:
                value = getattr(self._original, method)(query, *args, **kwargs)
            except BaseException as exc:
                if not abandoned.is_set():
                    holder["exc"] = exc
                return
            if abandoned.is_set():
                close = getattr(value, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.debug("Failed to close abandoned query result", exc_info=True)
            else:
                holder["value"] = value

        t = threading.Thread(
            target=runner,
            name="bow_query_timeout_guard",
            daemon=True,
        )
        t.start()
        t.join(self._query_timeout_seconds)
        if t.is_alive():
            abandoned.set()
            # Race window: the runner may have completed and stored its result
            # between join() expiring and abandoned.set() — close it here so a
            # multi-GB spill isn't left waiting for GC. (The LazyFrame
            # finalizer remains the backstop for the residual nanosecond
            # interleaving.)
            value = holder.pop("value", None)
            if value is not None:
                close = getattr(value, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.debug("Failed to close raced query result", exc_info=True)
            raise QueryTimeoutError(
                self._query_timeout_seconds,
                sql=query if isinstance(query, str) else None,
            )
        if "exc" in holder:
            raise holder["exc"]
        return holder.get("value")

    def _consume_query_quota(self, query: str) -> None:
        context = self._usage_context
        if context is None or context.session_maker is None:
            return
        connection_id = self._connection_id()
        if not connection_id:
            return
        metadata = self._usage_metadata(query)
        try:
            context.run_blocking(
                usage_policy_service.consume_data_query_with_context(
                    context,
                    connection_id=str(connection_id),
                    metadata=metadata,
                )
            )
        except OperationalError as e:
            # SQLite-only: the agent's long-lived session can hold the single
            # write lock while model code runs, making this bookkeeping write
            # time out. Metering is best-effort there (same policy as the LLM
            # usage recorder); enforcement raises UsageLimitExceeded, which is
            # not an OperationalError and still propagates.
            if not _is_sqlite_lock_error(e):
                raise
            logger.debug("Skipping data-query quota write; SQLite is locked")

    def _consume_data_bytes_quota(self, query: str, result_bytes: int, rows: Optional[int]) -> None:
        context = self._usage_context
        if context is None or context.session_maker is None or result_bytes <= 0:
            return
        connection_id = self._connection_id()
        if not connection_id:
            return
        metadata = {
            **self._usage_metadata(query),
            "rows": rows,
            "result_bytes": result_bytes,
        }
        try:
            context.run_blocking(
                usage_policy_service.consume_data_bytes_with_context(
                    context,
                    connection_id=str(connection_id),
                    amount=result_bytes,
                    metadata=metadata,
                )
            )
        except OperationalError as e:
            if not _is_sqlite_lock_error(e):
                raise
            logger.debug("Skipping data-bytes quota write; SQLite is locked")

    def _connection_id(self) -> Optional[str]:
        connection_id = getattr(self._original, "_bow_connection_id", None)
        return str(connection_id) if connection_id else None

    def _usage_metadata(self, query: str) -> dict:
        return {
            "client_key": self._client_key or getattr(self._original, "_bow_client_key", None),
            "connection_name": getattr(self._original, "_bow_connection_name", None),
            "data_source_id": getattr(self._original, "_bow_data_source_id", None),
            "data_source_name": getattr(self._original, "_bow_data_source_name", None),
            "sql": query[:500] if isinstance(query, str) else None,
        }

    def query(self, query: str, *args, **kwargs):
        """Alias for execute_query.

        Model-generated code often calls `.query(...)` instead of
        `.execute_query(...)`. Route it through our own `execute_query` so the
        call is still captured, timed, and metered — delegating via __getattr__
        would hit the raw client and bypass all of that instrumentation.
        """
        return self.execute_query(query, *args, **kwargs)

    def __getattr__(self, name):
        """Delegate all other attributes to the original client."""
        return getattr(self._original, name)


def wrap_clients_for_capture(
    ds_clients: Dict,
    captured_queries: List[str],
    captured_timings: List[dict],
    usage_context: Optional[UsageLimitContext] = None,
    organization_settings: Optional[OrganizationSettingsConfig] = None,
) -> Dict:
    """Wrap all database clients to capture queries and per-query timing.

    The per-query timeout is resolved per-client so that a single tool
    invocation hitting multiple connections gets the right value for each
    underlying database.
    """
    wrapped = {}
    lazy_enabled = resolve_lazy_enabled(organization_settings)
    for key, client in (ds_clients or {}).items():
        if client is not None and hasattr(client, 'execute_query'):
            wrapped[key] = QueryCapturingClientWrapper(
                client,
                captured_queries,
                captured_timings,
                usage_context=usage_context,
                client_key=str(key),
                query_timeout_seconds=resolve_query_timeout(client, organization_settings),
                lazy_enabled=lazy_enabled,
            )
        else:
            wrapped[key] = client
    return wrapped


class CodeExecutionManager:
    """
    Deprecated shim. Use StreamingCodeExecutor instead.
    Provides only minimal helpers to preserve imports.
    """
    def __init__(self, logger=None, project_manager=None, db=None, report=None, head_completion=None, widget=None, step=None, organization_settings: OrganizationSettingsConfig = None):
        self.logger = logger
        self.organization_settings = organization_settings
        # Other params are ignored; legacy API compatibility only

    async def generate_and_execute_with_retries(self, *args, **kwargs):
        raise RuntimeError("CodeExecutionManager.generate_and_execute_with_retries is deprecated. Use StreamingCodeExecutor.generate_and_execute_stream.")

    def execute_code(self, code: str, db_clients: Dict, excel_files: List, loadables: Optional[Dict] = None):
        executor = StreamingCodeExecutor(organization_settings=self.organization_settings, logger=self.logger)
        return executor.execute_code(code=code, ds_clients=db_clients, excel_files=excel_files, loadables=loadables)

    def format_df_for_widget(self, df: pd.DataFrame, max_rows: Optional[int] = None) -> Dict:
        executor = StreamingCodeExecutor(organization_settings=self.organization_settings, logger=self.logger)
        return executor.format_df_for_widget(df=df, max_rows=max_rows)


class StreamingCodeExecutor:
    """
    Pure, tool-first streaming executor with retries. No project_manager/DB side-effects.
    """
    def __init__(
        self,
        organization_settings: OrganizationSettingsConfig = None,
        logger=None,
        context_hub=None,
        usage_context: Optional[UsageLimitContext] = None,
    ):
        self.organization_settings = organization_settings
        self.logger = logger
        self.context_hub = context_hub
        self.usage_context = usage_context

    def execute_code(self, *, code: str, ds_clients: Dict, excel_files: List,
                     captured_timings: Optional[List[dict]] = None,
                     captured_queries: Optional[List[str]] = None,
                     loadables: Optional[Dict] = None) -> Tuple[pd.DataFrame, str, List[str]]:
        """Execute Python code and return the resulting DataFrame, captured stdout log, and executed queries.

        captured_timings: if provided, per-query wall-clock timings are appended to this list.

        Security:
            - Validates Python code via AST analysis before execution
            - Checks all string literals for dangerous SQL operations (INSERT, DELETE, DROP, etc.)

        Returns:
            Tuple of (DataFrame, stdout_log, executed_queries) where executed_queries
            contains all query strings passed to client.execute_query() during execution.

        Raises:
            UnsafePythonError: If code contains forbidden imports, calls, or attributes
            UnsafeSQLError: If code contains SQL strings with write/modify operations
        """
        with _tracer.start_as_current_span("code_execution.execute_code") as span:
            span.set_attribute("code_execution.code_chars", len(code or ""))
            span.set_attribute("code_execution.clients", len(ds_clients or {}))
            span.set_attribute("code_execution.excel_files", len(excel_files or []))

            # Security: Validate Python code and SQL strings before execution
            validate_python_code(code)

            output_log = ""
            executed_queries: List[str] = captured_queries if captured_queries is not None else []
            _timings: List[dict] = captured_timings if captured_timings is not None else []

            # Wrap clients to capture all queries passed to execute_query
            wrapped_clients = wrap_clients_for_capture(
                ds_clients,
                executed_queries,
                _timings,
                self.usage_context,
                organization_settings=self.organization_settings,
            )

            # Inject a sync HTTP client when the org has web fetch enabled. The
            # client owns concurrency internally so model code never imports
            # asyncio/threading/socket (all of which are AST-forbidden).
            http_client = self._build_http_client()

            # Pre-resolved loadables (see loadables.py). Build pure in-memory
            # lookup closures — no DB/I/O happens inside the sandbox thread.
            load_step, load_entity = self._build_loadable_closures(loadables)

            local_namespace = {
                'pd': pd,
                'np': np,
                'db_clients': wrapped_clients,
                'excel_files': excel_files,
                'load_step': load_step,
                'load_entity': load_entity,
            }
            if http_client is not None:
                local_namespace['http'] = http_client

            if self.logger:
                self.logger.debug(f"Executing code:\n{code}")
            wait_started = _time.monotonic()
            _STDOUT_REDIRECT_LOCK.acquire()
            lock_acquired_at = _time.monotonic()
            try:
                with _tracer.start_as_current_span("code_execution.stdout_lock") as lock_span:
                    lock_span.set_attribute("code_execution.lock_wait_ms", round((lock_acquired_at - wait_started) * 1000.0, 3))
                    lock_span.set_attribute("code_execution.code_chars", len(code or ""))
                    with io.StringIO() as stdout_capture:
                        with redirect_stdout(stdout_capture):
                            exec(code, local_namespace)
                            generate_df = local_namespace.get('generate_df')
                            if not generate_df:
                                raise Exception("No generate_df function found in code")
                            df = self._invoke_generate_df(
                                generate_df, wrapped_clients, excel_files, http_client,
                                load_step=load_step, load_entity=load_entity,
                            )
                            df = self._coerce_exec_result(df)
                        output_log = stdout_capture.getvalue()
                    lock_span.set_attribute("code_execution.lock_held_ms", round((_time.monotonic() - lock_acquired_at) * 1000.0, 3))
            finally:
                _STDOUT_REDIRECT_LOCK.release()
            span.set_attribute("code_execution.query_count", len(executed_queries))
            span.set_attribute("code_execution.stdout_chars", len(output_log or ""))
            return df, output_log, executed_queries

    @staticmethod
    def _coerce_exec_result(df):
        """generate_df must hand back an in-memory DataFrame. A LazyFrame
        would pass the downstream hasattr(df, 'columns') checks and then break
        far away in widget formatting with a non-proximate error the retry
        loop can't act on — so handle it here: materialize small results,
        raise an actionable, retryable error for large ones."""
        from app.data_sources.clients.lazy_frame import LazyFrame

        if not isinstance(df, LazyFrame):
            return df
        cap = int(os.environ.get("BOW_LAZY_RESULT_MATERIALIZE_CAP") or 1_000_000)
        # The hazard is bytes, not just rows: a wide frame under the row cap
        # can still be multiple GB — the exact OOM the lazy path exists to
        # prevent. Estimate from the spill's uncompressed columnar size (for
        # a derived frame this reflects its SOURCE, i.e. an upper bound for
        # plain filters/projections).
        byte_cap = int(os.environ.get("BOW_LAZY_RESULT_MATERIALIZE_MAX_BYTES") or 512 * 1024 * 1024)
        try:
            rows = df.row_count()
            est_bytes = None
            try:
                _, est_bytes, _ = df.spill_stats()
            except Exception:
                logger.debug("Could not estimate LazyFrame result bytes", exc_info=True)
            if rows <= cap and (est_bytes is None or est_bytes <= byte_cap):
                return df.to_df()
            raise ValueError(
                f"generate_df returned a LazyFrame with {rows} rows"
                f"{f' (~{est_bytes} uncompressed bytes)' if est_bytes else ''} — too "
                "large to materialize into the result DataFrame. Reduce it before "
                "returning, e.g. lf.sql('SELECT <aggregation> FROM data GROUP BY "
                f"...').to_df() or lf.limit(n).to_df() with n <= {cap}."
            )
        finally:
            df.close()

    def _build_http_client(self) -> Optional[SafeHttpClient]:
        """Return a SafeHttpClient when `enable_web_fetch` is on, else None."""
        settings = self.organization_settings
        if settings is None:
            return None
        try:
            cfg = settings.get_config("enable_web_fetch")
        except Exception:
            return None
        if cfg is None or not getattr(cfg, "value", False):
            return None
        return SafeHttpClient()

    @staticmethod
    def _build_loadable_closures(loadables: Optional[Dict]):
        """Build pure-lookup `load_step` / `load_entity` over a resolved registry.

        The registry maps the exact literal ref used in the code to a
        DataFrame. A miss raises a clear error naming what's available — it
        only fires for dynamic (non-literal) refs that bypassed pre-resolution.
        """
        reg = loadables or {}
        steps = reg.get("steps") or {}
        entities = reg.get("entities") or {}

        def load_step(id_or_name):
            key = str(id_or_name)
            if key in steps:
                return steps[key].copy()
            raise KeyError(
                f"load_step({key!r}) is not available. "
                f"Loadable steps: {list(steps.keys())}. "
                f"Use a string-literal id or name so it can be pre-loaded."
            )

        def load_entity(id_or_name):
            key = str(id_or_name)
            if key in entities:
                return entities[key].copy()
            raise KeyError(
                f"load_entity({key!r}) is not available. "
                f"Loadable entities: {list(entities.keys())}. "
                f"Use a string-literal id or name so it can be pre-loaded."
            )

        return load_step, load_entity

    @staticmethod
    def _invoke_generate_df(
        fn: Callable, wrapped_clients: Dict, excel_files: List,
        http_client: Optional[SafeHttpClient],
        load_step: Optional[Callable] = None, load_entity: Optional[Callable] = None,
    ):
        """Call generate_df, binding injectables by parameter name.

        `ds_clients` and `excel_files` are always passed positionally. Any of
        `http`, `load_step`, `load_entity` are passed by keyword only when the
        function declares a parameter of that name — so legacy two-arg
        `(ds_clients, excel_files)` and three-arg `(…, http)` signatures keep
        working unchanged.
        """
        injectables = {
            "http": http_client,
            "load_step": load_step,
            "load_entity": load_entity,
        }
        try:
            names = set(inspect.signature(fn).parameters.keys())
        except (TypeError, ValueError):
            names = set()
        kwargs = {k: v for k, v in injectables.items() if k in names}
        return fn(wrapped_clients, excel_files, **kwargs)

    async def execute_code_async(self, *, code: str, ds_clients: Dict, excel_files: List,
                                 captured_timings: Optional[List[dict]] = None,
                                 captured_queries: Optional[List[str]] = None,
                                 loadables: Optional[Dict] = None) -> Tuple[pd.DataFrame, str, List[str]]:
        """Run execute_code in a thread so it doesn't block the event loop."""
        loop = asyncio.get_running_loop()
        if self.usage_context is not None:
            self.usage_context.loop = loop
        with _tracer.start_as_current_span("code_execution.execute_code_async") as span:
            span.set_attribute("code_execution.pool_max_workers", _CODE_EXEC_POOL._max_workers)
            span.set_attribute("code_execution.code_chars", len(code or ""))
            started = _time.monotonic()
            worker_context = contextvars.copy_context()

            def _run_execute_code():
                return worker_context.run(
                    self.execute_code,
                    code=code,
                    ds_clients=ds_clients,
                    excel_files=excel_files,
                    captured_timings=captured_timings,
                    captured_queries=captured_queries,
                    loadables=loadables,
                )

            result = await loop.run_in_executor(
                _CODE_EXEC_POOL,
                _run_execute_code,
            )
            span.set_attribute("code_execution.total_ms", round((_time.monotonic() - started) * 1000.0, 3))
            return result

    def get_df_info(self, df: pd.DataFrame) -> Dict:
        """Extract comprehensive information from a DataFrame."""
        def convert_to_native(obj):
            if isinstance(obj, (np.int64, np.int32, np.int16, np.int8)):
                return int(obj)
            if isinstance(obj, (np.float64, np.float32, np.float16)):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, (np.datetime64, datetime.datetime, datetime.date)):
                return pd.Timestamp(obj).isoformat()
            if isinstance(obj, pd.Timestamp):
                return obj.isoformat()
            if isinstance(obj, datetime.time):
                return obj.isoformat()
            if isinstance(obj, (datetime.timedelta, pd.Timedelta)):
                return str(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, uuid.UUID):
                return str(obj)
            # Fallback for any other non-JSON-serializable types
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)
        def make_hashable(value: Any) -> Any:
            """
            Convert potentially unhashable values (dict, list, set, ndarray, Timestamp)
            into a hashable representation so nunique/value_counts won't crash.
            """
            try:
                # Fast path: already hashable
                hash(value)
                return value
            except Exception:
                pass
            # Normalize common container types
            if isinstance(value, (pd.Timestamp, datetime.date)):
                return pd.Timestamp(value).isoformat()
            if isinstance(value, np.ndarray):
                return tuple(value.tolist())
            if isinstance(value, (list, tuple)):
                try:
                    return tuple(make_hashable(v) for v in value)
                except Exception:
                    return tuple(str(v) for v in value)
            if isinstance(value, set):
                try:
                    return tuple(sorted(make_hashable(v) for v in value))
                except Exception:
                    return tuple(sorted(str(v) for v in value))
            if isinstance(value, dict):
                try:
                    # Stable, readable representation
                    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                except Exception:
                    # Fallback to tuple of items
                    try:
                        return tuple(sorted((str(k), str(v)) for k, v in value.items()))
                    except Exception:
                        return str(value)
            # Final fallback
            try:
                return str(value)
            except Exception:
                return None

        info_dict = {
            "total_rows": int(len(df)),
            "total_columns": int(len(df.columns)),
            "column_info": {},
            "memory_usage": int(df.memory_usage(deep=True).sum()),
            "dtypes_count": {str(k): int(v) for k, v in df.dtypes.value_counts().items()},
        }
        # describe(include='all') may fail on unhashable objects (e.g., dict cells). Guard it.
        try:
            desc_dict = df.describe(include='all').to_dict()
        except Exception:
            desc_dict = {}
        for column in df.columns:
            column_info = {
                "dtype": str(df[column].dtype),
                "non_null_count": int(df[column].count()),
                "memory_usage": int(df[column].memory_usage(deep=True)),
                "null_count": int(df[column].isna().sum()),
                # nunique may fail for unhashable objects; fall back to a hashable projection
                "unique_count": 0,
            }
            try:
                column_info["unique_count"] = int(df[column].nunique(dropna=True))
            except Exception:
                try:
                    projected = df[column].map(make_hashable)
                    column_info["unique_count"] = int(projected.nunique(dropna=True))
                except Exception:
                    column_info["unique_count"] = 0
            if column in desc_dict:
                try:
                    stats = {stat: convert_to_native(value) for stat, value in desc_dict[column].items() if pd.notna(value)}
                    column_info.update(stats)
                except Exception:
                    # Best-effort; skip stats if conversion fails
                    pass
            info_dict["column_info"][column] = column_info
        return info_dict

    def format_df_for_widget(self, df: pd.DataFrame, max_rows: Optional[int] = None) -> Dict:
        """Format a DataFrame into a widget-compatible structure.

        Uses pandas' native JSON serialization which handles datetime, time,
        timedelta, numpy types, NaN/NaT, and other edge cases robustly.

        Args:
            df: The DataFrame to format
            max_rows: Maximum rows to include. If None, uses organization setting
                      'limit_row_count' or defaults to 1000.
        """
        # Determine row limit: None means no limit (disabled)
        row_limit_disabled = False
        if max_rows is None:
            if self.organization_settings is not None:
                try:
                    limit_config = self.organization_settings.get_config("limit_row_count")
                    # "Set to 0 for no limit": a non-positive value means no cap,
                    # regardless of the persisted state flag (the state may be
                    # stored as ENABLED because the schema-level validator that
                    # maps <=0 to DISABLED does not run when a FeatureConfig is
                    # rebuilt through the settings-update path).
                    value = int(limit_config.value)
                    if limit_config.state == FeatureState.DISABLED or value <= 0:
                        row_limit_disabled = True
                    else:
                        max_rows = value
                except (AttributeError, TypeError, ValueError):
                    max_rows = 1000
            else:
                max_rows = 1000
        columns = [{"headerName": str(col), "field": str(col)} for col in df.columns]
        if df.empty:
            rows = []
            df_info = {
                "total_rows": 0,
                "total_columns": int(len(df.columns)),
                "column_info": {str(col): {
                    "dtype": str(df[col].dtype),
                    "non_null_count": 0,
                    "memory_usage": 0,
                    "null_count": 0,
                    "unique_count": 0,
                } for col in df.columns},
                "memory_usage": int(df.memory_usage(deep=True).sum()),
                "dtypes_count": {str(k): int(v) for k, v in df.dtypes.value_counts().items()},
            }
        else:
            # Use pandas' native JSON serialization for robust type handling:
            # - date_format='iso' handles datetime, date, time, Timestamp
            # - default_handler=str catches anything else (UUID, Decimal, etc.)
            df_to_serialize = df if row_limit_disabled else df.head(max_rows)
            rows = json.loads(
                df_to_serialize.to_json(orient='records', date_format='iso', default_handler=str)
            )
            df_info = self.get_df_info(df)
        return {
            "rows": rows,
            "columns": columns,
            "loadingColumn": False,
            "info": df_info,
        }

    async def generate_and_execute_stream(
        self,
        *,
        data_model: Dict,
        prompt: str,
        schemas: str,
        ds_clients: Dict,
        excel_files: List,
        code_context_builder: 'CodeContextBuilder',
        code_generator_fn: Callable,
        max_retries: int = 2,
        sigkill_event=None,
    ):
        """
        Async generator that yields dict events:
          { "type": "progress"|"stdout", "payload": {...} }
        At the end, returns (df, code, code_and_error_messages, execution_log)
        """
        retries = 0
        code_and_error_messages: List[Tuple[str, str]] = []
        final_code = ""
        exec_df = pd.DataFrame()
        execution_log = ""
        executed_successfully = False
        while retries < max_retries:
            # Cooperative cancellation check at loop start
            if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                break

            yield {"type": "progress", "payload": {"stage": "code_generation", "attempt": retries}}
            try:
                # Cancellation before expensive LLM call
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                _t_codegen = _time.monotonic()
                final_code = await code_generator_fn(
                    data_model=data_model,
                    prompt=prompt,
                    schemas=schemas,
                    ds_clients=ds_clients,
                    excel_files=excel_files,
                    code_and_error_messages=code_and_error_messages,
                    memories="",
                    previous_messages="",
                    retries=retries,
                    prev_data_model_code_pair=None,
                    sigkill_event=sigkill_event,
                    code_context_builder=code_context_builder,
                )
                codegen_ms = round((_time.monotonic() - _t_codegen) * 1000.0, 1)
                yield {"type": "progress", "payload": {"stage": "code_generated", "attempt": retries, "code": final_code, "timing": False}}
            except Exception as e:
                msg = f"Code generation error: {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                continue

            # Executing code
            yield {"type": "progress", "payload": {"stage": "data_query_execution", "attempt": retries}}
            try:
                # Cancellation before executing user code
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                _t_exec = _time.monotonic()
                query_timings: List[dict] = []
                exec_df, execution_log, executed_queries = await self.execute_code_async(
                    code=final_code, ds_clients=ds_clients, excel_files=excel_files, captured_timings=query_timings
                )
                execution_ms = round((_time.monotonic() - _t_exec) * 1000.0, 1)
                yield {
                    "type": "progress",
                    "payload": {
                        "stage": "post_execution",
                        "attempt": retries,
                        "execution_ms": execution_ms,
                    },
                }
                executed_successfully = True
                break
            except Exception as e:
                msg = f"Execution error: {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                continue

        # If cancelled, emit a final done with empty results to let caller stop cleanly
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            yield {
                "type": "done",
                "payload": {
                    "df": pd.DataFrame(),
                    "code": final_code,
                    "errors": code_and_error_messages,
                    "execution_log": execution_log,
                    "executed_queries": [],
                    "query_timings": [],
                    "codegen_ms": None,
                    "execution_ms": None,
                },
            }
            return
        else:
            # If we never executed successfully (e.g., validation failed up to max retries),
            # signal failure by returning df=None so callers can treat as error.
            if not executed_successfully and code_and_error_messages:
                yield {
                    "type": "done",
                    "payload": {
                        "df": None,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                        "executed_queries": [],
                        "query_timings": [],
                        "codegen_ms": None,
                        "execution_ms": None,
                    },
                }
            else:
                # Emit a final done event carrying the results instead of returning values
                yield {
                    "type": "done",
                    "payload": {
                        "df": exec_df,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                        "executed_queries": executed_queries,
                        "query_timings": query_timings,
                        "codegen_ms": codegen_ms,
                        "execution_ms": execution_ms,
                    },
                }

    async def generate_and_execute_stream_v2(
        self,
        *,
        request: CodeGenRequest,
        ds_clients: Dict,
        excel_files: List,
        code_context_builder: Optional['CodeContextBuilder'] = None,
        code_generator_fn: Callable = None,
        sigkill_event=None,
        loadable_resolver_fn: Optional[Callable] = None,
    ):
        """
        V2: Typed context-based generator. Yields the same event shapes as v1.
        """
        retries = 0
        # Respect explicit values (including 0→1). `or 2` was swallowing
        # retries=0 and silently running two attempts.
        _req_retries = getattr(request, "retries", None)
        max_retries = max(1, int(_req_retries)) if _req_retries is not None else 2
        code_and_error_messages: List[Tuple[str, str]] = []
        final_code = ""
        exec_df = pd.DataFrame()
        execution_log = ""
        executed_successfully = False
        ctx: CodeGenContext = request.context
        # Derive prompt/schemas for legacy generator signature
        derived_prompt = ctx.user_prompt
        derived_interpreted_prompt = ctx.interpreted_prompt
        derived_schemas = ctx.schemas_excerpt

        # Hoisted so the wrapper's capture survives an exception inside
        # execute_code_async — the failure branch can surface the failing SQL.
        query_timings: List[dict] = []
        executed_queries: List[str] = []

        while retries < max_retries:
            if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                break
            yield {"type": "progress", "payload": {"stage": "code_generation", "attempt": retries}}
            try:
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                # Call code generator with typed context and legacy params populated from context
                _t_codegen = _time.monotonic()
                final_code = await code_generator_fn(
                    data_model={},
                    prompt=derived_prompt,
                    interpreted_prompt=derived_interpreted_prompt,
                    schemas=derived_schemas,
                    ds_clients=ds_clients,
                    excel_files=excel_files,
                    code_and_error_messages=code_and_error_messages,
                    memories="",
                    previous_messages="",
                    retries=retries,
                    prev_data_model_code_pair=None,
                    sigkill_event=sigkill_event,
                    code_context_builder=None,
                    context=ctx,
                )
                codegen_ms = round((_time.monotonic() - _t_codegen) * 1000.0, 1)
                yield {"type": "progress", "payload": {"stage": "code_generated", "attempt": retries, "code": final_code, "timing": False}}
            except Exception as e:
                msg = f"Code generation error: {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                continue

            # Pre-resolve load_step()/load_entity() references before exec. A
            # resolution miss is folded into the error feedback so the coder
            # regenerates (same path as a bad column), rather than failing the
            # sandbox call.
            loadables = None
            if loadable_resolver_fn is not None and final_code:
                try:
                    step_refs, entity_refs = extract_loadable_refs(final_code)
                    if step_refs or entity_refs:
                        resolved = await loadable_resolver_fn(step_refs, entity_refs)
                        loadables = {
                            "steps": resolved.get("steps", {}),
                            "entities": resolved.get("entities", {}),
                        }
                        resolve_errors = resolved.get("errors") or []
                        if resolve_errors:
                            msg = "Loadable resolution failed: " + " | ".join(resolve_errors)
                            code_and_error_messages.append((final_code, msg))
                            yield {"type": "stdout", "payload": msg}
                            retries += 1
                            if retries < max_retries:
                                yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                            continue
                except Exception as e:
                    yield {"type": "stdout", "payload": f"Loadable resolution error: {str(e)}"}

            yield {"type": "progress", "payload": {"stage": "data_query_execution", "attempt": retries}}
            try:
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                _t_exec = _time.monotonic()
                # Fresh per-attempt capture — on success we keep these; on
                # exception the wrapper's partial writes still reach the outer
                # scope so the failure branch can surface the failing SQL / DB error.
                query_timings.clear()
                executed_queries.clear()
                exec_df, execution_log, _ = await self.execute_code_async(
                    code=final_code, ds_clients=ds_clients, excel_files=excel_files,
                    captured_timings=query_timings, captured_queries=executed_queries,
                    loadables=loadables,
                )
                execution_ms = round((_time.monotonic() - _t_exec) * 1000.0, 1)
                yield {
                    "type": "progress",
                    "payload": {
                        "stage": "post_execution",
                        "attempt": retries,
                        "execution_ms": execution_ms,
                    },
                }
                # Treat None/empty-columns DataFrame as a soft failure so the
                # LLM gets a chance to fix defensive stub code that never
                # actually calls execute_query — but only when there's an SQL
                # client or file to query against. URL-fetch-only runs (no
                # ds_clients, no excel_files) legitimately may have nothing
                # to return; the printed output is the deliverable.
                _has_queryable_source = bool(ds_clients) or bool(excel_files)
                if _has_queryable_source and (exec_df is None or not hasattr(exec_df, 'columns') or len(exec_df.columns) == 0):
                    msg = (
                        "Code executed but returned None or an empty DataFrame (0 columns). "
                        "You MUST call ds_clients[\"<client_key>\"].execute_query(...) using the "
                        "EXACT client_key from <connection_clients> and return the resulting DataFrame. "
                        "Do NOT return an empty pd.DataFrame() as a defensive fallback and do NOT "
                        "wrap the query in 'if client is None' branches — the client_key is guaranteed to exist."
                    )
                    code_and_error_messages.append((final_code, msg))
                    yield {"type": "stdout", "payload": msg}
                    retries += 1
                    if retries < max_retries:
                        yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                    continue
                if exec_df is None:
                    exec_df = pd.DataFrame()
                executed_successfully = True
                break
            except CodeSecurityError as e:
                # Tag security violations distinctly so callers can audit them
                violation_type = "unsafe_python" if isinstance(e, UnsafePythonError) else "unsafe_sql"
                msg = f"Security violation ({violation_type}): {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "security_violation", "payload": {"violation_type": violation_type, "message": str(e), "code_snippet": final_code[:500]}}
                yield {"type": "stdout", "payload": msg}
                if violation_type == "unsafe_python":
                    # AST validation runs BEFORE exec() — nothing has executed,
                    # so this is a correctable style problem (e.g. the coder
                    # used getattr()). Feed the violation back and regenerate.
                    retries += 1
                    if retries < max_retries:
                        yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                    continue
                # unsafe_sql fires mid-execution (a write query reached a real
                # client wrapper), so the attempt is not safely repeatable.
                break
            except Exception as e:
                msg = f"Execution error: {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries, "timing": False}}
                continue

        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            yield {
                "type": "done",
                "payload": {
                    "df": pd.DataFrame(),
                    "code": final_code,
                    "errors": code_and_error_messages,
                    "execution_log": execution_log,
                    "executed_queries": [],
                    "query_timings": [],
                    "codegen_ms": None,
                    "execution_ms": None,
                },
            }
            return
        else:
            if not executed_successfully and code_and_error_messages:
                yield {
                    "type": "done",
                    "payload": {
                        "df": None,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                        "executed_queries": executed_queries,
                        "query_timings": query_timings,
                        "codegen_ms": None,
                        "execution_ms": None,
                    },
                }
            else:
                yield {
                    "type": "done",
                    "payload": {
                        "df": exec_df,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                        "executed_queries": executed_queries,
                        "query_timings": query_timings,
                        "codegen_ms": codegen_ms,
                        "execution_ms": execution_ms,
                    },
                }

    async def execute_and_update_step(self,
                              data_model: Dict,
                              code_generator_fn: Callable,
                              db_clients: Dict = None,
                              excel_files: List = None,
                              step=None,  # Optional override for current step
                              **generator_kwargs) -> bool:
        """
        Execute code generation/execution process and update the step with results

        Args:
            data_model: The data model to generate code for
            code_generator_fn: Function that generates code
            db_clients: Database clients
            excel_files: Excel files
            step: Override for the step object (uses self.step if None)
            **generator_kwargs: Additional arguments to pass to code_generator_fn

        Returns:
            Boolean indicating if execution was successful
        """
        # Use provided step or fall back to instance step
        current_step = step or self.step
        if not current_step:
            if self.logger:
                self.logger.error("No step provided for execute_and_update_step")
            return False

        df, final_code, code_and_error_messages = await self.generate_and_execute_with_retries(
            data_model=data_model,
            code_generator_fn=code_generator_fn,
            db_clients=db_clients,
            excel_files=excel_files,
            step=current_step,
            max_retries=self.organization_settings.get_config("limit_code_retries").value,
            **generator_kwargs
        )
        
        # Check if the DataFrame has columns, which indicates success even if empty
        if len(df.columns) > 0:
            # Format the data for widget display
            widget_data = self.format_df_for_widget(df)
            
            # Update step with data
            try:
                await self.project_manager.update_step_with_data(self.db, current_step, widget_data)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error updating step with data: {str(e)}")
                return False
            return True
        else:
            # Handle error case if all retries failed and we have no columns
            try:
                if self.report and self.head_completion and self.widget:
                    await self.project_manager.create_message(
                        report=self.report,
                        db=self.db,
                        message="I faced some issues while generating data. The result had no columns. Can you try explaining again?",
                        status="success",
                        completion=self.head_completion,
                        widget=self.widget,
                        role="ai_agent"
                    )
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error creating error message: {str(e)}")
            return False
