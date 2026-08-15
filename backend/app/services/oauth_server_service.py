"""OAuth 2.1 Authorization Server service.

Handles client registration, authorization code issuance, token exchange,
and token validation for external OAuth clients.
"""

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oauth_server import (
    OAuthAccessToken,
    OAuthAuthorizationCode,
    OAuthClient,
)
from app.models.organization import Organization
from app.models.user import User

logger = logging.getLogger(__name__)

# Token prefixes for easy identification in auth middleware
ACCESS_TOKEN_PREFIX = "bow_oauth_"
REFRESH_TOKEN_PREFIX = "bow_rt_"

# Token lifetimes
MCP_ACCESS_TOKEN_LIFETIME = timedelta(days=365)
APP_ACCESS_TOKEN_LIFETIME = timedelta(hours=8)
REFRESH_TOKEN_LIFETIME = timedelta(days=365)
AUTHORIZATION_CODE_LIFETIME = timedelta(minutes=5)
TOKEN_ACTIVITY_DEBOUNCE = timedelta(hours=1)

# Default redirect URIs (covers Claude Web and common MCP inspector tools).
DEFAULT_REDIRECT_URIS = [
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


def _access_token_lifetime(scope: str) -> timedelta:
    """App-capable tokens use the short lifetime, including mixed tokens."""
    return APP_ACCESS_TOKEN_LIFETIME if "app" in scope.split() else MCP_ACCESS_TOKEN_LIFETIME


@dataclass
class OAuthTokenContext:
    user: User
    organization: Organization
    scopes: frozenset[str]
    client: OAuthClient
    token_record: OAuthAccessToken


class OAuthServerService:

    @staticmethod
    def _client_payload(
        client: OAuthClient,
        *,
        active_token_count: int = 0,
        last_used_at: Optional[datetime] = None,
        last_issued_at: Optional[datetime] = None,
    ) -> dict:
        return {
            "id": client.id,
            "client_id": client.client_id,
            "name": client.name,
            "redirect_uris": json.loads(client.redirect_uris),
            "scopes": (client.scopes or "").split(),
            "trusted": bool(client.trusted),
            "active_token_count": active_token_count,
            "last_used_at": last_used_at.isoformat() if last_used_at else None,
            "last_issued_at": last_issued_at.isoformat() if last_issued_at else None,
            "created_at": client.created_at.isoformat() if client.created_at else None,
        }

    # ── Client management ──────────────────────────────────────────

    async def create_client(
        self,
        db: AsyncSession,
        organization_id: str,
        name: str,
        scopes: str,
        redirect_uris: Optional[list[str]] = None,
        trusted: bool = False,
    ) -> dict:
        """Create an OAuth client for an organization.

        Returns dict with client_id, client_secret (plaintext, shown only once), and name.
        """
        if redirect_uris is None:
            redirect_uris = list(DEFAULT_REDIRECT_URIS)

        client_id = f"bow_client_{secrets.token_urlsafe(16)}"
        client_secret = f"bow_secret_{secrets.token_urlsafe(32)}"
        secret_hash = _hash(client_secret)

        client = OAuthClient(
            organization_id=organization_id,
            client_id=client_id,
            client_secret_hash=secret_hash,
            name=name,
            redirect_uris=json.dumps(redirect_uris),
            scopes=scopes,
            trusted=trusted,
        )
        db.add(client)
        await db.commit()
        await db.refresh(client)

        return {
            **self._client_payload(client),
            "client_secret": client_secret,
        }

    async def update_client(
        self,
        db: AsyncSession,
        client_db_id: str,
        organization_id: str,
        name: Optional[str] = None,
        redirect_uris: Optional[list[str]] = None,
        scopes: Optional[str] = None,
        trusted: Optional[bool] = None,
    ) -> Optional[dict]:
        """Update an existing client's metadata, access surfaces, and trust.

        Only fields passed (non-None) are changed. Returns the updated client
        (no secret) or None if not found in this org.
        """
        result = await db.execute(
            select(OAuthClient)
            .where(OAuthClient.id == client_db_id)
            .where(OAuthClient.organization_id == organization_id)
            .where(OAuthClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return None

        if name is not None:
            client.name = name
        if redirect_uris is not None:
            client.redirect_uris = json.dumps(redirect_uris)
        scopes_changed = (
            scopes is not None
            and set(scopes.split()) != set((client.scopes or "").split())
        )
        if scopes is not None:
            client.scopes = scopes
        if trusted is not None:
            client.trusted = trusted
        if scopes_changed:
            now = datetime.utcnow()
            # A scope edit changes the client's security boundary. Revoke both
            # pending grants and existing credentials so removed permissions
            # cannot survive through an old access or refresh token.
            await db.execute(
                update(OAuthAuthorizationCode)
                .where(OAuthAuthorizationCode.client_id == client.client_id)
                .where(OAuthAuthorizationCode.deleted_at.is_(None))
                .values(deleted_at=now)
            )
            await db.execute(
                update(OAuthAccessToken)
                .where(OAuthAccessToken.client_id == client.client_id)
                .where(OAuthAccessToken.deleted_at.is_(None))
                .values(deleted_at=now)
            )
        await db.commit()
        await db.refresh(client)

        return self._client_payload(client)

    async def list_clients(
        self,
        db: AsyncSession,
        organization_id: str,
    ) -> list[dict]:
        result = await db.execute(
            select(OAuthClient)
            .where(OAuthClient.organization_id == organization_id)
            .where(OAuthClient.deleted_at.is_(None))
            .order_by(OAuthClient.created_at.desc())
        )
        clients = result.scalars().all()
        if not clients:
            return []

        now = datetime.utcnow()
        stats_result = await db.execute(
            select(
                OAuthAccessToken.client_id,
                func.sum(case((OAuthAccessToken.expires_at > now, 1), else_=0)),
                func.max(case(
                    (
                        OAuthAccessToken.updated_at > OAuthAccessToken.created_at,
                        OAuthAccessToken.updated_at,
                    ),
                    else_=None,
                )),
                func.max(OAuthAccessToken.created_at),
            )
            .where(OAuthAccessToken.client_id.in_([c.client_id for c in clients]))
            .where(OAuthAccessToken.deleted_at.is_(None))
            .group_by(OAuthAccessToken.client_id)
        )
        stats = {
            client_id: (int(active or 0), last_used, last_issued)
            for client_id, active, last_used, last_issued in stats_result.all()
        }
        payloads = []
        for client in clients:
            active, last_used, last_issued = stats.get(client.client_id, (0, None, None))
            payloads.append(self._client_payload(
                client,
                active_token_count=active,
                last_used_at=last_used,
                last_issued_at=last_issued,
            ))
        return payloads

    async def get_client_info(
        self,
        db: AsyncSession,
        client_id: str,
    ) -> Optional[dict]:
        """Public info about a client (for consent screen)."""
        result = await db.execute(
            select(OAuthClient)
            .where(OAuthClient.client_id == client_id)
            .where(OAuthClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return None
        return {
            "client_id": client.client_id,
            "name": client.name,
            "scopes": (client.scopes or "").split(),
            "trusted": bool(client.trusted),
        }

    async def delete_client(
        self,
        db: AsyncSession,
        client_db_id: str,
        organization_id: str,
    ) -> bool:
        result = await db.execute(
            select(OAuthClient)
            .where(OAuthClient.id == client_db_id)
            .where(OAuthClient.organization_id == organization_id)
            .where(OAuthClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return False
        now = datetime.utcnow()
        client.deleted_at = now
        await db.execute(
            update(OAuthAuthorizationCode)
            .where(OAuthAuthorizationCode.client_id == client.client_id)
            .where(OAuthAuthorizationCode.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await db.execute(
            update(OAuthAccessToken)
            .where(OAuthAccessToken.client_id == client.client_id)
            .where(OAuthAccessToken.deleted_at.is_(None))
            .values(deleted_at=now)
        )
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
            select(OAuthClient)
            .where(OAuthClient.id == client_db_id)
            .where(OAuthClient.organization_id == organization_id)
            .where(OAuthClient.deleted_at.is_(None))
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
    ) -> Optional[OAuthClient]:
        """Validate client credentials. If client_secret is None, only validate client_id."""
        result = await db.execute(
            select(OAuthClient)
            .where(OAuthClient.client_id == client_id)
            .where(OAuthClient.deleted_at.is_(None))
        )
        client = result.scalar_one_or_none()
        if not client:
            return None

        if client_secret is not None:
            if _hash(client_secret) != client.client_secret_hash:
                return None

        return client

    def validate_redirect_uri(self, client: OAuthClient, redirect_uri: str) -> bool:
        allowed = json.loads(client.redirect_uris)
        return redirect_uri in allowed

    async def user_is_member_of_org(
        self,
        db: AsyncSession,
        user_id: str,
        organization_id: str,
    ) -> bool:
        """True if the user belongs to the organization.

        Used at consent time to ensure a user can only mint tokens for an org
        they actually belong to — the token's org is the client's org, so this
        is the membership gate that backs that binding.
        """
        from app.models.membership import Membership

        result = await db.execute(
            select(Membership)
            .where(Membership.user_id == str(user_id))
            .where(Membership.organization_id == str(organization_id))
            .where(Membership.deleted_at.is_(None))
        )
        return result.scalar_one_or_none() is not None

    async def _active_subject(
        self,
        db: AsyncSession,
        user_id: str,
        organization_id: str,
    ) -> Optional[Tuple[User, Organization]]:
        """Load a live user/org pair only while their binding is still valid."""
        user = (await db.execute(
            select(User).where(
                User.id == user_id,
                User.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if not user:
            return None

        organization = (await db.execute(
            select(Organization).where(
                Organization.id == organization_id,
                Organization.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not organization:
            return None

        from app.core.permission_resolver import principal_belongs_to_org

        if not await principal_belongs_to_org(db, user, organization.id):
            return None
        return user, organization

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

        auth_code = OAuthAuthorizationCode(
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
            select(OAuthAuthorizationCode)
            .where(OAuthAuthorizationCode.code == code)
            .where(OAuthAuthorizationCode.client_id == client_id)
            .where(OAuthAuthorizationCode.deleted_at.is_(None))
        )
        auth_code = result.scalar_one_or_none()
        if not auth_code:
            logger.warning("exchange_code failed: authorization code not found or already used (client_id=%s)", client_id)
            return None

        if not set((auth_code.scope or "").split()).issubset(
            set((client.scopes or "").split())
        ):
            logger.warning("exchange_code failed: authorization scope is no longer registered (client_id=%s)", client_id)
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

        if not await self._active_subject(
            db,
            auth_code.user_id,
            auth_code.organization_id,
        ):
            logger.warning("exchange_code failed: user or organization access was removed (client_id=%s)", client_id)
            return None

        # Consume the code (one-time use)
        auth_code.deleted_at = datetime.utcnow()

        # Issue tokens
        access_token, access_hash = _generate_token(ACCESS_TOKEN_PREFIX)
        refresh_token, refresh_hash = _generate_token(REFRESH_TOKEN_PREFIX)

        issued_at = datetime.utcnow()
        access_lifetime = _access_token_lifetime(auth_code.scope)
        token_record = OAuthAccessToken(
            token_hash=access_hash,
            client_id=client_id,
            user_id=auth_code.user_id,
            organization_id=auth_code.organization_id,
            scope=auth_code.scope,
            expires_at=issued_at + access_lifetime,
            refresh_token_hash=refresh_hash,
            refresh_expires_at=issued_at + REFRESH_TOKEN_LIFETIME,
            created_at=issued_at,
            updated_at=issued_at,
        )
        db.add(token_record)
        await db.commit()

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(access_lifetime.total_seconds()),
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
            select(OAuthAccessToken)
            .where(OAuthAccessToken.refresh_token_hash == refresh_hash)
            .where(OAuthAccessToken.client_id == client_id)
            .where(OAuthAccessToken.deleted_at.is_(None))
        )
        token_record = result.scalar_one_or_none()
        if not token_record:
            logger.warning("refresh_access_token failed: refresh token not found or already rotated (client_id=%s)", client_id)
            return None

        # Check refresh token expiration
        if token_record.refresh_expires_at and token_record.refresh_expires_at < datetime.utcnow():
            logger.warning("refresh_access_token failed: refresh token expired at %s (client_id=%s)", token_record.refresh_expires_at, client_id)
            return None

        if not set((token_record.scope or "").split()).issubset(
            set((client.scopes or "").split())
        ):
            logger.warning("refresh_access_token failed: token scope is no longer registered (client_id=%s)", client_id)
            return None

        if not await self._active_subject(
            db,
            token_record.user_id,
            token_record.organization_id,
        ):
            logger.warning("refresh_access_token failed: user or organization access was removed (client_id=%s)", client_id)
            return None

        # Invalidate old access token
        token_record.deleted_at = datetime.utcnow()

        # Issue new tokens
        new_access, new_access_hash = _generate_token(ACCESS_TOKEN_PREFIX)
        new_refresh, new_refresh_hash = _generate_token(REFRESH_TOKEN_PREFIX)

        issued_at = datetime.utcnow()
        access_lifetime = _access_token_lifetime(token_record.scope)
        new_record = OAuthAccessToken(
            token_hash=new_access_hash,
            client_id=client_id,
            user_id=token_record.user_id,
            organization_id=token_record.organization_id,
            scope=token_record.scope,
            expires_at=issued_at + access_lifetime,
            refresh_token_hash=new_refresh_hash,
            refresh_expires_at=issued_at + REFRESH_TOKEN_LIFETIME,
            created_at=issued_at,
            updated_at=issued_at,
        )
        db.add(new_record)
        await db.commit()

        return {
            "access_token": new_access,
            "token_type": "Bearer",
            "expires_in": int(access_lifetime.total_seconds()),
            "refresh_token": new_refresh,
            "scope": token_record.scope,
        }

    # ── Token validation (used by app and MCP surfaces) ───────────

    async def validate_access_token_context(
        self,
        db: AsyncSession,
        token: str,
        required_scope: Optional[str] = None,
    ) -> Optional[OAuthTokenContext]:
        """Validate a token and return its org-pinned security context.

        User activity, live organization membership, client revocation, expiry,
        and surface scope are re-asserted on every request. This intentionally
        makes the token a session credential, not a snapshot of old access.
        """
        if not token.startswith(ACCESS_TOKEN_PREFIX):
            return None

        token_hash = _hash(token)
        result = await db.execute(
            select(OAuthAccessToken)
            .where(OAuthAccessToken.token_hash == token_hash)
            .where(OAuthAccessToken.deleted_at.is_(None))
        )
        token_record = result.scalar_one_or_none()
        if not token_record or token_record.expires_at < datetime.utcnow():
            return None

        scopes = frozenset((token_record.scope or "").split())
        if required_scope and required_scope not in scopes:
            return None

        client = (await db.execute(
            select(OAuthClient)
            .where(OAuthClient.client_id == token_record.client_id)
            .where(OAuthClient.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not client:
            return None
        if not scopes.issubset(set((client.scopes or "").split())):
            return None

        subject = await self._active_subject(
            db,
            token_record.user_id,
            token_record.organization_id,
        )
        if not subject:
            return None
        user, organization = subject

        now = datetime.utcnow()
        if (
            token_record.updated_at <= token_record.created_at
            or now - token_record.updated_at >= TOKEN_ACTIVITY_DEBOUNCE
        ):
            token_record.updated_at = now
            try:
                await db.commit()
            except Exception:
                await db.rollback()

        return OAuthTokenContext(
            user=user,
            organization=organization,
            scopes=scopes,
            client=client,
            token_record=token_record,
        )

    async def validate_access_token(
        self,
        db: AsyncSession,
        token: str,
        required_scope: Optional[str] = None,
    ) -> Optional[Tuple[User, Organization]]:
        """Validate an OAuth access token and return (user, organization).

        Returns None if token is invalid or expired.
        """
        context = await self.validate_access_token_context(
            db,
            token,
            required_scope=required_scope,
        )
        if not context:
            return None
        return context.user, context.organization
