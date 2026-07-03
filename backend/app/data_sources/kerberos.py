"""Kerberos credential management for data source clients.

Two auth shapes are supported:

* **Service identity** — the app authenticates as itself. Either the process
  default credential cache is used untouched (populated by ``kinit`` or, better,
  ``KRB5_CLIENT_KTNAME`` pointing at the service keytab so GSSAPI initiates
  from the keytab with no renewal cron), or a specific principal is resolved
  from the client keytab into a dedicated cache.

* **Delegated identity (KCD / protocol transition)** — the app impersonates an
  end user via S4U2Self ("get a ticket *for user X* to myself"; needs only the
  UPN, no password) and the resulting proxy credential performs S4U2Proxy when
  the SQL driver initiates a context to ``MSSQLSvc/...``. Requires the service
  account to be trusted for constrained delegation with protocol transition in
  Active Directory (``msDS-AllowedToDelegateTo`` + "Use any authentication
  protocol", or resource-based delegation on the SQL service account).

The MS ODBC driver on Linux consumes whatever credential cache ``KRB5CCNAME``
points at when the connection is opened, and there is no per-connection ODBC
keyword to select a cache. ``KRB5CCNAME`` is process-global, so activation is
serialized under a lock held only for the duration of the driver connect —
established connections keep their authenticated session afterwards.

python-gssapi is imported lazily: Phase-A service auth via the default cache
needs no Python Kerberos bindings at all (the ODBC driver talks to libgssapi
directly), so the app must keep working without the ``gssapi`` package.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Serializes KRB5CCNAME mutation across the whole process. Concurrent connects
# as different users queue on this lock; once the driver has opened the
# connection the env var no longer matters, so the hold time is one TCP+GSS
# handshake.
_ENV_LOCK = threading.Lock()

# Refresh a cached credential when less than this many seconds of its ticket
# lifetime remain, so a connection never starts with an about-to-expire ticket.
_REFRESH_MARGIN_SECONDS = 120
# Fallback lifetime when GSSAPI doesn't report one (0 / None): re-acquire
# every 10 minutes rather than trusting an unknown expiry.
_DEFAULT_LIFETIME_SECONDS = 600

CCACHE_DIR_ENV = "BOW_KRB5_CCACHE_DIR"
CLIENT_KEYTAB_ENV = "KRB5_CLIENT_KTNAME"


class KerberosConfigurationError(RuntimeError):
    """Kerberos support is not available or not configured on this deployment."""


class KerberosDelegationError(RuntimeError):
    """Obtaining a (delegated) Kerberos credential failed."""


def _import_gssapi():
    try:
        import gssapi  # noqa: PLC0415
        return gssapi
    except ImportError as e:
        raise KerberosConfigurationError(
            "Kerberos support requires the 'gssapi' Python package and MIT krb5 "
            "libraries (krb5-user, libgssapi-krb5-2). Install them and mount a "
            "krb5.conf plus the service keytab."
        ) from e


@dataclass
class _CachedCredential:
    ccache_path: str
    expires_at: float


class KerberosTicketManager:
    """Acquires and caches Kerberos credentials as file-based credential caches.

    One ccache file per principal, refreshed when close to expiry. Thread-safe.
    """

    def __init__(self, ccache_dir: str | None = None, client_keytab: str | None = None):
        self._ccache_dir = ccache_dir or os.environ.get(CCACHE_DIR_ENV) or "/tmp/bow_krb5"
        self._client_keytab = client_keytab or os.environ.get(CLIENT_KEYTAB_ENV)
        self._lock = threading.Lock()
        self._cache: dict[str, _CachedCredential] = {}

    # ── public API ──────────────────────────────────────────────────────────

    def service_ccache(self, principal: str | None = None) -> str | None:
        """Credential cache for the app's own identity.

        With no explicit principal the process default cache is used — returns
        None, meaning "leave KRB5CCNAME alone".
        """
        if not principal:
            return None
        return self._get_or_acquire(f"svc:{principal}", lambda: self._acquire_service(principal))

    def delegated_ccache(self, user_principal: str) -> str:
        """Credential cache holding an S4U2Self proxy credential for the user.

        The credential is acquired with the service account's keytab identity
        impersonating ``user_principal`` (UPN — no password involved). When the
        SQL driver later initiates a security context from this cache toward the
        SQL Server SPN, MIT krb5 transparently performs S4U2Proxy.
        """
        principal = (user_principal or "").strip()
        if not principal or "@" not in principal:
            raise KerberosDelegationError(
                f"Cannot impersonate '{user_principal}': a full user principal name "
                "(user@REALM) is required for Kerberos delegation."
            )
        return self._get_or_acquire(f"user:{principal}", lambda: self._acquire_delegated(principal))

    @contextmanager
    def activate(self, ccache_path: str | None):
        """Point KRB5CCNAME at ``ccache_path`` for the duration of the block.

        Process-global and therefore serialized: hold only around the driver
        connect call, never around query execution. A ``None`` path activates
        nothing (default cache) without taking the lock.
        """
        if not ccache_path:
            yield
            return
        with _ENV_LOCK:
            previous = os.environ.get("KRB5CCNAME")
            os.environ["KRB5CCNAME"] = f"FILE:{ccache_path}"
            try:
                yield
            finally:
                if previous is None:
                    os.environ.pop("KRB5CCNAME", None)
                else:
                    os.environ["KRB5CCNAME"] = previous

    def invalidate(self, user_principal: str | None = None) -> None:
        """Drop cached credentials (all, or one user's) so the next use re-acquires."""
        with self._lock:
            if user_principal is None:
                self._cache.clear()
            else:
                self._cache.pop(f"user:{user_principal.strip()}", None)

    # ── internals ───────────────────────────────────────────────────────────

    def _get_or_acquire(self, key: str, acquire) -> str:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                return cached.ccache_path
            ccache_path, lifetime = acquire()
            lifetime = lifetime or _DEFAULT_LIFETIME_SECONDS
            self._cache[key] = _CachedCredential(ccache_path=ccache_path, expires_at=now + lifetime)
            return ccache_path

    def _ccache_path_for(self, key: str) -> str:
        os.makedirs(self._ccache_dir, mode=0o700, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        token = base64.urlsafe_b64encode(digest[:12]).decode("ascii").rstrip("=")
        return os.path.join(self._ccache_dir, f"krb5cc_{token}")

    def _service_credentials(self, gssapi, principal: str | None = None):
        """Initiate-usage credentials for the app's service identity (keytab)."""
        store = {}
        if self._client_keytab:
            store["client_keytab"] = self._client_keytab
        name = None
        if principal:
            name = gssapi.Name(principal, gssapi.NameType.kerberos_principal)
        try:
            if store:
                return gssapi.Credentials(usage="initiate", name=name, store=store)
            return gssapi.Credentials(usage="initiate", name=name)
        except Exception as e:
            raise KerberosDelegationError(
                f"Failed to acquire service credentials"
                f"{f' for {principal}' if principal else ''}: {e}. "
                f"Check the keytab ({self._client_keytab or 'default ccache'}) and krb5.conf."
            ) from e

    def _acquire_service(self, principal: str) -> tuple[str, int | None]:
        gssapi = _import_gssapi()
        creds = self._service_credentials(gssapi, principal)
        return self._store_into_ccache(gssapi, creds, f"svc:{principal}")

    def _acquire_delegated(self, user_principal: str) -> tuple[str, int | None]:
        gssapi = _import_gssapi()
        impersonator = self._service_credentials(gssapi)
        # Enterprise names let AD resolve UPN suffixes that differ from the
        # realm name; fall back to plain principal parsing on older bindings.
        name_type = getattr(gssapi.NameType, "enterprise_principal", None) \
            or gssapi.NameType.kerberos_principal
        user_name = gssapi.Name(user_principal, name_type)
        try:
            result = gssapi.raw.acquire_cred_impersonate_name(
                impersonator, user_name, usage="initiate"
            )
        except Exception as e:
            raise KerberosDelegationError(
                f"S4U2Self impersonation of '{user_principal}' failed: {e}. "
                "Verify the service account is trusted for constrained delegation "
                "with protocol transition ('Use any authentication protocol') and "
                "that the user is not marked sensitive/Protected Users."
            ) from e
        lifetime = getattr(result, "lifetime", None)
        path, _ = self._store_into_ccache(gssapi, result.creds, f"user:{user_principal}")
        return path, lifetime

    def _store_into_ccache(self, gssapi, creds, key: str) -> tuple[str, int | None]:
        path = self._ccache_path_for(key)
        try:
            gssapi.raw.store_cred_into(
                {"ccache": f"FILE:{path}"},
                creds,
                usage="initiate",
                overwrite=True,
                set_default=False,
            )
        except Exception as e:
            raise KerberosDelegationError(f"Failed to store Kerberos credential cache: {e}") from e
        with suppress(OSError):
            os.chmod(path, 0o600)
        lifetime = getattr(creds, "lifetime", None)
        return path, lifetime


_manager: KerberosTicketManager | None = None
_manager_lock = threading.Lock()


def get_ticket_manager() -> KerberosTicketManager:
    """Process-wide ticket manager singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = KerberosTicketManager()
    return _manager
