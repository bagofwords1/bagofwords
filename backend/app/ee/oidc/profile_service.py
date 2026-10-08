# Entra ID profile / job-info sync service.
# Licensed under the BOW Enterprise License
#
# Fetches the signed-in user's Microsoft Graph /me profile (job title,
# department, etc.) and persists the selected fields on their per-org
# Membership. Everything here uses the default-granted delegated User.Read
# scope — no admin consent required.

import logging
import time
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.ee.oidc.graph_client import resolve_user_profile
from app.models.oauth_account import OAuthAccount

logger = logging.getLogger(__name__)

# Scope requested when refreshing a login token specifically for a Graph /me
# call. offline_access keeps the refresh token rolling.
_GRAPH_REFRESH_SCOPE = "openid profile email https://graph.microsoft.com/User.Read offline_access"

# Scope requested when OBO-exchanging a login token for a Graph /me call.
# OBO requests must target a single resource, so no openid/profile/email here.
_GRAPH_OBO_SCOPE = "https://graph.microsoft.com/User.Read"


class EntraReauthRequired(Exception):
    """The stored Entra tokens can no longer produce a usable Graph token —
    the user has to sign in with Entra ID again."""


async def _entra_oauth_account(db: AsyncSession, user_id: str) -> OAuthAccount | None:
    """Return the user's Entra-based OAuth login account, if any."""
    from app.services.auth_providers import _is_entra_provider

    rows = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == str(user_id)))).scalars().all()
    for acc in rows:
        try:
            if _is_entra_provider(acc.oauth_name):
                return acc
        except Exception:
            continue
    return None


async def get_entra_graph_token(db: AsyncSession, user) -> str | None:
    """Return a login token candidate, refreshing for Graph when expired.

    The saved access token may target a data-source API. Graph validates the
    candidate in fetch_profile_fields; a 401 triggers a resource-specific
    refresh there even when the login token has not expired.
    """
    acc = await _entra_oauth_account(db, str(user.id))
    if not acc:
        return None
    if acc.access_token and (not acc.expires_at or int(acc.expires_at) > time.time() + 60):
        return acc.access_token
    return await _refresh_graph_token(db, acc) or acc.access_token


async def _refresh_graph_token(db: AsyncSession, acc: OAuthAccount) -> str | None:
    """Acquire a separate Graph token using the SSO client's refresh grant.

    Refresh tokens belong to the SSO client, while the login access token can
    target a different Fabric client. Never replace that assertion or expiry
    with a Graph token; connection recovery still needs the original audience.
    """
    old_refresh = acc.refresh_token
    old_access = acc.access_token
    if not old_refresh:
        return None

    from app.services.auth_providers import _discover_endpoints, _get_oidc_config

    cfg = _get_oidc_config(acc.oauth_name)
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
                    "grant_type": "refresh_token",
                    "refresh_token": old_refresh,
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "scope": _GRAPH_REFRESH_SCOPE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.is_error:
                _log_exchange_failure("refresh", resp)
                return None
            token = resp.json()
    except Exception as e:
        logger.warning("Entra Graph refresh failed (%s)", type(e).__name__)
        return None

    if not isinstance(token, dict):
        return None
    new_access = token.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        return None
    new_refresh = token.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        # A concurrent login or connection recovery may already have rotated
        # this account. Do not overwrite its newer credentials with this result.
        await db.execute(
            update(OAuthAccount)
            .where(
                OAuthAccount.id == acc.id,
                OAuthAccount.refresh_token == old_refresh,
                OAuthAccount.access_token == old_access,
            )
            .values(refresh_token=new_refresh)
        )
        await db.commit()
    return new_access


def _log_exchange_failure(grant: str, response: httpx.Response) -> None:
    # Descriptions and exception strings can contain tokens/request data.
    try:
        codes = [c for c in response.json().get("error_codes", []) if isinstance(c, int)]
    except (ValueError, TypeError, AttributeError):
        codes = []
    logger.warning("Entra Graph %s failed: HTTP %s, AADSTS codes %s", grant, response.status_code, codes)


async def _obo_exchange_for_graph(oauth_name: str, assertion: str) -> dict | None:
    """Exchange a login access token for a Graph-audience token via OBO.

    Entra configs that also drive data-source OBO request an
    ``api://<client-id>/...`` scope at login, which makes the login token's
    audience the app's own API — Graph rejects it with 401 InvalidAuthenticationToken.
    When its audience matches the SSO client, it is also a valid assertion for
    a Graph exchange using that client's credentials. With different clients,
    Entra rejects this fallback; the SSO refresh grant is needed instead. Returns the token
    response dict, or None when the provider config can't do the exchange or
    Entra refuses it (e.g. expired assertion, missing consent).
    """
    from app.services.auth_providers import _discover_endpoints, _get_oidc_config

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
                _log_exchange_failure("OBO", resp)
                return None
            return resp.json()
    except Exception as e:
        logger.warning("Entra Graph OBO failed (%s)", type(e).__name__)
        return None


async def fetch_profile_fields(
    db: AsyncSession,
    user,
    fields: list[str],
    access_token: str | None = None,
) -> dict[str, Any]:
    """Raw Graph /me projection for the given fields (unset fields → None).

    Graph validates the supplied/stored token first. On 401, acquire a separate
    Graph token with the SSO refresh grant (works with different SSO/Fabric
    clients). Preserve the legacy same-client OBO fallback when no refresh is
    available. Graph access tokens remain request-local, never replacing the
    saved data-source assertion.
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

    refreshed = await _refresh_graph_token(db, acc) if acc else None
    if refreshed:
        try:
            return await resolve_user_profile(refreshed, fields)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401:
                raise

    assertion = access_token or (acc.access_token if acc else None)
    obo = await _obo_exchange_for_graph(acc.oauth_name, assertion) if (acc and assertion) else None
    if not obo or not obo.get("access_token"):
        raise EntraReauthRequired()
    return await resolve_user_profile(obo["access_token"], fields)


async def fetch_profile(
    db: AsyncSession,
    user,
    fields: list[str],
    access_token: str | None = None,
) -> dict[str, Any]:
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
    attrs: dict[str, Any],
    provider_label: str = "Profile",
) -> dict[str, Any]:
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
    logger.info(f"{provider_label} sync: stored {len(attrs)} attribute(s) for user {user.id} in org {organization_id}")
    return attrs


async def sync_profile_on_login(
    db: AsyncSession,
    user,
    organization_id: str,
    fields: list[str],
    access_token: str,
) -> dict[str, Any]:
    """Fetch the profile and store it on the user's Membership for this org.

    Called from the login callback with the fresh delegated token. Best-effort:
    a Graph failure logs and leaves the existing attributes untouched.
    """
    attrs = await fetch_profile(db, user, fields, access_token=access_token)
    return await store_profile_attributes(db, user, organization_id, attrs, provider_label="Entra profile")
