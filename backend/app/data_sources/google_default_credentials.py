"""Keyless Google Cloud auth for data-source clients.

A connection normally authenticates to Google with a service-account key the
admin pastes into the form. That key is a long-lived secret that has to be
copied around and rotated by hand. This module is the alternative: the
connection authenticates as the *deployment itself*, through Google's
Application Default Credentials (ADC) chain, and stores no secret at all.

What ADC resolves to is decided entirely by the operator, outside the app:

  * a service account attached to the VM / pod (GCE metadata server, GKE
    Workload Identity);
  * a Workload Identity Federation config file named by
    ``GOOGLE_APPLICATION_CREDENTIALS`` — the deployment proves who it is with
    an identity it already has (AWS role, Azure managed identity, a Kubernetes
    or other OIDC token, an X.509 certificate) and Google exchanges that for
    short-lived tokens;
  * a developer's ``gcloud auth application-default login``.

Optionally the resolved identity impersonates a *target* service account, so
connections on one deployment can carry different permissions while still
holding no key. The deployment identity then needs
``roles/iam.serviceAccountTokenCreator`` on the target.

Two rules keep this safe:

  * **Opt-in per deployment.** The ambient identity belongs to whoever runs the
    server, not to whoever fills in a connection form. It is refused unless the
    operator set ``security.allow_google_default_credentials``
    (``BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true``).
  * **No credential configuration through the app.** A federation config names
    files, URLs and executables the auth library will read or run, and the
    library does not validate it. It is only ever read from the path the
    operator put in the environment — never from a form field or the database.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from typing import Any

from app.settings.logging_config import get_logger

logger = get_logger(__name__)

ENABLE_ENV_VAR = "BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS"

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# The target is interpolated into the IAM Credentials URL by google-auth, so
# only a plain service-account address is accepted: no path, query or
# whitespace can ride along.
_SERVICE_ACCOUNT_EMAIL = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,180})\.gserviceaccount\.com$"
)

_CACHE: dict[tuple, Any] = {}
_CACHE_LOCK = threading.Lock()


class GoogleDefaultCredentialsError(ValueError):
    """Keyless Google auth could not be used. The message is written for the
    admin looking at the connection form and names the fix."""


def default_credentials_allowed() -> bool:
    """Whether the operator has let connections use the deployment's own
    Google identity."""
    try:
        from app.settings.config import settings

        return bool(settings.bow_config.security.allow_google_default_credentials)
    except Exception:
        return False


def require_default_credentials_allowed() -> None:
    if not default_credentials_allowed():
        raise GoogleDefaultCredentialsError(
            "Application Default Credentials are turned off on this deployment. "
            f"Ask the operator to set {ENABLE_ENV_VAR}=true (or "
            "security.allow_google_default_credentials in bow-config.yaml) and restart."
        )


def normalize_target_service_account(value: str | None) -> str | None:
    """A cleaned service-account email, or None when no target was given.
    Raises ``GoogleDefaultCredentialsError`` for anything else."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise GoogleDefaultCredentialsError("Service account to impersonate must be an email address.")
    email = value.strip()
    if not email:
        return None
    if not _SERVICE_ACCOUNT_EMAIL.match(email):
        raise GoogleDefaultCredentialsError(
            "Service account to impersonate must be a service account email, "
            "e.g. reader@my-project.iam.gserviceaccount.com."
        )
    return email


def _environment_fingerprint() -> tuple:
    """Identifies what ADC currently points at, so a replaced credential file
    (a rotated federation config, a re-mounted secret) is picked up without a
    restart instead of being served from the cache."""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    stamp: Any = None
    if path:
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = "missing"
    return (path, stamp)


def resolve_default_credentials(
    scopes: Iterable[str] = (CLOUD_PLATFORM_SCOPE,),
    target_service_account: str | None = None,
):
    """Return self-refreshing Google credentials for the deployment identity,
    impersonating ``target_service_account`` when one is given.

    May touch the network (metadata server, token exchange), so call it from a
    worker thread, not the event loop. Credentials are cached per
    (environment, target, scopes): a client is built per query, and without the
    cache every query would repeat the lookup and mint a new token.
    """
    require_default_credentials_allowed()
    target = normalize_target_service_account(target_service_account)
    scope_list = tuple(scopes)
    key = (_environment_fingerprint(), target, scope_list)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    import google.auth
    from google.auth import exceptions as google_exceptions

    try:
        # The source identity always asks for cloud-platform: that is what lets
        # it call the IAM Credentials API when a target is impersonated.
        source_scopes = scope_list if not target else (CLOUD_PLATFORM_SCOPE,)
        credentials, _project = google.auth.default(scopes=list(source_scopes))
    except google_exceptions.DefaultCredentialsError as e:
        raise GoogleDefaultCredentialsError(
            "No Application Default Credentials were found on this deployment. "
            "Attach a service account to the server, or point "
            "GOOGLE_APPLICATION_CREDENTIALS at a Workload Identity Federation "
            f"credential configuration file. ({e})"
        ) from e

    if target:
        from google.auth import impersonated_credentials

        credentials = impersonated_credentials.Credentials(
            source_credentials=credentials,
            target_principal=target,
            target_scopes=list(scope_list),
        )

    with _CACHE_LOCK:
        # Another thread may have resolved the same identity meanwhile; keep
        # one object so they share its token.
        credentials = _CACHE.setdefault(key, credentials)
        # Entries for a credential file that has since been replaced are dead.
        current = key[0]
        for stale in [k for k in _CACHE if k[0] != current]:
            _CACHE.pop(stale, None)
    logger.info(
        "google default credentials resolved (impersonating=%s)", bool(target)
    )
    return credentials


def _google_error_detail(error: Exception) -> str:
    """Google's own one-line reason. google-auth packs the HTTP response body
    into the exception arguments; the nested JSON is noise in a form."""
    import json

    for arg in reversed(getattr(error, "args", ()) or ()):
        payload = arg
        if isinstance(arg, str):
            try:
                payload = json.loads(arg)
            except ValueError:
                continue
        if isinstance(payload, dict):
            inner = payload.get("error")
            message = inner.get("message") if isinstance(inner, dict) else payload.get("error_description") or inner
            if isinstance(message, str) and message:
                return message
    args = getattr(error, "args", ()) or ()
    return str(args[0]) if args and isinstance(args[0], str) else str(error)


def explain_refresh_error(error: Exception, target_service_account: str | None = None) -> str | None:
    """Turn google-auth's token failures into something an admin can act on.
    Returns None for errors that are not about obtaining a token."""
    try:
        from google.auth import exceptions as google_exceptions
    except Exception:
        return None
    if not isinstance(error, google_exceptions.GoogleAuthError):
        return None
    detail = _google_error_detail(error)
    if target_service_account and "impersonated" in str(error).lower():
        return (
            f"Could not impersonate {target_service_account}. Grant the deployment's "
            "identity the Service Account Token Creator role "
            "(roles/iam.serviceAccountTokenCreator) on that service account and make "
            f"sure the IAM Service Account Credentials API is enabled. ({detail})"
        )
    return (
        "The deployment's Google identity could not obtain an access token. "
        f"Check the Application Default Credentials configured on the server. ({detail})"
    )


def reset_cache() -> None:
    """Drop cached credentials. For tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
