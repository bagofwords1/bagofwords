"""Process-wide TLS policy and the shared client context.

Two things live here:

``configure_process_tls`` applies the operator's verification policy to every
``SSLContext`` the process builds afterwards. Since Python 3.13,
``ssl.create_default_context()`` turns on OpenSSL's strict X.509 checks
(``VERIFY_X509_STRICT``), which reject any CA-issued certificate that lacks an
Authority Key Identifier. A TLS-inspecting corporate proxy that re-signs
traffic with a hand-made CA then breaks every outbound HTTPS call (OIDC
discovery, Graph, the LLM providers, OTEL exporters). Those libraries build
their own clients, and httpx-oauth builds one inside ``OpenID.__init__`` with
no way to pass a context in, so the policy has to be applied where they all
meet: the ``ssl.create_default_context`` factory. Relaxing it keeps chain
verification and hostname checks; only the extension-presence rules go.

``shared_ssl_context`` is one context per process for the LLM providers' HTTP
clients. Every httpx client or transport built without an explicit context
parses the whole CA bundle (~20-45 ms); the provider SDKs build a sync and an
async client per instance, and the agent builds a planner and a reporter LLM
per message, so each message paid that four-plus times before its first model
call (an Anthropic client measured 178 ms to construct; 2 ms with a shared
context). An ``SSLContext`` is safe to share across clients and threads once
configured. It is built the way httpx builds its default (``SSL_CERT_FILE`` /
``SSL_CERT_DIR`` honored), so verification behavior does not change.
"""
from __future__ import annotations

import functools
import logging
import ssl

import httpx

logger = logging.getLogger(__name__)

# The interpreter's own factory, captured once so the policy can be applied
# and reverted (tests) without stacking wrappers.
_create_default_context = ssl.create_default_context


def _relaxed_create_default_context(*args, **kwargs) -> ssl.SSLContext:
    ctx = _create_default_context(*args, **kwargs)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def configure_process_tls(*, strict_x509: bool) -> None:
    """Apply the verification policy to every context built from now on.

    Idempotent. Call before the first HTTPS client is built; contexts that
    already exist (including a cached ``shared_ssl_context``) are not touched.
    """
    if strict_x509:
        ssl.create_default_context = _create_default_context
        return
    ssl.create_default_context = _relaxed_create_default_context
    logger.warning(
        "tls.strict_x509_disabled: outbound TLS uses pre-Python-3.13 X.509 "
        "checks (security.tls_strict_x509=false). Chains and hostnames are "
        "still verified; certificate-extension rules are not."
    )


@functools.lru_cache(maxsize=1)
def shared_ssl_context() -> ssl.SSLContext:
    return httpx.create_ssl_context()
