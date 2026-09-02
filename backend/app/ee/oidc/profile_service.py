# Entra ID profile / job-info sync service.
# Licensed under the Bag of Words Enterprise License
#
# Fetches the signed-in user's Microsoft Graph /me profile (job title,
# department, etc.) and persists the selected fields on their per-org
# Membership. Everything here uses the default-granted delegated User.Read
# scope — no admin consent required.

import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.oauth_account import OAuthAccount
from app.ee.oidc.graph_client import resolve_user_profile

logger = logging.getLogger(__name__)

# Scope requested when refreshing a login token specifically for a Graph /me
# call. offline_access keeps the refresh token rolling.
_GRAPH_REFRESH_SCOPE = "openid profile email https://graph.microsoft.com/User.Read offline_access"

# Scope requested when OBO-exchanging a login token for a Graph /me call.
# OBO requests must target a single resource, so no openid/profile/email here.
_GRAPH_OBO_SCOPE = "https://graph.microsoft.com/User.Read offline_access"


class EntraReauthRequired(Exception):
    """The stored Entra tokens can no longer produce a usable Graph token —
    the user has to sign in with Entra ID again."""


async def _entra_oauth_account(db: AsyncSession, user_id: str) -> Optional[OAuthAccount]:
    """Return the user's Entra-based OAuth login account, if any."""
    from app.services.auth_providers import _is_entra_provider

    rows = (
        await db.execute(
            select(OAuthAccount).where(OAuthAccount.user_id == str(user_id))
        )
    ).scalars().all()
    for acc in rows:
        try:
            if _is_entra_provider(acc.oauth_name):
                return acc
        except Exception:
            continue
    return None


async def get_entra_graph_token(db: AsyncSession, user) -> Optional[str]:
    """Return a usable Graph access token for the user, refreshing if expired.

    Reads the token persisted at login (fastapi-users ``oauth_accounts``). When
    it has expired and a refresh token is available, exchanges it for a fresh
    Graph-scoped token and updates the stored credentials. Returns None when the
    user has no Entra login on file.
    """
    acc = await _entra_oauth_account(db, str(user.id))
    if not acc:
        return None

    now = int(time.time())
    if acc.access_token and (not acc.expires_at or int(acc.expires_at) > now + 60):
        return acc.access_token

    if not acc.refresh_token:
        # Nothing to refresh with — hand back whatever we have and let the
        # caller surface a 401 rather than silently returning None.
        return acc.access_token

    from app.services.auth_providers import _get_oidc_config, _discover_endpoints

    cfg = _get_oidc_config(acc.oauth_name)
    if not cfg or not (cfg.client_id and cfg.client_secret and cfg.issuer):
        return acc.access_token

    issuer = cfg.issuer.rstrip("/")
    well_known = issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
    try:
        token_endpoint = (await _discover_endpoints(well_known))["token_endpoint"]
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": acc.refresh_token,
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "scope": _GRAPH_REFRESH_SCOPE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token = resp.json()
    except Exception as e:
        logger.warning(f"Entra token refresh failed for user {user.id}: {e}")
        return acc.access_token

    new_access = token.get("access_token")
    if new_access:
        acc.access_token = new_access
        if token.get("refresh_token"):
            acc.refresh_token = token["refresh_token"]
        expires_in = token.get("expires_in")
        if isinstance(expires_in, int):
            acc.expires_at = now + expires_in
        db.add(acc)
        await db.commit()
        return new_access

    return acc.access_token


async def _obo_exchange_for_graph(oauth_name: str, assertion: str) -> Optional[dict]:
    """Exchange a login access token for a Graph-audience token via OBO.

    Entra configs that also drive data-source OBO request an
    ``api://<client-id>/...`` scope at login, which makes the login token's
    audience the app's own API — Graph rejects it with 401 InvalidAuthenticationToken.
    That same token is a valid OBO assertion, so exchange it for a User.Read
    Graph token using the provider's client credentials. Returns the token
    response dict, or None when the provider config can't do the exchange or
    Entra refuses it (e.g. expired assertion, missing consent).
    """
    from app.services.auth_providers import _get_oidc_config, _discover_endpoints

    cfg = _get_oidc_config(oauth_name)
    if not cfg or not (cfg.client_id and cfg.client_secret and cfg.issuer):
        return None

    issuer = cfg.issuer.rstrip("/")
    well_known = issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
    try:
        token_endpoint = (await _discover_endpoints(well_known))["token_endpoint"]
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                token_endpoint,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "assertion": assertion,
                    "scope": _GRAPH_OBO_SCOPE,
                    "requested_token_use": "on_behalf_of",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code >= 400:
                logger.warning(
                    f"Entra OBO exchange for Graph failed: {resp.status_code} {resp.text[:300]}"
                )
                return None
            return resp.json()
    except Exception as e:
        logger.warning(f"Entra OBO exchange for Graph failed: {e}")
        return None


