"""Opt-in RFC 8693 exchange for confidential custom applications.

Only tenant-specific, public-cloud Entra providers are accepted. Assertions are
validated against configured authorities, never token-controlled URLs, and are
not persisted. Entra oid/tid bindings are established by BOW's SSO callback.
"""

import asyncio
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from sqlalchemy import select

from app.models.oauth_account import OAuthAccount
from app.models.oauth_server import OAuthAccessToken
from app.settings.config import settings

GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_CACHE = {}


class ExchangeError(Exception):
    def __init__(self, error, description, status=400):
        self.error, self.description, self.status = error, description, status


def provider_config(name):
    cfg = next((p for p in settings.bow_config.oidc_providers if p.name == name and p.enabled), None)
    if cfg is None:
        raise ValueError("Entra provider is unavailable")
    url = urlsplit(cfg.issuer.rstrip("/"))
    parts = url.path.strip("/").split("/")
    if url.scheme != "https" or url.netloc != "login.microsoftonline.com" or url.query or url.fragment:
        raise ValueError("A tenant-specific Microsoft Entra provider is required")
    if len(parts) not in (1, 2) or (len(parts) == 2 and parts[1] != "v2.0"):
        raise ValueError("A tenant-specific Microsoft Entra provider is required")
    tenant = str(UUID(parts[0]))
    client = str(UUID(cfg.client_id or ""))
    if not cfg.client_secret:
        raise ValueError("The Entra provider requires a backend credential")
    scope = f"api://{client}/access_as_user"
    if scope not in cfg.scopes:
        raise ValueError("Configure access_as_user on the Entra provider first")
    return cfg, tenant, client


def provider_options():
    result = []
    for cfg in settings.bow_config.oidc_providers:
        try:
            _, tenant, client = provider_config(cfg.name)
        except (ValueError, TypeError):
            continue
        result.append(
            {
                "name": cfg.name,
                "label": cfg.label or cfg.name,
                "tenant_id": tenant,
                "audience": f"api://{client}",
                "scope": f"api://{client}/access_as_user",
            }
        )
    return result


def validate_settings(value):
    if value is None or value == {}:
        return None
    if not isinstance(value, dict) or set(value) != {"provider", "external_client_id"}:
        raise ValueError("Specify provider and external_client_id")
    _, tenant, client = provider_config(value["provider"])
    external = str(UUID(value["external_client_id"]))
    return {
        "provider": value["provider"],
        "external_client_id": external,
        "tenant_id": tenant,
        "audience_client_id": client,
    }


def active_config(config):
    if not config:
        raise ValueError("Entra exchange is disabled")
    cfg, tenant, client = provider_config(config["provider"])
    if tenant != config["tenant_id"] or client != config["audience_client_id"]:
        raise ValueError("Entra provider changed; configure the application again")
    return cfg, tenant, client


async def _json(url, refresh=False):
    # Bounded cache and TTL. No event-loop-bound clients/locks are shared.
    cached = _CACHE.get(url)
    if not refresh and cached and cached[0] > time.monotonic():
        return cached[1]
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        value = response.json()
    if len(_CACHE) >= 128:
        _CACHE.clear()
    _CACHE[url] = (time.monotonic() + 300, value)
    return value


async def verify_token(token, cfg, *, id_token=False):
    _, tenant, audience = provider_config(cfg.name)
    if not isinstance(token, str) or len(token) > 32768:
        raise ValueError("Invalid token")
    unverified = jwt.decode(token, options={"verify_signature": False})
    version = unverified.get("ver")
    if version not in ("1.0", "2.0"):
        raise ValueError("Unsupported token version")
    authority = f"https://login.microsoftonline.com/{tenant}"
    issuer = f"{authority}/v2.0" if version == "2.0" else f"https://sts.windows.net/{tenant}/"
    metadata = await _json(f"{authority}{'/v2.0' if version == '2.0' else ''}/.well-known/openid-configuration")
    if metadata.get("issuer") != issuer:
        raise ValueError("Invalid discovery issuer")
    jwks_uri = metadata["jwks_uri"]
    parsed = urlsplit(jwks_uri)
    if parsed.scheme != "https" or parsed.netloc != "login.microsoftonline.com":
        raise ValueError("Invalid key authority")
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("Invalid signing algorithm")
    for refresh in (False, True):
        keys = await _json(jwks_uri, refresh=refresh)
        key = next((k for k in keys.get("keys", []) if k.get("kid") == header["kid"]), None)
        if key:
            break
    if not key:
        raise ValueError("Unknown signing key")
    key_issuer = key.get("issuer", issuer).replace("{tenantid}", tenant)
    if key_issuer != issuer:
        raise ValueError("Invalid key issuer")
    expected_aud = audience if id_token or version == "2.0" else f"api://{audience}"
    claims = jwt.decode(
        token,
        jwt.PyJWK.from_dict(key).key,
        algorithms=["RS256"],
        audience=expected_aud,
        issuer=issuer,
        options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "sub"], "strict_aud": True},
    )
    if claims["tid"] != tenant:
        raise ValueError("Invalid tenant")
    UUID(claims["oid"])
    if not id_token and (claims.get("idtyp") == "app" or "access_as_user" not in str(claims.get("scp", "")).split()):
        raise ValueError("Delegated access_as_user is required")
    return claims


