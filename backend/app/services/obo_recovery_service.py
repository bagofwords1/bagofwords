"""Best-effort recovery for an existing Entra login missing connection credentials.

Status reads and query attempts can enqueue background recovery. Completions
make a bounded best-effort attempt before execution. No new client protocol or
browser authorization is introduced; access requires saved delegated credentials.

Recovery is per USER, not per connection: one job obtains the login assertion
once and hands it to `auto_provision_connection_credentials`, which dedupes OBO
exchanges per app/scope and syncs catalogs per data source. A per-connection
job would cost N exchanges, N parallel refreshes of the same refresh token and
an N x N catalog crawl for an agent with N connections.
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from contextlib import suppress

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.connection import Connection
from app.models.oauth_account import OAuthAccount
from app.models.user import User
from app.services.connection_oauth_service import (
    ENTRA_OBO_CONNECTION_TYPES,
    auto_provision_connection_credentials,
    sync_obo_catalogs,
)

logger = logging.getLogger(__name__)
# Process-local admission control; no asyncio primitives shared between loops.
_lock = threading.Lock()
_attempts: OrderedDict[str, float] = OrderedDict()
_running: set[str] = set()
_RETRY_SECONDS = 300
_MAX_TRACKED = 4096
_MAX_RUNNING = 16


def schedule_stored_login_recovery(user_id: str, connection: Connection) -> None:
    """Queue at most one bounded attempt per user per five minutes."""
    if (
        connection.type not in ENTRA_OBO_CONNECTION_TYPES
        or connection.auth_policy != "user_required"
        or "oauth" not in (connection.allowed_user_auth_modes or [])
    ):
        return
    key = str(user_id)
    now = time.monotonic()
    with _lock:
        if key in _running or now - _attempts.get(key, float("-inf")) < _RETRY_SECONDS:
            return
        if len(_running) >= _MAX_RUNNING:
            logger.debug("Stored-login OBO recovery deferred for user %s: worker limit reached", key)
            return
        _attempts[key] = now
        _attempts.move_to_end(key)
        while len(_attempts) > _MAX_TRACKED:
            _attempts.popitem(last=False)
        _running.add(key)
    try:
        from app.services.connection_indexing_service import _get_background_loop

        future = asyncio.run_coroutine_threadsafe(_recover(key), _get_background_loop())

        def finished(_):
            with _lock:
                _running.discard(key)

        future.add_done_callback(finished)
    except Exception:
        with _lock:
            _running.discard(key)
        logger.warning("Could not schedule stored-login OBO recovery")


def assertion_targets(cfg):
    """Only API audiences explicitly requested by this trusted SSO config.

    The SSO client is the caller, not necessarily the recipient of its access
    token. Never discover recipients by trying every connection's credentials.
    Custom URI audiences need an explicit mapping before they can be supported.
    """
    from urllib.parse import urlsplit

    targets = {}
    for scope in cfg.scopes or []:
        resource, _, name = scope.rpartition("/")
        if resource.startswith("api://") and name:
            client_id = resource[len("api://"):]
            if client_id and "/" not in client_id:
                targets.setdefault(client_id, []).append(scope)
    tenant = urlsplit(cfg.issuer).path.strip("/").split("/")[0]
    return tenant, targets


async def _refresh_assertion(db, account, cfg, scopes):
    from app.services.auth_providers import _discover_endpoints

    if not account.refresh_token:
        return None
    issuer = cfg.issuer.rstrip("/")
    well_known = issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
    endpoint = (await _discover_endpoints(well_known))["token_endpoint"]
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": account.refresh_token,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                # Request the original API audience, not a Graph profile token.
                "scope": " ".join(scopes),
            },
        )
        if response.status_code >= 400:
            try:
                codes = [c for c in response.json().get("error_codes", []) if isinstance(c, int)]
            except (ValueError, TypeError, AttributeError):
                codes = []
            logger.warning(
                "Stored-login refresh rejected for user %s: HTTP %s, AADSTS codes %s",
                account.user_id,
                response.status_code,
                codes,
            )
            return None
        tokens = response.json()
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    account.access_token = access
    account.expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
    if tokens.get("refresh_token"):
        account.refresh_token = tokens["refresh_token"]
    await db.commit()
    return access


async def recover_stored_login(db, user, *, organization_id=None):
    """Bounded, per-user recovery shared by status jobs and prompt preflight.

    Absence of a refresh token is normal: a usable assertion can still be
    exchanged. Without refresh, an expired/wrong-resource assertion needs SSO.
    Returns False when another worker already owns recovery.
    """
    from app.services.auth_providers import _get_oidc_config, _is_entra_provider
    from app.services.credential_coordination import provisioning_lock

    pending_catalogs = []
    async with provisioning_lock(db, str(user.id)) as acquired:
        if not acquired:
            return False
        if not user.is_active:
            return True
        accounts = (await db.scalars(select(OAuthAccount).where(OAuthAccount.user_id == str(user.id)))).all()
        for account in accounts:
            cfg = _get_oidc_config(account.oauth_name)
            if not cfg or not cfg.enabled or not _is_entra_provider(account.oauth_name) or not cfg.client_id or not cfg.client_secret:
                continue
            tenant, targets = assertion_targets(cfg)
            for client_id, scopes in targets.items():
                async def provision(assertion, client_id=client_id, tenant=tenant):
                    return await auto_provision_connection_credentials(
                        db, user, assertion, missing_only=True,
                        client_id=client_id, tenant_id=tenant,
                        organization_id=organization_id, catalog_targets=pending_catalogs,
                    )

                summary = None
                if account.access_token and (not account.expires_at or account.expires_at > time.time() + 60):
                    with suppress(ValueError, httpx.HTTPError):
                        summary = await provision(account.access_token)
                if summary is None or summary["failed"]:
                    assertion = await _refresh_assertion(db, account, cfg, scopes)
                    if assertion:
                        summary = await provision(assertion)
                if summary and summary["provisioned"]:
                    logger.info("Stored-login OBO recovered %d connection(s) for user %s", len(summary["provisioned"]), user.id)
    await sync_obo_catalogs(db, user, pending_catalogs)
    return True


async def _recover_report_connections(db, user, organization, report_id):
    """Best-effort recovery for connections visible to this prompt.

    Only visible, selected agents and explicitly configured Entra audiences
    participate. Explicit Disconnect/service-account choices are left alone.
    """
    from sqlalchemy.orm import selectinload

    from app.ai.tools.implementations.agent_focus_common import accessible_agents
    from app.models.report import Report
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.auth_providers import _get_oidc_config, _is_entra_provider

    report = await db.scalar(select(Report).where(
        Report.id == report_id, Report.organization_id == str(organization.id),
    ).options(selectinload(Report.data_sources)))
    if not report or report.report_type == "artifact_chat":
        return None
    accounts = (await db.scalars(select(OAuthAccount).where(OAuthAccount.user_id == str(user.id)))).all()
    targets = set()
    for account in accounts:
        cfg = _get_oidc_config(account.oauth_name)
        if cfg and cfg.enabled and _is_entra_provider(account.oauth_name):
            tenant, clients = assertion_targets(cfg)
            targets.update((tenant, client) for client in clients)
    if not targets:
        return None
    selected = {str(ds.id) for ds in report.data_sources}
    candidates = {}
    for ds in await accessible_agents(db, organization, user):
        if selected and str(ds.id) not in selected:
            continue
        for conn in ds.connections:
            if (str(conn.organization_id) != str(organization.id) or not conn.is_active or conn.deleted_at
                    or conn.type not in ENTRA_OBO_CONNECTION_TYPES or conn.auth_policy != "user_required"
                    or "oauth" not in (conn.allowed_user_auth_modes or [])):
                continue
            creds = conn.decrypt_credentials() or {}
            if (creds.get("tenant_id"), creds.get("oauth_client_id") or creds.get("client_id")) in targets:
                candidates[str(conn.id)] = conn
    if not candidates:
        return None

    async def missing():
        # Any persisted choice, including Disconnect, belongs to the user.
        present = set(await db.scalars(select(UserConnectionCredentials.connection_id).where(
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.connection_id.in_(candidates),
        )))
        return sorted(set(candidates) - present)

    if not await missing():
        return None
    await recover_stored_login(db, user, organization_id=str(organization.id))


async def recover_report_connections(db, user, organization, report_id):
    """Recover before execution without changing ordinary completion responses.

    Invalid/expired assertions without a refresh token remain unauthenticated.
    The existing connection access checks still apply; never fall back to system
    credentials or initiate a browser redirect.
    """
    from app.models.organization import Organization

    user_id, organization_id = str(user.id), str(organization.id)
    try:
        async with asyncio.timeout(20):
            # Isolate recovery commits/rollbacks from the completion request's
            # ORM objects, especially when the bounded attempt is cancelled.
            maker = async_sessionmaker(db.bind, expire_on_commit=False)
            async with maker() as recovery_db:
                recovery_user = await recovery_db.get(User, user_id)
                recovery_org = await recovery_db.get(Organization, organization_id)
                if recovery_user and recovery_org:
                    await _recover_report_connections(recovery_db, recovery_user, recovery_org, report_id)
    except Exception as exc:
        logger.warning("Prompt OBO recovery unavailable for user %s (%s)", user_id, type(exc).__name__)


async def _recover(user_id):
    from app.settings.database import create_async_database_engine_for_indexing

    engine = None
    try:
        # Includes token HTTP, database work, and catalog discovery. Failures
        # leave the normal Connect flow intact; no service-account fallback.
        async with asyncio.timeout(120):
            engine = create_async_database_engine_for_indexing()
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                user = await db.get(User, user_id)
                if user:
                    await recover_stored_login(db, user)
    except Exception as exc:
        # Do not log assertions, refresh tokens, response bodies, or exception
        # messages (providers can echo credentials in them).
        logger.warning(
            "Stored-login OBO recovery failed for user %s (%s); sign-in remains required",
            user_id,
            type(exc).__name__,
        )
    finally:
        if engine is not None:
            await engine.dispose()
