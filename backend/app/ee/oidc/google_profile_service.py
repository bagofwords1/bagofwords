# Google profile / job-info sync service.
# Licensed under the BOW Enterprise License
#
# Fetches the signed-in user's Google profile and persists the selected fields
# on their per-org Membership — the Google counterpart of the Entra sync in
# profile_service.py. Two sources, both covered by the ``openid profile email``
# scopes the Google login already requests:
#
#   - OAuth2 userinfo: display name, locale, hosted domain. Always available.
#   - People API ``people/me``: the Workspace admin-set directory profile
#     (job title, department, organization, location). Needs the People API
#     enabled on the OAuth client's GCP project; when it isn't (or the account
#     is a personal Gmail with no directory data), those fields simply come
#     back unset — the sync degrades instead of failing.

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oauth_account import OAuthAccount

logger = logging.getLogger(__name__)

# Env overrides exist so sandboxes/e2e runs can point the sync at a mock
# Google; production deployments never set them.
USERINFO_URL = os.environ.get(
    "BOW_GOOGLE_USERINFO_URL", "https://www.googleapis.com/oauth2/v1/userinfo"
)
PEOPLE_ME_URL = os.environ.get(
    "BOW_GOOGLE_PEOPLE_URL", "https://people.googleapis.com/v1/people/me"
)
GOOGLE_TOKEN_URL = os.environ.get(
    "BOW_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"
)

# personFields covering every allowlisted attribute the People API can serve.
_PERSON_FIELDS = "names,organizations,locations,locales"

# Allowlisted fields the People API can improve on over plain userinfo.
_PEOPLE_API_FIELDS = {
    "displayName",
    "jobTitle",
    "department",
    "organization",
    "location",
    "locale",
}


class GoogleReauthRequired(Exception):
    """The stored Google tokens can no longer produce a usable access token —
    the user has to sign in with Google again. Google login tokens expire after
    ~1h and the default login flow gets no refresh token (no offline access),
    so this is the expected state for the settings preview between logins."""


async def _google_oauth_account(db: AsyncSession, user_id: str) -> Optional[OAuthAccount]:
    """Return the user's Google-based OAuth login account, if any."""
    from app.services.auth_providers import _is_google_provider

    rows = (
        await db.execute(
            select(OAuthAccount).where(OAuthAccount.user_id == str(user_id))
        )
    ).scalars().all()
    for acc in rows:
        try:
            if _is_google_provider(acc.oauth_name):
                return acc
        except Exception:
            continue
    return None


def _google_client_credentials(oauth_name: str) -> Optional[tuple]:
    """Client id/secret for refreshing a Google login token.

    The built-in ``google`` provider reads bow-config ``google_oauth``; a
    Google-issuer OIDC provider reads its own provider entry.
    """
    from app.settings.config import settings
    from app.services.auth_providers import _get_oidc_config

    if oauth_name == "google":
        g = settings.bow_config.google_oauth
        if g and g.client_id and g.client_secret:
            return (g.client_id, g.client_secret)
        return None
    cfg = _get_oidc_config(oauth_name)
    if cfg and cfg.client_id and cfg.client_secret:
        return (cfg.client_id, cfg.client_secret)
    return None


async def _refresh_google_token(db: AsyncSession, acc: OAuthAccount) -> Optional[str]:
    """Exchange the stored refresh token for a fresh access token, persist it.

    Returns the new access token, or None when there is no refresh token /
    client credentials / Google refuses the exchange.
    """
    if not acc or not acc.refresh_token:
        return None
    creds = _google_client_credentials(acc.oauth_name)
    if not creds:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": acc.refresh_token,
                    "client_id": creds[0],
                    "client_secret": creds[1],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token = resp.json()
    except Exception as e:
        logger.warning(f"Google token refresh failed for account {acc.account_email}: {e}")
        return None

    new_access = token.get("access_token")
    if not new_access:
        return None
    acc.access_token = new_access
    if token.get("refresh_token"):
        acc.refresh_token = token["refresh_token"]
    expires_in = token.get("expires_in")
    if isinstance(expires_in, int):
        acc.expires_at = int(time.time()) + expires_in
    db.add(acc)
    await db.commit()
    return new_access


async def get_google_access_token(db: AsyncSession, user) -> Optional[str]:
    """Return a usable Google access token for the user, refreshing if expired.

    Reads the token persisted at login (fastapi-users ``oauth_accounts``).
    Returns None when the user has no Google login on file. An expired token
    with no refresh token is returned as-is — the caller surfaces the 401 as
    a reauth-required state rather than silently skipping.
    """
    acc = await _google_oauth_account(db, str(user.id))
    if not acc:
        return None

    now = int(time.time())
    if acc.access_token and (not acc.expires_at or int(acc.expires_at) > now + 60):
        return acc.access_token

    refreshed = await _refresh_google_token(db, acc)
    return refreshed or acc.access_token


