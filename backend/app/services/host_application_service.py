"""Host applications: configuration, silent sign-in and embed sessions.

See docs/design/embedded-artifacts.md and app/models/host_application.py for
the trust model. Sign-in modes (one per host application):

* ``assertion`` -- the host backend signs an RS256 JWT (``iss`` = configured
  issuer, ``aud`` = host application id, ``sub``, ``iat``, ``exp`` at most
  2 minutes later, single-use ``jti``) with the private key BOW generated.
* ``idp_token`` -- the host passes a token issued by an OIDC provider BOW
  already uses for login, addressed to the configured audience. Verified
  against the provider's own discovery document and keys.
* ``sso`` -- the viewer signs in to BOW in a top-level popup; the popup gets a
  single-use, PKCE-bound code that the embedded page exchanges.

Every mode ends in host_identity.resolve_user (or, for sso, the signed-in BOW
user) and an embed session: a ``bow_embed_`` token in
``oauth_mcp_access_tokens`` (scope ``embed``), re-checked on every request and
accepted only by the routes in app/core/embed_auth.py.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import jwt
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.host_application import ArtifactHostApplication, HostApplication, HostAssertionReplay
from app.models.oauth_server import OAuthAccessToken, OAuthAuthorizationCode
from app.models.organization import Organization
from app.models.user import User
from app.schemas.host_application_schema import HostApplicationCreate, HostApplicationUpdate
from app.services import host_identity

logger = logging.getLogger(__name__)

ASSERTION_ALGORITHMS = ("RS256",)
ASSERTION_MAX_LIFETIME = timedelta(minutes=2)
LEEWAY = timedelta(seconds=60)
SESSION_LIFETIME = timedelta(hours=1)
# Dev/test only: BOW_EMBED_SESSION_LIFETIME_SECONDS shortens embed sessions so a
# renewal can be watched in minutes. Clamped to 60 s - 1 h; unset or unreadable
# is the hour above.
SESSION_LIFETIME_RANGE = (60, 3600)
SSO_CODE_LIFETIME = timedelta(seconds=60)
SESSION_PREFIX = "bow_embed_"
SESSION_SCOPE = "embed"
PUBLIC_ID_PREFIX = "host_"
IDP_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


def _session_lifetime() -> timedelta:
    try:
        seconds = int(os.environ.get("BOW_EMBED_SESSION_LIFETIME_SECONDS", ""))
    except ValueError:
        return SESSION_LIFETIME
    low, high = SESSION_LIFETIME_RANGE
    return timedelta(seconds=min(high, max(low, seconds)))


def generate_keypair() -> Tuple[str, str, str]:
    """(private_pem, public_pem, fingerprint). RSA-2048: every stack can sign RS256."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem, _hash(public_pem)[:32]


def _invalid(reason: str, message: str = "The sign-in from the host application was not accepted") -> AppError:
    return AppError.unauthorized(ErrorCode.EMBED_ASSERTION_INVALID, message, reason=reason)


def _config_invalid(message: str, field: str) -> AppError:
    return AppError.bad_request(ErrorCode.HOST_APP_CONFIG_INVALID, message, field=field)


@dataclass
class EmbedContext:
    host: HostApplication
    user: User
    organization: Organization
    token_record: OAuthAccessToken


