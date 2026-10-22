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
meet. There are three such places, and all of them are covered:

* ``ssl.create_default_context`` — httpx (so httpx-oauth, the OpenAI /
  Anthropic / Google SDKs), OTEL exporters, ldap3, the stdlib.
* ``urllib3.util.ssl_.VERIFY_X509_STRICT`` — urllib3 2.x never calls the
  stdlib factory; it ORs its own copy of the flag into every context it builds
  (``create_urllib3_context``), which is what ``requests``, MSAL /
  azure-identity and ~30 connectors use. The module global is read at call
  time, so replacing it is enough.
* contexts built at import and cached — aiohttp's ``_SSL_CONTEXT_VERIFIED``
  (``aiohttp/connector.py``). Those are patched in place, so the policy holds
  even when applied after the import; ``main.py`` still applies it first.

Relaxing keeps chain verification and hostname checks; only the
extension-presence rules go.

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
import sys

import httpx

try:  # urllib3 is a transitive dependency; keep this module importable without it.
    import urllib3.util.ssl_ as _urllib3_ssl
except ImportError:  # pragma: no cover
    _urllib3_ssl = None

logger = logging.getLogger(__name__)

# Already-built contexts that libraries cache at import time, as
# (module, attribute). Patched in place so call order does not matter.
_CACHED_CONTEXTS = (("aiohttp.connector", "_SSL_CONTEXT_VERIFIED"),)

# The interpreter's own factory, captured once so the policy can be applied
# and reverted (tests) without stacking wrappers.
_create_default_context = ssl.create_default_context


def _relaxed_create_default_context(*args, **kwargs) -> ssl.SSLContext:
    ctx = _create_default_context(*args, **kwargs)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _set_strict(ctx: ssl.SSLContext, strict: bool) -> None:
    if strict:
        ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    else:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT


def configure_process_tls(*, strict_x509: bool) -> None:
    """Apply the verification policy to every context the process builds.

    Idempotent and reversible. Covers the stdlib factory, urllib3's own flag,
    and the contexts libraries cached at import; clears the shared context so
    the next caller gets one built under the new policy. Contexts a client
    built itself and still holds are not touched, so call this before the
    first HTTPS client exists (``main.py`` does, before any other import).
    """
    ssl.create_default_context = (
        _create_default_context if strict_x509 else _relaxed_create_default_context
    )
    if _urllib3_ssl is not None:
        _urllib3_ssl.VERIFY_X509_STRICT = ssl.VERIFY_X509_STRICT if strict_x509 else 0
    for module_name, attr in _CACHED_CONTEXTS:
        ctx = getattr(sys.modules.get(module_name), attr, None)
        if isinstance(ctx, ssl.SSLContext):
            _set_strict(ctx, strict_x509)
    shared_ssl_context.cache_clear()
    if not strict_x509:
        logger.warning(
            "tls.strict_x509_disabled: outbound TLS uses pre-Python-3.13 X.509 "
            "checks (security.tls_strict_x509=false). Chains and hostnames are "
            "still verified; certificate-extension rules are not."
        )


@functools.lru_cache(maxsize=1)
def shared_ssl_context() -> ssl.SSLContext:
    return httpx.create_ssl_context()
