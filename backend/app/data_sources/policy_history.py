"""Associate parameterized outbound SQL with its existing connection execution."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

_handle = ContextVar("protected_query_execution", default=None)


@contextmanager
def track_policy(handle):
    token = _handle.set(handle)
    try:
        yield
    finally:
        _handle.reset(token)


def record_policy(policy, statement=None):
    handle = _handle.get()
    if handle is None:
        return
    digest = hashlib.sha256(json.dumps(policy.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    handle.policy_version = digest
    handle.protected_statement = statement


def record_protected_execution(method):
    """Include catalog previews which enter a bound client outside the agent executor."""
    from functools import wraps
    from app.data_sources import query_executions
    from app.data_sources.query_concurrency import DEFAULT_MAX_CONCURRENT_QUERIES

    @wraps(method)
    def execute(client, sql, policy, employee, identity=None, *, preview_columns=None):
        scope = getattr(client, "_policy_audit_scope", None)
        if _handle.get() is not None or not scope:
            return method(client, sql, policy, employee, identity, preview_columns=preview_columns)
        connection, organization, user = scope
        handle = query_executions.begin(
            connection_id=connection,
            limit=DEFAULT_MAX_CONCURRENT_QUERIES,
            wait_seconds=30,
            query_text=sql,
            meta={
                "organization_id": organization,
                "user_id": user,
                "client_type": type(client).__name__,
                "source": "protected_preview",
                "killable": 0,
            },
        )
        if handle is None:
            raise ValueError("Protected connection unavailable")
        with track_policy(handle):
            try:
                result = method(client, sql, policy, employee, identity, preview_columns=preview_columns)
            except Exception:
                query_executions.finish(handle, "error", error="Protected query unavailable")
                raise
            query_executions.finish(handle, "success", rows=len(result.index))
            return result

    return execute
