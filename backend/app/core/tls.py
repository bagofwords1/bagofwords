"""One TLS context per process for the LLM providers' HTTP clients.

Every httpx client or transport built without an explicit context calls
``ssl.create_default_context()``, which parses the whole CA bundle (~20-45 ms).
The provider SDKs build a sync and an async client per instance, and the agent
builds a planner and a reporter LLM per message, so each message paid that
four-plus times before its first model call (an Anthropic client measured
178 ms to construct; 2 ms with a shared context).

An ``SSLContext`` is safe to share across clients and threads once configured.
It is built the way httpx builds its default (``SSL_CERT_FILE`` /
``SSL_CERT_DIR`` honored), so verification behavior does not change.
"""
from __future__ import annotations

import functools
import ssl

import httpx


@functools.lru_cache(maxsize=1)
def shared_ssl_context() -> ssl.SSLContext:
    return httpx.create_ssl_context()