class HostApplicationService:

    # ── configuration ─────────────────────────────────────────────────

    async def list(self, db: AsyncSession, organization) -> List[HostApplication]:
        return list((await db.execute(select(HostApplication).where(
            HostApplication.organization_id == str(organization.id), HostApplication.deleted_at.is_(None),
        ).order_by(HostApplication.created_at.asc()))).scalars().all())

    async def get(self, db: AsyncSession, organization, host_id: str) -> HostApplication:
        host = (await db.execute(select(HostApplication).where(
            HostApplication.id == str(host_id), HostApplication.organization_id == str(organization.id),
            HostApplication.deleted_at.is_(None)))).scalar_one_or_none()
        if host is None:
            raise AppError.not_found(ErrorCode.HOST_APP_NOT_FOUND, "Host application not found")
        return host

    async def by_public_id(self, db: AsyncSession, public_id: str) -> Optional[HostApplication]:
        return (await db.execute(select(HostApplication).where(
            HostApplication.public_id == str(public_id), HostApplication.deleted_at.is_(None)))).scalar_one_or_none()

    @staticmethod
    def _check_identity(organization_id: str, mode: str, source: Optional[str], issuer: Optional[str],
                        audience: Optional[str], create_users: bool) -> None:
        if mode == "assertion" and not issuer:
            raise _config_invalid("Assertions need an issuer", "issuer")
        if mode == "idp_token" and not audience:
            raise _config_invalid("IdP tokens need an audience", "audience")
        host_identity.validate_source(source, organization_id, mode=mode, create_users=create_users)

    async def create(self, db: AsyncSession, organization, user, payload: HostApplicationCreate):
        """Returns (host, private_key_pem or None). The private key is never stored."""
        source = None if payload.identity_mode == "sso" else payload.identity_source
        create_users = bool(payload.create_users) and payload.identity_mode != "sso"
        self._check_identity(str(organization.id), payload.identity_mode, source, payload.issuer,
                             payload.audience, create_users)
        private_pem = public_pem = fingerprint = None
        if payload.identity_mode == "assertion":
            private_pem, public_pem, fingerprint = generate_keypair()
        host = HostApplication(
            organization_id=str(organization.id), public_id=PUBLIC_ID_PREFIX + secrets.token_hex(10),
            name=payload.name, enabled=payload.enabled, allowed_origins=list(payload.allowed_origins),
            identity_mode=payload.identity_mode, identity_source=source, create_users=create_users,
            issuer=payload.issuer if payload.identity_mode == "assertion" else None,
            audience=payload.audience if payload.identity_mode == "idp_token" else None,
            public_key=public_pem, key_fingerprint=fingerprint, epoch=1, revision=1,
            created_by_user_id=str(user.id),
        )
        db.add(host)
        await db.commit()
        await db.refresh(host)
        return host, private_pem

    async def update(self, db: AsyncSession, organization, host_id: str, payload: HostApplicationUpdate):
        """Returns (host, private_key_pem or None): switching to assertion mode
        generates the first key pair."""
        host = await self.get(db, organization, host_id)
        if int(payload.revision) != int(host.revision):
            raise AppError.conflict(ErrorCode.HOST_APP_REVISION_CONFLICT, "Changed by someone else",
                                    revision=host.revision)
        before = (host.enabled, host.identity_mode, host.identity_source, host.issuer, host.audience,
                  host.create_users)
        if payload.name is not None:
            host.name = payload.name.strip()
        if payload.allowed_origins is not None:
            host.allowed_origins = list(payload.allowed_origins)
        if payload.enabled is not None:
            host.enabled = bool(payload.enabled)
        mode = payload.identity_mode or host.identity_mode
        source = host.identity_source if payload.identity_source is None else payload.identity_source
        issuer = host.issuer if payload.issuer is None else (payload.issuer or None)
        audience = host.audience if payload.audience is None else (payload.audience or None)
        create_users = host.create_users if payload.create_users is None else bool(payload.create_users)
        if mode == "sso":
            source, create_users = None, False
        self._check_identity(str(organization.id), mode, source, issuer, audience, create_users)
        host.identity_mode, host.identity_source, host.create_users = mode, source, create_users
        host.issuer = issuer if mode == "assertion" else None
        host.audience = audience if mode == "idp_token" else None
        private_pem = None
        if mode == "assertion" and not host.public_key:
            private_pem, host.public_key, host.key_fingerprint = generate_keypair()
        after = (host.enabled, host.identity_mode, host.identity_source, host.issuer, host.audience,
                 host.create_users)
        if before != after:
            # Disabling, or changing who may sign in and how, ends every session.
            host.epoch = int(host.epoch or 1) + 1
        host.revision = int(host.revision or 1) + 1
        await db.commit()
        await db.refresh(host)
        return host, private_pem

    async def rotate_key(self, db: AsyncSession, organization, host_id: str):
        host = await self.get(db, organization, host_id)
        if host.identity_mode != "assertion":
            raise _config_invalid("Only assertion mode uses a signing key", "identity_mode")
        private_pem, host.public_key, host.key_fingerprint = generate_keypair()
        host.epoch = int(host.epoch or 1) + 1
        host.revision = int(host.revision or 1) + 1
        await db.commit()
        await db.refresh(host)
        return host, private_pem

    async def revoke_sessions(self, db: AsyncSession, organization, host_id: str) -> HostApplication:
        host = await self.get(db, organization, host_id)
        host.epoch = int(host.epoch or 1) + 1
        await db.commit()
        await db.refresh(host)
        return host

    async def delete(self, db: AsyncSession, organization, host_id: str) -> None:
        host = await self.get(db, organization, host_id)
        host.deleted_at = _now()
        host.enabled = False
        host.epoch = int(host.epoch or 1) + 1
        await db.execute(delete(ArtifactHostApplication).where(
            ArtifactHostApplication.host_application_id == str(host.id)))
        await db.commit()

    async def counters(self, db: AsyncSession, host: HostApplication) -> Dict[str, int]:
        contexts = (await db.execute(select(OAuthAccessToken.exchange_context).where(
            OAuthAccessToken.host_application_id == str(host.id), OAuthAccessToken.deleted_at.is_(None),
            OAuthAccessToken.expires_at > _now()))).scalars().all()
        live = sum(1 for ctx in contexts if (ctx or {}).get("host_epoch") == host.epoch)
        artifacts = (await db.execute(select(func.count(ArtifactHostApplication.id)).where(
            ArtifactHostApplication.host_application_id == str(host.id),
            ArtifactHostApplication.deleted_at.is_(None)))).scalar() or 0
        return {"active_sessions": live, "artifacts": int(artifacts)}

    # ── the artifact side of the opt-in ───────────────────────────────

    async def artifact_host_ids(self, db: AsyncSession, artifact_id: str) -> List[str]:
        return [str(x) for x in (await db.execute(select(ArtifactHostApplication.host_application_id).where(
            ArtifactHostApplication.artifact_id == str(artifact_id),
            ArtifactHostApplication.deleted_at.is_(None)))).scalars().all()]

    async def allows(self, db: AsyncSession, artifact_id: str, host_id: str) -> bool:
        return (await db.execute(select(ArtifactHostApplication.id).where(
            ArtifactHostApplication.artifact_id == str(artifact_id),
            ArtifactHostApplication.host_application_id == str(host_id),
            ArtifactHostApplication.deleted_at.is_(None)).limit(1))).first() is not None

    async def set_artifact_hosts(self, db: AsyncSession, artifact, host_ids: List[str], user) -> List[str]:
        wanted = []
        for hid in host_ids:
            hid = str(hid)
            if hid in wanted:
                continue
            host = (await db.execute(select(HostApplication).where(
                HostApplication.id == hid, HostApplication.organization_id == str(artifact.organization_id),
                HostApplication.deleted_at.is_(None)))).scalar_one_or_none()
            if host is None:
                raise AppError.bad_request(ErrorCode.HOST_APP_NOT_FOUND, "Unknown host application",
                                           field="host_application_ids")
            wanted.append(hid)
        current = set(await self.artifact_host_ids(db, artifact.id))
        for hid in set(wanted) - current:
            db.add(ArtifactHostApplication(artifact_id=str(artifact.id), host_application_id=hid,
                                           created_by_user_id=str(user.id)))
        if current - set(wanted):
            await db.execute(delete(ArtifactHostApplication).where(
                ArtifactHostApplication.artifact_id == str(artifact.id),
                ArtifactHostApplication.host_application_id.in_(list(current - set(wanted)))))
        await db.commit()
        return wanted

    async def frame_origins(self, db: AsyncSession, artifact_id: str, public_id: str) -> Optional[List[str]]:
        """The origins that may frame this artifact for this host application,
        or None when the host does not exist, is disabled, or the artifact
        does not allow it."""
        host = await self.by_public_id(db, public_id)
        if host is None or not host.enabled or not await self.allows(db, artifact_id, host.id):
            return None
        return list(host.allowed_origins or [])

    # ── sign-in ───────────────────────────────────────────────────────

    async def sign_in(self, db: AsyncSession, host: HostApplication, *, assertion=None, id_token=None,
                      sso_code=None, code_verifier=None) -> User:
        """The BOW user the presented credential stands for. One credential,
        matching the host's identity mode; typed refusals otherwise."""
        if not host.enabled:
            raise AppError.forbidden(ErrorCode.HOST_APP_DISABLED, "This host application is disabled")
        presented = [k for k, v in (("assertion", assertion), ("id_token", id_token), ("sso_code", sso_code)) if v]
        expected = {"assertion": "assertion", "idp_token": "id_token", "sso": "sso_code"}[host.identity_mode]
        if presented != [expected]:
            raise _invalid("wrong_credential", f"This host application signs in with {expected}")
        if host.identity_mode == "assertion":
            claims = self.decode_assertion(host, assertion)
            await self._consume_jti(db, host, claims)
            return await host_identity.resolve_user(db, host, claims["sub"], claims["raw"])
        if host.identity_mode == "idp_token":
            claims, subject = await self.verify_idp_token(host, id_token)
            return await host_identity.resolve_user(db, host, subject, claims)
        return await self._redeem_sso_code(db, host, sso_code, code_verifier)

    @staticmethod
    def decode_assertion(host: HostApplication, assertion: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Signature, algorithm, issuer, audience, time window and required
        claims. Pure, so the whole matrix is unit-testable."""
        if not host.public_key:
            raise _invalid("no_key")
        try:
            claims = jwt.decode(
                assertion, host.public_key, algorithms=list(ASSERTION_ALGORITHMS), audience=host.public_id,
                issuer=host.issuer, leeway=LEEWAY.total_seconds(),
                options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise _invalid("expired")
        except jwt.ImmatureSignatureError:
            raise _invalid("not_yet_valid")
        except jwt.InvalidAudienceError:
            raise _invalid("audience")
        except jwt.InvalidIssuerError:
            raise _invalid("issuer")
        except jwt.MissingRequiredClaimError:
            raise _invalid("missing_claim")
        except jwt.InvalidAlgorithmError:
            raise _invalid("algorithm")
        except jwt.PyJWTError:
            raise _invalid("signature")
        sub, jti = str(claims.get("sub") or "").strip(), str(claims.get("jti") or "").strip()
        if not sub or not jti or len(jti) > 255 or len(sub) > 512:
            raise _invalid("claims")
        try:
            iat = datetime.utcfromtimestamp(int(claims["iat"]))
            exp = datetime.utcfromtimestamp(int(claims["exp"]))
        except (TypeError, ValueError, OverflowError, OSError):
            raise _invalid("claims")
        current = now or _now()
        if iat > current + LEEWAY:
            raise _invalid("not_yet_valid")
        if exp - iat > ASSERTION_MAX_LIFETIME:
            raise _invalid("lifetime")
        return {"sub": sub, "jti": jti, "exp": exp, "raw": claims}

    async def _consume_jti(self, db: AsyncSession, host: HostApplication, claims: Dict[str, Any]) -> None:
        await db.execute(delete(HostAssertionReplay).where(HostAssertionReplay.expires_at < _now() - LEEWAY))
        db.add(HostAssertionReplay(host_application_id=str(host.id), jti=claims["jti"],
                                   expires_at=claims["exp"] + LEEWAY))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise AppError.unauthorized(ErrorCode.EMBED_ASSERTION_REPLAYED, "This sign-in was already used")

    async def verify_idp_token(self, host: HostApplication, token: str) -> Tuple[Dict[str, Any], str]:
        """Claims of a token from the host's OIDC provider, verified with the
        provider's published keys. The provider vouches; the host only relays."""
        from app.services.entra_token_exchange import _json

        name = (host.identity_source or "").partition(":")[2]
        provider = host_identity._oidc_provider(name)
        if provider is None:
            raise AppError.bad_request(ErrorCode.EMBED_IDENTITY_UNAVAILABLE, "The identity provider is not available")
        if not isinstance(token, str) or len(token) > 32768:
            raise _invalid("token")
        issuer = provider.issuer.rstrip("/")
        try:
            metadata = await _json(f"{issuer}/.well-known/openid-configuration")
        except Exception:
            raise AppError.bad_request(ErrorCode.EMBED_IDENTITY_UNAVAILABLE, "The identity provider is unreachable")
        if str(metadata.get("issuer", "")).rstrip("/") != issuer:
            raise _invalid("issuer")
        jwks_uri = str(metadata.get("jwks_uri") or "")
        if urlsplit(jwks_uri).netloc != urlsplit(issuer).netloc or urlsplit(jwks_uri).scheme != urlsplit(issuer).scheme:
            raise _invalid("key_authority")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise _invalid("token")
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in IDP_ALGORITHMS or not isinstance(kid, str):
            raise _invalid("algorithm")
        key = None
        for refresh in (False, True):
            try:
                keys = await _json(jwks_uri, refresh=refresh)
            except Exception:
                raise AppError.bad_request(ErrorCode.EMBED_IDENTITY_UNAVAILABLE, "The identity provider is unreachable")
            key = next((k for k in keys.get("keys", []) if k.get("kid") == kid), None)
            if key:
                break
        if not key:
            raise _invalid("unknown_key")
        try:
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=[alg], audience=host.audience,
                                issuer=metadata["issuer"], leeway=LEEWAY.total_seconds(),
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError:
            raise _invalid("expired")
        except jwt.InvalidAudienceError:
            raise _invalid("audience")
        except jwt.InvalidIssuerError:
            raise _invalid("issuer")
        except jwt.PyJWTError:
            raise _invalid("signature")
        subject = str(claims.get(provider.uid_claim or "sub") or "").strip()
        if not subject:
            raise _invalid("claims")
        return claims, subject

    async def issue_sso_code(self, db: AsyncSession, host: HostApplication, user: User, code_challenge: str) -> str:
        """A single-use code for the signed-in BOW user, bound to the embedded
        page's PKCE challenge. Issued only to members of the host's organization."""
        from app.core.permission_resolver import principal_belongs_to_org
        from app.core.session_revocation import lock_user_sessions

        if not host.enabled:
            raise AppError.forbidden(ErrorCode.HOST_APP_DISABLED, "This host application is disabled")
        if host.identity_mode != "sso":
            raise _invalid("wrong_credential", "This host application does not sign in through BOW")
        if not await principal_belongs_to_org(db, user, host.organization_id):
            raise AppError.forbidden(ErrorCode.EMBED_IDENTITY_UNMAPPED, "Your account is not a member of this workspace")
        await lock_user_sessions(db, str(user.id))
        code = secrets.token_urlsafe(32)
        db.add(OAuthAuthorizationCode(
            code=_hash(code), client_id=f"host:{host.public_id}"[:64], user_id=str(user.id),
            organization_id=str(host.organization_id), redirect_uri="embed", scope=SESSION_SCOPE,
            code_challenge=code_challenge, expires_at=_now() + SSO_CODE_LIFETIME,
        ))
        await db.commit()
        return code

    async def _redeem_sso_code(self, db: AsyncSession, host: HostApplication, code: str, verifier: Optional[str]) -> User:
        row = (await db.execute(select(OAuthAuthorizationCode).where(
            OAuthAuthorizationCode.code == _hash(code or ""), OAuthAuthorizationCode.deleted_at.is_(None),
            OAuthAuthorizationCode.client_id == f"host:{host.public_id}"[:64]))).scalar_one_or_none()
        if row is None or row.expires_at < _now():
            raise _invalid("code")
        # Single use whatever happens next.
        row.deleted_at = _now()
        await db.commit()
        digest = hashlib.sha256((verifier or "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != row.code_challenge:
            raise _invalid("code_verifier")
        return await host_identity._active_member(db, str(row.user_id), str(host.organization_id))

    # ── sessions ──────────────────────────────────────────────────────

    async def issue_session(self, db: AsyncSession, host: HostApplication, user: User) -> Tuple[str, OAuthAccessToken]:
        token = SESSION_PREFIX + secrets.token_urlsafe(32)
        record = OAuthAccessToken(
            token_hash=_hash(token), client_id=f"host:{host.public_id}"[:64], user_id=str(user.id),
            organization_id=str(host.organization_id), scope=SESSION_SCOPE,
            expires_at=_now() + _session_lifetime(), host_application_id=str(host.id),
            exchange_context={"host_epoch": int(host.epoch or 1),
                              "user_epoch": int(getattr(user, "session_epoch", 0) or 0)},
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return token, record

    async def resolve_session(self, db: AsyncSession, token: str) -> Optional[EmbedContext]:
        """A live session or None. Re-asserted on every request: expiry, host
        enabled + epoch, user active + session epoch, organization membership."""
        from app.core.permission_resolver import principal_belongs_to_org

        if not token or not token.startswith(SESSION_PREFIX):
            return None
        record = (await db.execute(select(OAuthAccessToken).where(
            OAuthAccessToken.token_hash == _hash(token), OAuthAccessToken.deleted_at.is_(None)))).scalar_one_or_none()
        if record is None or record.scope != SESSION_SCOPE or not record.host_application_id:
            return None
        if record.expires_at < _now():
            return None
        host = (await db.execute(select(HostApplication).where(
            HostApplication.id == str(record.host_application_id),
            HostApplication.deleted_at.is_(None)))).scalar_one_or_none()
        if host is None or not host.enabled:
            return None
        ctx = record.exchange_context or {}
        if int(ctx.get("host_epoch", -1)) != int(host.epoch or 1):
            return None
        user = (await db.execute(select(User).where(User.id == str(record.user_id),
                                                    User.is_active.is_(True)))).scalar_one_or_none()
        if user is None or int(ctx.get("user_epoch", -1)) != int(getattr(user, "session_epoch", 0) or 0):
            return None
        organization = (await db.execute(select(Organization).where(
            Organization.id == str(host.organization_id), Organization.deleted_at.is_(None)))).scalar_one_or_none()
        if organization is None or not await principal_belongs_to_org(db, user, organization.id):
            return None
        return EmbedContext(host=host, user=user, organization=organization, token_record=record)

    async def revoke_session(self, db: AsyncSession, record: OAuthAccessToken) -> None:
        record.deleted_at = _now()
        await db.commit()


host_application_service = HostApplicationService()
