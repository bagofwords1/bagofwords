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
