"""Keep provider debug payloads out of logs for private artifact operations."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

_private = ContextVar("private_artifact_provider_logs", default=False)


class _PrivateProviderFilter(logging.Filter):
    def filter(self, record):
        return not _private.get()


_filter = _PrivateProviderFilter()


@contextmanager
def private_provider_logs(enabled):
    if not enabled:
        yield
        return
    # SDK debug logs include request bodies. Install once per known SDK logger;
    # ContextVar scoping preserves unrelated calls running on the same worker.
    names = {
        f"{provider}.{module}"
        for provider in ("openai", "anthropic")
        for module in ("_base_client", "_response", "_legacy_response")
    }
    names.update(
        name
        for name in logging.Logger.manager.loggerDict
        if name.startswith(("openai.", "anthropic.", "httpcore.")) or name in ("httpx", "httpcore")
    )
    for name in names:
        logger = logging.getLogger(name)
        if _filter not in logger.filters:
            logger.addFilter(_filter)
    token = _private.set(True)
    try:
        yield
    finally:
        _private.reset(token)
