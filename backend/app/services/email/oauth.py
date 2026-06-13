"""OAuth token acquisition + XOAUTH2 SASL for cloud mailboxes.

Basic Auth (passwords/app-passwords) is dead on Microsoft 365 (IMAP/POP since
2022; SMTP AUTH disabled-by-default end of 2026) and Google Workspace (since
March 2025). To keep the IMAP/SMTP transport working against those providers we
authenticate the socket with an **OAuth access token** via the XOAUTH2 SASL
mechanism instead of a password.

Two app-level (no interactive user) strategies — the right model for a single
shared/service mailbox an admin configures org-wide:

- ``microsoft`` — OAuth2 **client-credentials** (app-only). Token scope
  ``https://outlook.office365.com/.default``; the Entra app holds the
  ``IMAP.AccessAsApp`` / ``SMTP.SendAsApp`` application permissions and is
  granted access to the one mailbox via ``Add-MailboxPermission``.
- ``google`` — **service account + domain-wide delegation** impersonating the
  mailbox; scope ``https://mail.google.com/``.

This module only mints tokens and formats the SASL string; ``sender.py`` and
``mailbox_reader.py`` apply it to SMTP/IMAP respectively.
"""
from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from typing import Optional

import httpx

# Default OAuth scopes for IMAP/SMTP access (NOT the Graph scopes).
MS_SCOPE = "https://outlook.office365.com/.default"
GOOGLE_SCOPE = "https://mail.google.com/"


@dataclass
class OAuthSettings:
    """Everything needed to mint an XOAUTH2 token for one mailbox."""

    provider: str  # "microsoft" | "google" | "microsoft_delegated" | "google_delegated"
    mailbox: str
    # Microsoft (client-credentials)
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    # Google (service account + DWD)
    service_account_info: Optional[dict] = None
    # Delegated flows (refresh-token based; no PowerShell / no app-only perms)
    refresh_token: Optional[str] = None

    @classmethod
    def from_credentials(cls, creds: dict, config: Optional[dict] = None) -> Optional["OAuthSettings"]:
        """Build from stored credentials/config, or None for password auth."""
        creds = creds or {}
        config = config or {}
        auth_type = creds.get("auth_type") or config.get("auth_type") or "password"
        mailbox = (
            creds.get("from_address")
            or config.get("from_address")
            or creds.get("smtp_username")
            or creds.get("imap_username")
        )
        if auth_type == "microsoft":
            return cls(
                provider="microsoft",
                mailbox=mailbox,
                tenant_id=creds.get("ms_tenant_id") or config.get("ms_tenant_id"),
                client_id=creds.get("ms_client_id") or config.get("ms_client_id"),
                client_secret=creds.get("ms_client_secret"),
            )
        if auth_type == "microsoft_delegated":
            return cls(
                provider="microsoft_delegated",
                mailbox=mailbox,
                tenant_id=creds.get("ms_tenant_id") or config.get("ms_tenant_id"),
                client_id=creds.get("ms_client_id") or config.get("ms_client_id"),
                client_secret=creds.get("ms_client_secret"),  # optional (public client)
                refresh_token=creds.get("ms_refresh_token"),
            )
        if auth_type == "google":
            return cls(
                provider="google",
                mailbox=mailbox,
                service_account_info=creds.get("google_service_account_info"),
            )
        if auth_type == "google_delegated":
            return cls(
                provider="google_delegated",
                mailbox=mailbox,
                client_id=creds.get("google_client_id") or config.get("google_client_id"),
                client_secret=creds.get("google_client_secret"),
                refresh_token=creds.get("google_refresh_token"),
            )
        return None


def build_xoauth2(user: str, access_token: str) -> str:
    """Return the base64 XOAUTH2 SASL initial-response string.

    Format (RFC 7628 / Google & Microsoft convention):
        base64("user=" + user + ^A + "auth=Bearer " + token + ^A^A)
    where ^A is Ctrl-A (0x01).
    """
    raw = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _ms_token_url(tenant_id: str) -> str:
    # Overridable for sandbox/tests.
    base = os.environ.get("BOW_MS_LOGIN_BASE", "https://login.microsoftonline.com")
    return f"{base}/{tenant_id}/oauth2/v2.0/token"


async def get_ms_app_token(
    tenant_id: str, client_id: str, client_secret: str, scope: str = MS_SCOPE
) -> str:
    """Acquire an app-only access token via the client-credentials flow."""
    if not (tenant_id and client_id and client_secret):
        raise ValueError("microsoft auth requires tenant_id, client_id, client_secret")
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": scope,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_ms_token_url(tenant_id), data=data)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise ValueError("microsoft token response had no access_token")
        return token


def _get_google_dwd_token_blocking(
    service_account_info: dict, subject: str, scope: str = GOOGLE_SCOPE
) -> str:
    """Mint a token for a service account impersonating ``subject`` (blocking)."""
    from google.oauth2 import service_account
    import google.auth.transport.requests

    if not service_account_info or not subject:
        raise ValueError("google auth requires service_account_info and mailbox")
    creds = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=[scope], subject=subject
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def get_google_dwd_token(service_account_info: dict, subject: str, scope: str = GOOGLE_SCOPE) -> str:
    return await asyncio.to_thread(_get_google_dwd_token_blocking, service_account_info, subject, scope)


async def get_ms_delegated_token(
    tenant_id: str,
    client_id: str,
    refresh_token: str,
    client_secret: Optional[str] = None,
    scope: str = MS_SCOPE,
) -> str:
    """Refresh a delegated (user-consented) access token for IMAP/SMTP.

    Works with a public client (no secret, device-code/PKCE) or a confidential
    client (with secret). No PowerShell / app-only permissions needed.
    """
    if not (tenant_id and client_id and refresh_token):
        raise ValueError("microsoft_delegated requires tenant_id, client_id, refresh_token")
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_ms_token_url(tenant_id), data=data)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise ValueError("microsoft_delegated token response had no access_token")
        return token


async def get_google_delegated_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Refresh a delegated Google access token (OAuth client + stored refresh token)."""
    if not (client_id and client_secret and refresh_token):
        raise ValueError("google_delegated requires client_id, client_secret, refresh_token")
    url = os.environ.get("BOW_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, data=data)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise ValueError("google_delegated token response had no access_token")
        return token


async def get_access_token(oauth: OAuthSettings) -> str:
    """Mint an access token for the configured provider/mailbox."""
    if oauth.provider == "microsoft":
        return await get_ms_app_token(oauth.tenant_id, oauth.client_id, oauth.client_secret)
    if oauth.provider == "microsoft_delegated":
        return await get_ms_delegated_token(
            oauth.tenant_id, oauth.client_id, oauth.refresh_token, oauth.client_secret
        )
    if oauth.provider == "google":
        return await get_google_dwd_token(oauth.service_account_info, oauth.mailbox)
    if oauth.provider == "google_delegated":
        return await get_google_delegated_token(
            oauth.client_id, oauth.client_secret, oauth.refresh_token
        )
    raise ValueError(f"unsupported oauth provider: {oauth.provider}")


async def get_xoauth2_string(oauth: OAuthSettings) -> str:
    """Convenience: mint a token and return the ready XOAUTH2 SASL string."""
    token = await get_access_token(oauth)
    return build_xoauth2(oauth.mailbox, token)
