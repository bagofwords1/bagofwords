"""Organization-scoped admission for a signature-validated Entra principal.

Never links an existing account by email, reactivates a disabled user, or applies
another organization's invitation/domain policy. The caller validates the
external application, tenant, audience, scope and signature before entering.
"""

import hashlib
import secrets
from datetime import datetime

from fastapi_users.password import PasswordHelper
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.oauth_server import OAuthClient
from app.models.organization import Organization
from app.models.user import User
from app.services.entra_token_exchange import ExchangeError


async def resolve_or_admit(db, service, client, config, claims):
    """Return a current member, or atomically admit a new invited/domain user."""
    tenant, audience = config["tenant_id"], config["audience_client_id"]
    identity = {"tenant_id": tenant, "object_id": claims["oid"], "client_id": audience}
    subject_key = f"{tenant}:{audience}:{claims['oid']}"
    # Serialize this principal and this org's first-time admissions across pods.
    # Transaction locks release on commit/rollback and never span Microsoft IO.
    if db.bind.dialect.name == "postgresql":
        for value in ("subject:" + subject_key, "organization:" + client.organization_id):
            key = int.from_bytes(
                hashlib.sha256(("bow:entra-admission:" + value).encode()).digest()[:8], "big", signed=True
            )
            if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
                raise ExchangeError("temporarily_unavailable", "User provisioning is in progress; retry", 503)
    accounts = (
        await db.scalars(
            select(OAuthAccount).where(
                OAuthAccount.oauth_name == config["provider"],
                OAuthAccount.entra_identity["tenant_id"].as_string() == tenant,
                OAuthAccount.entra_identity["object_id"].as_string() == claims["oid"],
                OAuthAccount.entra_identity["client_id"].as_string() == audience,
            )
        )
    ).all()
    ids = {a.user_id for a in accounts}
    if ids:
        if len(ids) != 1:
            raise ExchangeError("invalid_grant", "Ambiguous Entra identity")
        user_id = ids.pop()
        if not await service._active_subject(db, user_id, client.organization_id):
            raise ExchangeError("invalid_grant", "User access is unavailable; contact your administrator")
        await db.commit()
        return user_id

    # A configured Entra tenant supplies the login name. Use it only to admit a
    # NEW principal according to this org's policy, never to claim an old user.
    try:
        email = str(
            TypeAdapter(EmailStr).validate_python(
                claims.get("preferred_username") or claims.get("unique_name") or claims.get("email")
            )
        ).lower()
    except (ValidationError, TypeError):
        raise ExchangeError("invalid_grant", "An Entra email login is required for first-time admission") from None
    if await db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise ExchangeError("invalid_grant", "Existing account requires a verified Entra identity link")
    current = await db.scalar(
        select(OAuthClient)
        .where(OAuthClient.id == client.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    org = await db.scalar(
        select(Organization)
        .where(Organization.id == client.organization_id, Organization.deleted_at.is_(None))
        .with_for_update(of=Organization)
    )
    if not current or current.deleted_at or current.entra_exchange != config or not org:
        raise ExchangeError("invalid_grant", "Application or organization access changed")
    invite = await db.scalar(
        select(Membership)
        .where(
            Membership.organization_id == client.organization_id,
            Membership.user_id.is_(None),
            Membership.deleted_at.is_(None),
            func.lower(Membership.email) == email,
            or_(Membership.invite_expires_at.is_(None), Membership.invite_expires_at > datetime.utcnow()),
        )
        .with_for_update()
    )
    from app.core.auth import UserManager, _org_signup_policy

    policy = await _org_signup_policy(db, client.organization_id)
    allowed = {str(domain).strip().lower() for domain in policy.get("allowed_domains", [])}
    if not invite and email.rsplit("@", 1)[1] not in allowed:
        raise ExchangeError("invalid_grant", "An invitation or enabled domain signup policy is required")
    if not invite:
        from app.core.seats import has_seat_for

        if not await has_seat_for(db, client.organization_id):
            raise ExchangeError("invalid_grant", "Organization seat limit reached")
    role = invite.role if invite else str(policy.get("auto_invite_role") or "member")
    helper = PasswordHelper()
    user = User(
        email=email,
        name=str(claims.get("name") or email.split("@")[0]),
        hashed_password=helper.hash(secrets.token_urlsafe(48)),
        is_active=True,
        is_verified=True,
        is_superuser=False,
    )
    try:
        db.add(user)
        await db.flush()
        db.add(
            OAuthAccount(
                user_id=user.id,
                oauth_name=config["provider"],
                account_id="entra:" + subject_key,
                account_email=email,
                access_token="",
                entra_identity=identity,
                entra_subject=subject_key,
            )
        )
        if invite:
            invite.user_id = user.id
            invite.email = None
            invite.invite_token = None
            invite.invite_expires_at = None
            # Preserve roles/groups deliberately assigned by the inviting admin.
            await UserManager._materialize_pending_rbac(db, invite, user.id)
        else:
            db.add(Membership(user_id=user.id, organization_id=client.organization_id, role=role))
        from app.core.permission_resolver import ensure_system_role_assignment

        await ensure_system_role_assignment(db, client.organization_id, user.id, role)
        await db.commit()
    except (IntegrityError, OperationalError):
        await db.rollback()
        raise ExchangeError("temporarily_unavailable", "Concurrent user provisioning; retry", 503) from None
    return user.id
