"""Best-effort recovery for an existing Entra login missing connection credentials.

Status reads and query attempts can enqueue recovery; neither waits for network
IO nor gains access until delegated credentials have actually been persisted.

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


async def _refresh_assertion(db, account, cfg):
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
                "scope": " ".join(cfg.scopes),
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


async def _recover(user_id):
    from app.services.auth_providers import _get_oidc_config, _is_entra_provider
    from app.settings.database import create_async_database_engine_for_indexing

    engine = None
    try:
        # Includes token HTTP, database work, and catalog discovery. Failures
        # leave the normal Connect flow intact; no service-account fallback.
        async with asyncio.timeout(120):
            engine = create_async_database_engine_for_indexing()
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                from app.services.credential_coordination import provisioning_lock

                pending_catalogs = []
                async with provisioning_lock(db, user_id) as acquired:
                    if not acquired:
                        return
                    user = await db.get(User, user_id)
                    if not user or not user.is_active:
                        return
                    accounts = (await db.scalars(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).all()
                    for account in accounts:
                        cfg = _get_oidc_config(account.oauth_name)
                        if (
                            not cfg
                            or not cfg.enabled
                            or not _is_entra_provider(account.oauth_name)
                            or not cfg.client_id
                            or not cfg.client_secret
                        ):
                            continue

                        async def provision(assertion, cfg=cfg):
                            # missing_only: current orgs only, never a row that
                            # already exists (manual connect, Disconnect marker,
                            # service-account choice), only this login's app.
                            return await auto_provision_connection_credentials(
                                db,
                                user,
                                assertion,
                                missing_only=True,
                                client_id=cfg.client_id,
                                catalog_targets=pending_catalogs,
                            )

                        summary = None
                        if account.access_token and (not account.expires_at or account.expires_at > time.time() + 60):
                            # A Graph profile token may have replaced the login
                            # assertion. If rejected, refresh original scopes once.
                            with suppress(ValueError, httpx.HTTPError):
                                summary = await provision(account.access_token)
                        if summary is None or (summary["failed"] and not summary["provisioned"]):
                            assertion = await _refresh_assertion(db, account, cfg)
                            if assertion:
                                summary = await provision(assertion)
                        if summary and summary["provisioned"]:
                            logger.info(
                                "Stored-login OBO recovered %d connection(s) for user %s",
                                len(summary["provisioned"]),
                                user_id,
                            )
                await sync_obo_catalogs(db, user, pending_catalogs)
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