async def _persist_graph_tokens(db: AsyncSession, acc: OAuthAccount, token: dict) -> None:
    """Store an OBO/refresh token response on the user's Entra OAuth account.

    Later preview/sync calls then start from a Graph-audience token (with a
    refresh token, thanks to offline_access) instead of the login token.
    """
    access = token.get("access_token")
    if not access:
        return
    acc.access_token = access
    if token.get("refresh_token"):
        acc.refresh_token = token["refresh_token"]
    expires_in = token.get("expires_in")
    if isinstance(expires_in, int):
        acc.expires_at = int(time.time()) + expires_in
    db.add(acc)
    await db.commit()


async def fetch_profile_fields(
    db: AsyncSession,
    user,
    fields: List[str],
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Raw Graph /me projection for the given fields (unset fields → None).

    Tries the supplied/stored token first; a Graph 401 (wrong audience — e.g.
    an api://-scoped login token — or an expired token) falls back to an OBO
    exchange, persists the resulting Graph tokens, and retries once. Raises
    EntraReauthRequired when no path yields a usable Graph token.
    """
    acc = await _entra_oauth_account(db, str(user.id))
    token = access_token or await get_entra_graph_token(db, user)
    if not token:
        return {}

    try:
        return await resolve_user_profile(token, fields)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 401:
            raise

    assertion = access_token or (acc.access_token if acc else None)
    obo = await _obo_exchange_for_graph(acc.oauth_name, assertion) if (acc and assertion) else None
    if not obo or not obo.get("access_token"):
        raise EntraReauthRequired()
    await _persist_graph_tokens(db, acc, obo)
    return await resolve_user_profile(obo["access_token"], fields)


async def fetch_profile(
    db: AsyncSession,
    user,
    fields: List[str],
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch the given Graph /me fields for a user.

    ``access_token`` may be supplied directly (e.g. the fresh token from a login
    callback); otherwise the user's stored Entra token is used/refreshed.
    Returns a dict of field → value (unset fields come back as None). Fields
    whose Graph value is None/empty are dropped so we don't store noise.
    """
    raw = await fetch_profile_fields(db, user, fields, access_token=access_token)
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


async def store_profile_attributes(
    db: AsyncSession,
    user,
    organization_id: str,
    attrs: Dict[str, Any],
    provider_label: str = "Profile",
) -> Dict[str, Any]:
    """Store fetched profile attributes on the user's Membership for this org.

    Provider-agnostic half of the on-login sync — the Entra and Google services
    both feed their fetched attributes through here. Empty attrs are a no-op so
    a failed fetch never wipes previously synced values.
    """
    from app.models.membership import Membership

    if not attrs:
        return {}

    membership = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization_id),
                Membership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not membership:
        return {}

    membership.profile_attributes = attrs
    flag_modified(membership, "profile_attributes")
    db.add(membership)
    await db.commit()
    logger.info(
        f"{provider_label} sync: stored {len(attrs)} attribute(s) for user "
        f"{user.id} in org {organization_id}"
    )
    return attrs


async def sync_profile_on_login(
    db: AsyncSession,
    user,
    organization_id: str,
    fields: List[str],
    access_token: str,
) -> Dict[str, Any]:
    """Fetch the profile and store it on the user's Membership for this org.

    Called from the login callback with the fresh delegated token. Best-effort:
    a Graph failure logs and leaves the existing attributes untouched.
    """
    attrs = await fetch_profile(db, user, fields, access_token=access_token)
    return await store_profile_attributes(
        db, user, organization_id, attrs, provider_label="Entra profile"
    )
