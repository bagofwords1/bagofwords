"""OAuth 2.1 Authorization Server service.

Handles client registration, authorization code issuance, token exchange,
and token validation for external OAuth clients.
"""

import base64
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oauth_server import (
    OAuthAccessToken,
    OAuthAuthorizationCode,
    OAuthClient,
)
from app.models.organization import Organization
from app.models.user import User
from app.settings.config import settings

logger = logging.getLogger(__name__)

# Token prefixes for easy identification in auth middleware
ACCESS_TOKEN_PREFIX = "bow_oauth_"
REFRESH_TOKEN_PREFIX = "bow_rt_"

# Token lifetimes (access / refresh lifetimes live in bow_config.oauth_server)
AUTHORIZATION_CODE_LIFETIME = timedelta(minutes=5)
TOKEN_ACTIVITY_DEBOUNCE = timedelta(hours=1)

# Default redirect URIs (covers Claude Web and common MCP inspector tools).
DEFAULT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:6274/oauth/callback",
    "http://localhost:6274/oauth/callback/debug",
]

# Self-registered (RFC 7591) clients only ever get the MCP surface.
DYNAMIC_CLIENT_SCOPE = "mcp"
DYNAMIC_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
MAX_DYNAMIC_REDIRECT_URIS = 10


class OAuthAuthorizationError(Exception):
    """Consent cannot be granted; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.utcnow()


def _cfg():
    return settings.bow_config.oauth_server


def dynamic_registration_enabled() -> bool:
    return bool(_cfg().allow_dynamic_client_registration)


def org_allows_dynamic_clients(organization: Organization) -> bool:
    """Org kill switch for self-registered clients (default on)."""
    org_settings = getattr(organization, "settings", None)
    if org_settings is None:
        return True
    feature = org_settings.get_config("allow_dynamic_oauth_clients")
    if feature is None:
        return True
    value = feature.get("value") if isinstance(feature, dict) else getattr(feature, "value", feature)
    return True if value is None else bool(value)


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
    """App-capable tokens use the app lifetime, including mixed tokens."""
    cfg = _cfg()
    seconds = (
        cfg.app_access_token_ttl_seconds
        if "app" in scope.split()
        else cfg.mcp_access_token_ttl_seconds
    )
    return timedelta(seconds=seconds)


def _refresh_expiry(now: datetime, session_started_at: datetime) -> datetime:
    """Sliding refresh expiry, clamped to the session's absolute cap."""
    cfg = _cfg()
    return min(
        now + timedelta(days=cfg.refresh_token_ttl_days),
        session_started_at + timedelta(days=cfg.refresh_session_max_days),
    )


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
            "dynamic": client.is_dynamic,
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

    async def register_dynamic_client(
        self,
        db: AsyncSession,
        client_name: str,
        redirect_uris: list[str],
        token_endpoint_auth_method: str,
    ) -> dict:
        """RFC 7591 registration. The caller validates the metadata.

        The client belongs to no organization and is never trusted; it can
        only request the MCP scope. Returns the RFC 7591 response body.
        """
        await self._sweep_unused_dynamic_clients(db)

        client_id = f"bow_client_{secrets.token_urlsafe(16)}"
        client_secret = None
        secret_hash = None
        if token_endpoint_auth_method != "none":
            client_secret = f"bow_secret_{secrets.token_urlsafe(32)}"
            secret_hash = _hash(client_secret)

        client = OAuthClient(
            organization_id=None,
            client_id=client_id,
            client_secret_hash=secret_hash,
            name=client_name,
            redirect_uris=json.dumps(redirect_uris),
            scopes=DYNAMIC_CLIENT_SCOPE,
            trusted=False,
            registration_type="dynamic",
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
        db.add(client)
        await db.commit()
        await db.refresh(client)

        body = {
            "client_id": client_id,
            "client_id_issued_at": int((client.created_at or _utcnow()).replace(tzinfo=timezone.utc).timestamp()),
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "scope": DYNAMIC_CLIENT_SCOPE,
        }
        if client_secret:
            body["client_secret"] = client_secret
            body["client_secret_expires_at"] = 0
        return body

    async def _sweep_unused_dynamic_clients(self, db: AsyncSession) -> None:
        """Drop self-registered clients that never completed a sign-in.

        Registration is unauthenticated, so abandoned registrations would
        otherwise accumulate forever. Runs opportunistically on each new
        registration.
        """
        cutoff = _utcnow() - timedelta(days=_cfg().dynamic_client_unused_ttl_days)
        await db.execute(
            update(OAuthClient)
            .where(OAuthClient.registration_type == "dynamic")
            .where(OAuthClient.deleted_at.is_(None))
            .where(OAuthClient.created_at < cutoff)
            .where(~exists().where(OAuthAccessToken.client_id == OAuthClient.client_id))
            .values(deleted_at=_utcnow())
            .execution_options(synchronize_session=False)
        )

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
            now = _utcnow()
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
        """Static clients owned by the org, plus self-registered clients that
        hold live tokens in it (those have no owning org; they show up for
        each org a member connected them to)."""
        now = _utcnow()
        # A token keeps its client connected until its refresh token lapses;
        # the access token alone expires within the hour.
        live_until = func.coalesce(OAuthAccessToken.refresh_expires_at, OAuthAccessToken.expires_at)
        connected_dynamic = (
            select(OAuthAccessToken.client_id)
            .where(OAuthAccessToken.organization_id == organization_id)
            .where(OAuthAccessToken.deleted_at.is_(None))
            .where(live_until > now)
        )
        result = await db.execute(
            select(OAuthClient)
            .where(OAuthClient.deleted_at.is_(None))
            .where(or_(
                OAuthClient.organization_id == organization_id,
                and_(
                    OAuthClient.registration_type == "dynamic",
                    OAuthClient.client_id.in_(connected_dynamic),
                ),
            ))
            .order_by(OAuthClient.created_at.desc())
        )
        clients = result.scalars().all()
        if not clients:
            return []

        stats_result = await db.execute(
            select(
                OAuthAccessToken.client_id,
                func.sum(case((live_until > now, 1), else_=0)),
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
            .where(OAuthAccessToken.organization_id == organization_id)
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
            # The consent page asks the user to pick an org for these and
            # marks the (self-chosen) name as unverified.
            "dynamic": client.is_dynamic,
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
        now = _utcnow()
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

    async def revoke_client_access(
        self,
        db: AsyncSession,
        client_db_id: str,
        organization_id: str,
    ) -> bool:
        """Revoke every grant a client holds in one org, keeping the client.

        This is how an org admin disconnects a self-registered client, which
        no single org owns and so cannot be deleted from one org's settings.
        """
        client = (await db.execute(
            select(OAuthClient)
            .where(OAuthClient.id == client_db_id)
            .where(OAuthClient.deleted_at.is_(None))
        )).scalar_one_or_none()
        if not client:
            return False
        if not client.is_dynamic and client.organization_id != organization_id:
            return False
        now = _utcnow()
        await db.execute(
            update(OAuthAuthorizationCode)
            .where(OAuthAuthorizationCode.client_id == client.client_id)
            .where(OAuthAuthorizationCode.organization_id == organization_id)
            .where(OAuthAuthorizationCode.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        revoked = await db.execute(
            update(OAuthAccessToken)
            .where(OAuthAccessToken.client_id == client.client_id)
            .where(OAuthAccessToken.organization_id == organization_id)
            .where(OAuthAccessToken.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await db.commit()
        # A dynamic client with nothing in this org is not this org's to see.
        return not client.is_dynamic or revoked.rowcount > 0

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
        if client.is_dynamic and not dynamic_registration_enabled():
            return None

        if client_secret is not None:
            if _hash(client_secret) != client.client_secret_hash:
                return None

        return client

    async def authenticate_client(
        self,
        db: AsyncSession,
        client_id: str,
        client_secret: Optional[str],
    ) -> Optional[OAuthClient]:
        """Client authentication at the token endpoint.

        Clients that registered a ``token_endpoint_auth_method`` are held to
        it: a confidential client must present its secret. Clients created
        before the method was recorded keep the old rule (a secret, when sent,
        must match; PKCE protects the code either way).
        """
        client = await self.validate_client(db, client_id)
        if not client:
            return None
        method = client.token_endpoint_auth_method
        if method == "none":
            return client
        if method is None and client_secret is None:
            return client
        if not client_secret or not client.client_secret_hash:
            return None
        if not secrets.compare_digest(_hash(client_secret), client.client_secret_hash):
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

    async def resolve_consent_organization(
        self,
        db: AsyncSession,
        client: OAuthClient,
        user: User,
        requested_organization_id: Optional[str],
    ) -> str:
        """Pick the org a consent binds to, or raise OAuthAuthorizationError.

        A static client's org is fixed: it is the org that registered it. A
        self-registered client has none, so the user chooses one of their
        orgs (or it is implied when they belong to exactly one), and that org
        must allow self-registered clients.
        """
        if not client.is_dynamic:
            organization_id = client.organization_id
            if requested_organization_id and str(requested_organization_id) != str(organization_id):
                raise OAuthAuthorizationError("organization_mismatch")
            if not await self.user_is_member_of_org(db, user.id, organization_id):
                raise OAuthAuthorizationError("not_a_member", status_code=403)
            return organization_id

        from app.models.membership import Membership

        if requested_organization_id:
            organization_id = str(requested_organization_id)
            if not await self.user_is_member_of_org(db, user.id, organization_id):
                raise OAuthAuthorizationError("not_a_member", status_code=403)
        else:
            org_ids = (await db.execute(
                select(Membership.organization_id)
                .where(Membership.user_id == str(user.id))
                .where(Membership.deleted_at.is_(None))
            )).scalars().all()
            if len(org_ids) != 1:
                raise OAuthAuthorizationError("organization_required")
            organization_id = org_ids[0]

        organization = (await db.execute(
            select(Organization)
            .where(Organization.id == organization_id)
            .where(Organization.deleted_at.is_(None))
        )).unique().scalar_one_or_none()
        if not organization:
            raise OAuthAuthorizationError("not_a_member", status_code=403)
        if not org_allows_dynamic_clients(organization):
            raise OAuthAuthorizationError("dynamic_clients_disabled", status_code=403)
        return organization_id

    @staticmethod
    def _client_allowed_in_org(client: OAuthClient, organization: Organization) -> bool:
        if client.is_dynamic:
            return org_allows_dynamic_clients(organization)
        return str(client.organization_id) == str(organization.id)

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
            expires_at=_utcnow() + AUTHORIZATION_CODE_LIFETIME,
        )
        db.add(auth_code)
        await db.commit()
        return code

    # ── Token exchange ─────────────────────────────────────────────

    async def _issue_tokens(
        self,
        db: AsyncSession,
        *,
        client_id: str,
        user_id: str,
        organization_id: str,
        scope: str,
        family_id: str,
        session_started_at: datetime,
        now: datetime,
    ) -> dict:
        """Add a new access/refresh pair to the session (caller commits)."""
        access_token, access_hash = _generate_token(ACCESS_TOKEN_PREFIX)
        refresh_token, refresh_hash = _generate_token(REFRESH_TOKEN_PREFIX)
        access_lifetime = _access_token_lifetime(scope)
        db.add(OAuthAccessToken(
            token_hash=access_hash,
            client_id=client_id,
            user_id=user_id,
            organization_id=organization_id,
            scope=scope,
            expires_at=now + access_lifetime,
            refresh_token_hash=refresh_hash,
            refresh_expires_at=_refresh_expiry(now, session_started_at),
            family_id=family_id,
            session_started_at=session_started_at,
            created_at=now,
            updated_at=now,
        ))
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(access_lifetime.total_seconds()),
            "refresh_token": refresh_token,
            "scope": scope,
        }

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
        client = await self.authenticate_client(db, client_id, client_secret)
        if not client:
            logger.warning("exchange_code failed: invalid client_id=%s or client authentication failed", client_id)
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
        now = _utcnow()
        if auth_code.expires_at < now:
            logger.warning("exchange_code failed: authorization code expired at %s (client_id=%s)", auth_code.expires_at, client_id)
            auth_code.deleted_at = now
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

        subject = await self._active_subject(
            db,
            auth_code.user_id,
            auth_code.organization_id,
        )
        if not subject:
            logger.warning("exchange_code failed: user or organization access was removed (client_id=%s)", client_id)
            return None
        if not self._client_allowed_in_org(client, subject[1]):
            logger.warning("exchange_code failed: client is not allowed in the organization (client_id=%s)", client_id)
            return None

        # Consume the code (one-time use)
        auth_code.deleted_at = now

        response = await self._issue_tokens(
            db,
            client_id=client_id,
            user_id=auth_code.user_id,
            organization_id=auth_code.organization_id,
            scope=auth_code.scope,
            family_id=str(uuid.uuid4()),
            session_started_at=now,
            now=now,
        )
        await db.commit()
        return response

    async def _revoke_family(self, db: AsyncSession, family_id: str, now: datetime) -> None:
        await db.execute(
            update(OAuthAccessToken)
            .where(or_(OAuthAccessToken.family_id == family_id, OAuthAccessToken.id == family_id))
            .where(OAuthAccessToken.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await db.commit()

    async def _family_is_live(self, db: AsyncSession, family_id: str) -> bool:
        live = (await db.execute(
            select(OAuthAccessToken.id)
            .where(or_(OAuthAccessToken.family_id == family_id, OAuthAccessToken.id == family_id))
            .where(OAuthAccessToken.deleted_at.is_(None))
            .limit(1)
        )).scalar_one_or_none()
        return live is not None

    async def refresh_access_token(
        self,
        db: AsyncSession,
        refresh_token: str,
        client_id: str,
        client_secret: Optional[str],
    ) -> Optional[dict]:
        """Rotate a refresh token into a new access + refresh pair.

        The presented token is retired on use. Presenting it again is either
        a client retry / race (within the grace window while the session is
        still live: a fresh pair is issued) or a replay of a leaked token
        (anything else: the whole session family is revoked).
        """
        client = await self.authenticate_client(db, client_id, client_secret)
        if not client:
            logger.warning("refresh_access_token failed: invalid client_id=%s or client authentication failed", client_id)
            return None

        token_record = (await db.execute(
            select(OAuthAccessToken)
            .where(OAuthAccessToken.refresh_token_hash == _hash(refresh_token))
            .where(OAuthAccessToken.client_id == client_id)
        )).scalar_one_or_none()
        if not token_record:
            logger.warning("refresh_access_token failed: refresh token not found (client_id=%s)", client_id)
            return None

        now = _utcnow()
        family_id = token_record.family_id or token_record.id
        if token_record.deleted_at is not None:
            if not await self._is_retry_of_rotation(db, token_record, family_id, now):
                return None
        else:
            # Retire the token atomically: if another request rotated it
            # between our read and this write, treat ours as that race.
            retired = await db.execute(
                update(OAuthAccessToken)
                .where(OAuthAccessToken.id == token_record.id)
                .where(OAuthAccessToken.deleted_at.is_(None))
                .values(deleted_at=now, rotated_at=now)
                .execution_options(synchronize_session=False)
            )
            if retired.rowcount != 1:
                # Nothing was written; re-read the row the winner rotated.
                # (No rollback: it would expire ``client`` mid-request.)
                await db.refresh(token_record)
                if not await self._is_retry_of_rotation(db, token_record, family_id, now):
                    return None

        if token_record.refresh_expires_at and token_record.refresh_expires_at < now:
            logger.warning("refresh_access_token failed: refresh token expired at %s (client_id=%s)", token_record.refresh_expires_at, client_id)
            await db.rollback()
            return None

        session_started_at = token_record.session_started_at or token_record.created_at or now
        if session_started_at + timedelta(days=_cfg().refresh_session_max_days) <= now:
            logger.warning("refresh_access_token failed: session reached its maximum lifetime (client_id=%s)", client_id)
            await db.rollback()
            return None

        if not set((token_record.scope or "").split()).issubset(
            set((client.scopes or "").split())
        ):
            logger.warning("refresh_access_token failed: token scope is no longer registered (client_id=%s)", client_id)
            await db.rollback()
            return None

        subject = await self._active_subject(
            db,
            token_record.user_id,
            token_record.organization_id,
        )
        if not subject:
            logger.warning("refresh_access_token failed: user or organization access was removed (client_id=%s)", client_id)
            await db.rollback()
            return None
        if not self._client_allowed_in_org(client, subject[1]):
            logger.warning("refresh_access_token failed: client is not allowed in the organization (client_id=%s)", client_id)
            await db.rollback()
            return None

        response = await self._issue_tokens(
            db,
            client_id=client_id,
            user_id=token_record.user_id,
            organization_id=token_record.organization_id,
            scope=token_record.scope,
            family_id=family_id,
            session_started_at=session_started_at,
            now=now,
        )
        await db.commit()
        return response

    async def _is_retry_of_rotation(
        self,
        db: AsyncSession,
        token_record: OAuthAccessToken,
        family_id: str,
        now: datetime,
    ) -> bool:
        """A retired refresh token came back. True if it is a benign retry."""
        if token_record.rotated_at is None:
            # Revoked (sign-out, client deleted, scope change), not rotated.
            logger.warning("refresh_access_token failed: refresh token was revoked (client_id=%s)", token_record.client_id)
            return False
        grace = timedelta(seconds=_cfg().refresh_reuse_grace_seconds)
        if now - token_record.rotated_at <= grace and await self._family_is_live(db, family_id):
            logger.info("refresh_access_token: rotated token reused within grace window (client_id=%s)", token_record.client_id)
            return True
        logger.warning(
            "refresh_access_token: rotated refresh token replayed; revoking token family (client_id=%s)",
            token_record.client_id,
        )
        await self._revoke_family(db, family_id, now)
        return False

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
        if not token_record or token_record.expires_at < _utcnow():
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
        if client.is_dynamic and not dynamic_registration_enabled():
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
        if not self._client_allowed_in_org(client, organization):
            return None

        now = _utcnow()
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