async def link_sso_identity(db, user_id, cfg, account_id, id_token):
    """Bind an existing SSO account using a signature-validated ID token."""
    claims = await verify_token(id_token, cfg, id_token=True)
    uid_claim = cfg.uid_claim or "sub"
    if str(claims.get(uid_claim) or claims["sub"]) != str(account_id):
        raise ValueError("SSO account mismatch")
    account = await db.scalar(
        select(OAuthAccount).where(
            OAuthAccount.user_id == str(user_id),
            OAuthAccount.oauth_name == cfg.name,
            OAuthAccount.account_id == str(account_id),
        )
    )
    if account:
        account.entra_identity = {"tenant_id": claims["tid"], "object_id": claims["oid"], "client_id": str(UUID(cfg.client_id))}
        await db.commit()


async def exchange(db, service, *, client_id, client_secret, subject_token, scope):
    if not client_secret:
        raise ExchangeError("invalid_client", "Client authentication is required", 401)
    client = await service.validate_client(db, client_id, client_secret)
    if client is None:
        raise ExchangeError("invalid_client", "Invalid client credentials", 401)
    config = dict(client.entra_exchange or {})
    try:
        cfg, tenant, audience = active_config(config)
    except (ValueError, KeyError, TypeError):
        raise ExchangeError("unauthorized_client", "Entra token exchange is not enabled for this application") from None
    requested = scope.split() if scope else client.scopes.split()
    if not requested or not set(requested) <= set(client.scopes.split()) or not set(requested) <= {"app", "mcp"}:
        raise ExchangeError("invalid_scope", "Requested scope is not allowed")
    normalized_scope = " ".join(dict.fromkeys(requested))
    try:
        claims = await verify_token(subject_token, cfg)
        caller = claims.get("azp") if claims["ver"] == "2.0" else claims.get("appid")
        if caller != config["external_client_id"]:
            raise ValueError("Invalid external client")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise ExchangeError("invalid_grant", "Invalid Entra access token") from None
    except httpx.HTTPError:
        raise ExchangeError("temporarily_unavailable", "Entra validation is temporarily unavailable", 503) from None

    from app.services.entra_admission import resolve_or_admit

    user_id = await resolve_or_admit(db, service, client, config, claims)
    subject = await service._active_subject(db, user_id, client.organization_id)
    if not subject:
        raise ExchangeError("invalid_grant", "User access is unavailable")

    from app.services.connection_oauth_service import auto_provision_connection_credentials

    try:
        async with asyncio.timeout(120):
            summary = await auto_provision_connection_credentials(
                db,
                subject[0],
                subject_token,
                missing_only=True,
                client_id=audience,
                organization_id=client.organization_id,
                tenant_id=tenant,
            )
    except (TimeoutError, httpx.HTTPError):
        raise ExchangeError(
            "temporarily_unavailable", "Connection provisioning is not ready; retry the exchange", 503
        ) from None
    if any(s.get("reason") == "provisioning_in_progress" for s in summary["skipped"]):
        raise ExchangeError(
            "temporarily_unavailable", "Connection provisioning is in progress; retry the exchange", 503
        )
    if summary["failed"]:
        raise ExchangeError(
            "invalid_grant",
            "Downstream authorization failed; check consent, connection configuration, or sign in interactively",
        )
    # Recheck app changes and membership after remote IO. Lock the app during
    # issuance, so concurrent disable/revoke either wins here or revokes the row.
    from app.models.oauth_server import OAuthClient

    client = await db.scalar(
        select(OAuthClient)
        .where(OAuthClient.client_id == client_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        not client
        or client.deleted_at
        or client.entra_exchange != config
        or not set(requested) <= set(client.scopes.split())
        or not secrets.compare_digest(service_hash(client_secret), client.client_secret_hash)
        or not await service._active_subject(db, user_id, client.organization_id)
    ):
        raise ExchangeError("invalid_grant", "Application or user access changed")
    try:
        active_config(config)
    except (ValueError, KeyError, TypeError):
        raise ExchangeError("invalid_grant", "Entra provider configuration changed") from None
    from app.services.oauth_server_service import ACCESS_TOKEN_PREFIX, _generate_token

    token, token_hash = _generate_token(ACCESS_TOKEN_PREFIX)
    lifetime = min(3600, int(claims["exp"] - time.time()))
    if lifetime < 1:
        raise ExchangeError("invalid_grant", "Entra access token expired")
    db.add(
        OAuthAccessToken(
            token_hash=token_hash,
            client_id=client_id,
            user_id=user_id,
            organization_id=client.organization_id,
            scope=normalized_scope,
            expires_at=datetime.utcnow() + timedelta(seconds=lifetime),
            exchange_context=config,
        )
    )
    await db.commit()
    return {
        "access_token": token,
        "token_type": "Bearer",
        "issued_token_type": ACCESS_TYPE,
        "expires_in": lifetime,
        "scope": normalized_scope,
    }


def service_hash(value):
    from app.services.oauth_server_service import _hash

    return _hash(value)