def _flatten_userinfo(data: dict) -> Dict[str, Any]:
    """Map the OAuth2 userinfo payload onto the allowlisted field names."""
    return {
        "displayName": data.get("name"),
        "locale": data.get("locale"),
        "hostedDomain": data.get("hd"),
    }


def _flatten_people(data: dict) -> Dict[str, Any]:
    """Map a People API person resource onto the allowlisted field names.

    ``organizations`` prefers the primary entry (metadata.primary) and carries
    the Workspace directory title/department. ``locations`` composes a readable
    value from the structured building/floor/desk parts when no plain value is
    present.
    """
    out: Dict[str, Any] = {}

    names = data.get("names") or []
    if names:
        out["displayName"] = names[0].get("displayName")

    orgs = data.get("organizations") or []
    org = next(
        (o for o in orgs if (o.get("metadata") or {}).get("primary")),
        orgs[0] if orgs else None,
    )
    if org:
        out["jobTitle"] = org.get("title")
        out["department"] = org.get("department")
        out["organization"] = org.get("name")
        if org.get("location"):
            out["location"] = org.get("location")

    if not out.get("location"):
        locs = data.get("locations") or []
        loc = next(
            (l for l in locs if (l.get("metadata") or {}).get("primary")),
            locs[0] if locs else None,
        )
        if loc:
            parts = [loc.get("value"), loc.get("buildingId"), loc.get("floor"), loc.get("deskCode")]
            composed = ", ".join(str(p) for p in parts if p)
            if composed:
                out["location"] = composed

    locales = data.get("locales") or []
    if locales:
        out["locale"] = locales[0].get("value")

    return out


async def _resolve_google_profile(access_token: str, fields: List[str]) -> Dict[str, Any]:
    """Fetch and merge userinfo + People API values for the requested fields.

    userinfo is authoritative for token validity — its 401 propagates so the
    caller can refresh/reauth. The People API layer is best-effort on top: a
    400/403/404 (API not enabled on the GCP project, consent shape, no person
    record) logs and degrades to userinfo-only values instead of failing.
    """
    if not fields:
        return {}

    headers = {"Authorization": f"Bearer {access_token}"}
    merged: Dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(USERINFO_URL, headers=headers)
        resp.raise_for_status()
        merged.update(_flatten_userinfo(resp.json()))

        if any(f in _PEOPLE_API_FIELDS for f in fields):
            presp = await client.get(
                f"{PEOPLE_ME_URL}?personFields={_PERSON_FIELDS}", headers=headers
            )
            if presp.status_code in (400, 403, 404):
                logger.warning(
                    "Google People API unavailable (%s) — syncing userinfo-only "
                    "fields. Enable the People API on the OAuth client's GCP "
                    "project for directory job info.",
                    presp.status_code,
                )
            else:
                presp.raise_for_status()
                for k, v in _flatten_people(presp.json()).items():
                    if v not in (None, "", [], {}):
                        merged[k] = v

    return {f: merged.get(f) for f in fields}


async def fetch_profile_fields(
    db: AsyncSession,
    user,
    fields: List[str],
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Raw Google profile projection for the given fields (unset fields → None).

    Tries the supplied/stored token first; a 401 (expired login token) falls
    back to a refresh-token exchange and retries once. Raises
    GoogleReauthRequired when no path yields a usable token.
    """
    acc = await _google_oauth_account(db, str(user.id))
    token = access_token or await get_google_access_token(db, user)
    if not token:
        return {}

    try:
        return await _resolve_google_profile(token, fields)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 401:
            raise

    refreshed = await _refresh_google_token(db, acc) if acc else None
    if not refreshed:
        raise GoogleReauthRequired()
    return await _resolve_google_profile(refreshed, fields)


async def fetch_profile(
    db: AsyncSession,
    user,
    fields: List[str],
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch the given Google profile fields for a user.

    ``access_token`` may be supplied directly (e.g. the fresh token from a login
    callback); otherwise the user's stored Google token is used/refreshed.
    Fields whose value is None/empty are dropped so we don't store noise.
    """
    raw = await fetch_profile_fields(db, user, fields, access_token=access_token)
    return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


async def sync_profile_on_login(
    db: AsyncSession,
    user,
    organization_id: str,
    fields: List[str],
    access_token: str,
) -> Dict[str, Any]:
    """Fetch the profile and store it on the user's Membership for this org.

    Called from the login callback with the fresh login token. Best-effort:
    a Google API failure logs and leaves the existing attributes untouched.
    """
    from app.ee.oidc.profile_service import store_profile_attributes

    attrs = await fetch_profile(db, user, fields, access_token=access_token)
    return await store_profile_attributes(
        db, user, organization_id, attrs, provider_label="Google profile"
    )
