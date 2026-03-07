"""OAuth 2.1 Authorization Server service.

Handles client registration, authorization code issuance, token exchange,
and token validation for external MCP clients (e.g., Claude Web).
"""

import json
import logging
import secrets
import hashlib
import base64
from typing import Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.oauth_server import (
    OAuthMCPClient,
    OAuthMCPAuthorizationCode,
    OAuthMCPAccessToken,
)
from app.models.user import User
from app.models.organization import Organization

# Token prefixes for easy identification in auth middleware
ACCESS_TOKEN_PREFIX = "bow_oauth_"
REFRESH_TOKEN_PREFIX = "bow_rt_"

# Token lifetimes
ACCESS_TOKEN_LIFETIME = timedelta(days=365)
REFRESH_TOKEN_LIFETIME = timedelta(days=365)
AUTHORIZATION_CODE_LIFETIME = timedelta(minutes=5)

# Claude's known callback URLs
CLAUDE_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:6274/oauth/callback",
    "http://localhost:6274/oauth/callback/debug",
]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_token(prefix: str) -> Tuple[str, str]:
    """Generate a token with prefix. Returns (full_token, hash)."""
    raw = secrets.token_urlsafe(32)
    full = f"{prefix}{raw}"
    return full, _hash(full)


def _verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE S256: SHA256(code_verifier) == code_challenge."""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


class OAuthServerService:

    # ── Client management ──────────────────────────────────────────

    async def create_client(
        self,
        db: AsyncSession,
        organization_id: str,
        name: str,
        redirect_uris: Optional[list[str]] = None,
    ) -> dict:
        """Create an OAuth client for an organization.

        Returns dict with client_id, client_secret (plaintext, shown only once), and name.
        """
        if redirect_uris is None:
            redirect_uris = list(CLAUDE_REDIRECT_URIS)

        client_id = f"bow_client_{secrets.token_urlsafe(16)}"
        client_secret = f"bow_secret_{secrets.token_urlsafe(32)}"
        secret_hash = _hash(client_secret)

        client = OAuthMCPClient(
            organization_id=organization_id,
            client_id=client_id,
            client_secret_hash=secret_hash,
            name=name,
            redirect_uris=json.dumps(redirect_uris),
            scopes="mcp",
        )
        db.add(client)
        await db.commit()
        await db.refresh(client)

        return {
            "id": client.id,
            "client_id": client.client_id,
            "client_secret": client_secret,
            "name": client.name,
            "redirect_uris": redirect_uris,
            "created_at": client.created_at.isoformat() if client.created_at else None,
        }

    async def list_clients(
        self,
        db: AsyncSession,
        organization_id: str,
    ) -> list[dict]:
        result = await db.execute(
            select(OAuthMCPClient)
            .where(OAuthMCPClient.organization_id == organization_id)
            .where(OAuthMCPClient.deleted_at.is_(None))
            .order_by(OAuthMCPClient.created_at.desc())
        )
        clients = result.scalars().all()
        return [
            {
                "id": c.id,
                "client_id": c.client_id,
                "name": c.name,
                "redirect_uris": json.loads(c.redirect_uris),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in clients
        ]

    async def get_client_info(
        self,
        db: AsyncSession,
        client_id: str,
    ) -> Optional[dict]:
        """Public info about a client (for consent screen)."""
        result = await db.execute(
            select(OAuthMCPClient)
            .where(OAuthMCPClient.client_id == client_id)
            .where(OAuthMCPClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return None
        return {"client_id": client.client_id, "name": client.name}

    async def delete_client(
        self,
        db: AsyncSession,
        client_db_id: str,
        organization_id: str,
    ) -> bool:
        result = await db.execute(
            select(OAuthMCPClient)
            .where(OAuthMCPClient.id == client_db_id)
            .where(OAuthMCPClient.organization_id == organization_id)
            .where(OAuthMCPClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return False
        client.deleted_at = datetime.utcnow()
        await db.commit()
        return True

    async def rotate_client_secret(
        self,
        db: AsyncSession,
        client_db_id: str,
        organization_id: str,
    ) -> Optional[dict]:
        """Rotate client_secret. Returns new secret (plaintext, shown once)."""
        result = await db.execute(
            select(OAuthMCPClient)
            .where(OAuthMCPClient.id == client_db_id)
            .where(OAuthMCPClient.organization_id == organization_id)
            .where(OAuthMCPClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return None

        new_secret = f"bow_secret_{secrets.token_urlsafe(32)}"
        client.client_secret_hash = _hash(new_secret)
        await db.commit()

        return {
            "id": client.id,
            "client_id": client.client_id,
            "client_secret": new_secret,
            "name": client.name,
        }

    # ── Client validation ──────────────────────────────────────────

    async def validate_client(
        self,
        db: AsyncSession,
        client_id: str,
        client_secret: Optional[str] = None,
    ) -> Optional[OAuthMCPClient]:
        """Validate client credentials. If client_secret is None, only validate client_id."""
        result = await db.execute(
            select(OAuthMCPClient)
            .where(OAuthMCPClient.client_id == client_id)
            .where(OAuthMCPClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return None

        if client_secret is not None:
            if _hash(client_secret) != client.client_secret_hash:
                return None

        return client

    def validate_redirect_uri(self, client: OAuthMCPClient, redirect_uri: str) -> bool:
        allowed = json.loads(client.redirect_uris)
        return redirect_uri in allowed

    # ── Authorization code ─────────────────────────────────────────

    async def create_authorization_code(
        self,
        db: AsyncSession,
        client_id: str,
        user_id: str,
        organization_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
    ) -> str:
        """Create and return an authorization code."""
        code = secrets.token_urlsafe(32)

        auth_code = OAuthMCPAuthorizationCode(
            code=code,
            client_id=client_id,
            user_id=user_id,
            organization_id=organization_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            expires_at=datetime.utcnow() + AUTHORIZATION_CODE_LIFETIME,
        )
        db.add(auth_code)
        await db.commit()
        return code

    # ── Token exchange ─────────────────────────────────────────────

    async def exchange_code(
        self,
        db: AsyncSession,
        code: str,
        client_id: str,
        client_secret: Optional[str],
        code_verifier: str,
        redirect_uri: str,
    ) -> Optional[dict]:
        """Exchange authorization code for access + refresh tokens.

        Returns token response dict or None on validation failure.
        """
        # Validate client
        client = await self.validate_client(db, client_id, client_secret)
        if not client:
            logger.warning("exchange_code failed: invalid client_id=%s or client_secret mismatch", client_id)
            return None

        # Look up the authorization code
        result = await db.execute(
            select(OAuthMCPAuthorizationCode)
            .where(OAuthMCPAuthorizationCode.code == code)
            .where(OAuthMCPAuthorizationCode.client_id == client_id)
            .where(OAuthMCPAuthorizationCode.deleted_at.is_(None))
        )
        auth_code = result.scalar_one_or_none()
        if not auth_code:
            logger.warning("exchange_code failed: authorization code not found or already used (client_id=%s)", client_id)
            return None

        # Check expiration
        if auth_code.expires_at < datetime.utcnow():
            logger.warning("exchange_code failed: authorization code expired at %s (client_id=%s)", auth_code.expires_at, client_id)
            auth_code.deleted_at = datetime.utcnow()
            await db.commit()
            return None

        # Verify PKCE
        if not _verify_pkce_s256(code_verifier, auth_code.code_challenge):
            logger.warning("exchange_code failed: PKCE verification failed (client_id=%s)", client_id)
            return None

        # Verify redirect_uri matches
        if auth_code.redirect_uri != redirect_uri:
            logger.warning("exchange_code failed: redirect_uri mismatch - expected=%s got=%s (client_id=%s)", auth_code.redirect_uri, redirect_uri, client_id)
            return None

        # Consume the code (one-time use)
        auth_code.deleted_at = datetime.utcnow()

        # Issue tokens
        access_token, access_hash = _generate_token(ACCESS_TOKEN_PREFIX)
        refresh_token, refresh_hash = _generate_token(REFRESH_TOKEN_PREFIX)

        token_record = OAuthMCPAccessToken(
            token_hash=access_hash,
            client_id=client_id,
            user_id=auth_code.user_id,
            organization_id=auth_code.organization_id,
            scope=auth_code.scope,
            expires_at=datetime.utcnow() + ACCESS_TOKEN_LIFETIME,
            refresh_token_hash=refresh_hash,
            refresh_expires_at=datetime.utcnow() + REFRESH_TOKEN_LIFETIME,
        )
        db.add(token_record)
        await db.commit()

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            "refresh_token": refresh_token,
            "scope": auth_code.scope,
        }

    async def refresh_access_token(
        self,
        db: AsyncSession,
        refresh_token: str,
        client_id: str,
        client_secret: Optional[str],
    ) -> Optional[dict]:
        """Issue a new access token using a refresh token."""
        # Validate client
        client = await self.validate_client(db, client_id, client_secret)
        if not client:
            logger.warning("refresh_access_token failed: invalid client_id=%s or client_secret mismatch", client_id)
            return None

        refresh_hash = _hash(refresh_token)

        result = await db.execute(
            select(OAuthMCPAccessToken)
            .where(OAuthMCPAccessToken.refresh_token_hash == refresh_hash)
            .where(OAuthMCPAccessToken.client_id == client_id)
            .where(OAuthMCPAccessToken.deleted_at.is_(None))
        )
        token_record = result.scalar_one_or_none()
        if not token_record:
            logger.warning("refresh_access_token failed: refresh token not found or already rotated (client_id=%s)", client_id)
            return None

        # Check refresh token expiration
        if token_record.refresh_expires_at and token_record.refresh_expires_at < datetime.utcnow():
            logger.warning("refresh_access_token failed: refresh token expired at %s (client_id=%s)", token_record.refresh_expires_at, client_id)
            return None

        # Invalidate old access token
        token_record.deleted_at = datetime.utcnow()

        # Issue new tokens
        new_access, new_access_hash = _generate_token(ACCESS_TOKEN_PREFIX)
        new_refresh, new_refresh_hash = _generate_token(REFRESH_TOKEN_PREFIX)

        new_record = OAuthMCPAccessToken(
            token_hash=new_access_hash,
            client_id=client_id,
            user_id=token_record.user_id,
            organization_id=token_record.organization_id,
            scope=token_record.scope,
            expires_at=datetime.utcnow() + ACCESS_TOKEN_LIFETIME,
            refresh_token_hash=new_refresh_hash,
            refresh_expires_at=datetime.utcnow() + REFRESH_TOKEN_LIFETIME,
        )
        db.add(new_record)
        await db.commit()

        return {
            "access_token": new_access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            "refresh_token": new_refresh,
            "scope": token_record.scope,
        }

    # ── Token validation (used by MCP endpoint) ───────────────────

    async def validate_access_token(
        self,
        db: AsyncSession,
        token: str,
    ) -> Optional[Tuple[User, Organization]]:
        """Validate an OAuth access token and return (user, organization).

        Returns None if token is invalid or expired.
        """
        if not token.startswith(ACCESS_TOKEN_PREFIX):
            return None

        token_hash = _hash(token)

        result = await db.execute(
            select(OAuthMCPAccessToken)
            .where(OAuthMCPAccessToken.token_hash == token_hash)
            .where(OAuthMCPAccessToken.deleted_at.is_(None))
        )
        token_record = result.scalar_one_or_none()
        if not token_record:
            return None

        if token_record.expires_at < datetime.utcnow():
            return None

        # Load user and organization
        user_result = await db.execute(
            select(User).where(User.id == token_record.user_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            return None

        org_result = await db.execute(
            select(Organization).where(Organization.id == token_record.organization_id)
        )
        org = org_result.scalar_one_or_none()
        if not org:
            return None

        return user, org
