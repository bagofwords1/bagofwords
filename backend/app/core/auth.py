import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, List, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update

import httpx

from fastapi import Depends, Request, HTTPException
from fastapi.security import APIKeyHeader
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users import exceptions
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy import select, and_, func, case
from httpx_oauth.oauth2 import BaseOAuth2

# fastapi-mail is no longer used here: account email goes through
# notification_service so it honours the organization's own SMTP server.

from app.schemas.user_schema import UserCreate
from app.models.organization import Organization
from app.models.membership import Membership

from app.models.user import User
from app.dependencies import get_user_db, get_async_db, resolve_organization
from app.models.oauth_account import OAuthAccount
from fastapi.responses import RedirectResponse

from app.settings.config import settings
from app.errors import AppError, ErrorCode
from app.services.organization_service import OrganizationService
from app.schemas.organization_schema import OrganizationCreate
from app.core.telemetry import telemetry

SECRET = settings.bow_config.encryption_key

logger = logging.getLogger(__name__)


DEFAULT_ORG_NAME = "Main Org"
DEFAULT_ORG_DESCRIPTION = ""

class UserManager(BaseUserManager[User, str]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    def parse_id(self, value: Any) -> str:
        return str(value)

    async def authenticate(self, credentials) -> Optional[User]:
        """
        Authenticate via LDAP bind (if enabled), otherwise by local password.

        When LDAP is enabled:
        - Try LDAP bind first
        - If LDAP bind succeeds, return the user
        - If the bind does not succeed — wrong password or directory
          unreachable alike — the login fails. The only local fallback is the
          explicit recovery path: ``ldap.allow_local_superuser_login`` for a
          superuser that is not linked to a directory identity.

        In ``auth.mode = sso_only``, the local form is a break-glass: SSO is
        the source of truth, so only the instance's administrators may sign in
        with a password (see ``_user_can_use_local_login``).
        """
        user = await self._do_authenticate(credentials)
        if user is None:
            from app.ee.audit.security_events import record_security_event
            known = None
            try:
                known = await self.get_by_email(credentials.username)
            except Exception:
                known = None
            # A failed attempt against a known account is evidence for that
            # account's organizations; an unknown account has none.
            await record_security_event(
                "auth.login_failed", user=known, outcome="failure", failure_reason="invalid_credentials",
                method="ldap" if settings.bow_config.ldap.enabled else "password",
                details={"email": (credentials.username or "")[:255], "account_known": known is not None},
            )
            return None
        # Credentials are correct at this point, so naming the real reason
        # leaks nothing an attacker could not already confirm — and the
        # alternative ("wrong email or password") sends a removed member off
        # to reset a password that was never the problem. fastapi-users' login
        # route would otherwise fold this into LOGIN_BAD_CREDENTIALS, so the
        # distinction has to be drawn here, before it returns.
        if not user.is_active:
            from app.ee.audit.security_events import record_security_event
            await record_security_event(
                "auth.login_failed", user=user, outcome="failure", failure_reason="account_disabled",
                method="password", details={"email": user.email},
            )
            raise AppError(
                ErrorCode.ACCOUNT_DISABLED,
                "Your account has been disabled. Contact your administrator.",
                status_code=403,
            )
        if (settings.bow_config.auth.mode == "sso_only" and
                not getattr(user, "_directory_authenticated", False) and
                not await self._user_can_use_local_login(user)):
            from app.ee.audit.security_events import record_security_event
            await record_security_event(
                "auth.login_failed", user=user, outcome="failure", failure_reason="local_login_not_allowed",
                method="password", details={"email": user.email},
            )
            return None
        return user

    async def _do_authenticate(self, credentials) -> Optional[User]:
        ldap_config = settings.bow_config.ldap
        if ldap_config.enabled:
            ldap_result = await self._ldap_authenticate(credentials.username, credentials.password)

            if isinstance(ldap_result, User):
                return ldap_result
            else:
                # Recovery is explicit and restricted to unlinked local superusers.
                try:
                    local_user = await self.get_by_email(credentials.username)
                    if (ldap_config.allow_local_superuser_login and local_user.is_superuser
                            and not local_user.ldap_subject):
                        return await super().authenticate(credentials)
                except exceptions.UserNotExists:
                    pass
                return None

        # No LDAP — standard local auth
        return await super().authenticate(credentials)

    async def _user_can_use_local_login(self, user: User) -> bool:
        """Allow local login in sso_only mode only for admins / superusers.

        "Admin" is decided by RBAC — the source of truth — not the legacy
        ``Membership.role`` string: a user gets break-glass password login iff
        they resolve to ``full_admin_access`` in any organization they belong to.
        (The resolver's transitional net still maps a legacy ``role='admin'``
        membership with no assignment to admin, so pre-backfill admins are not
        locked out; a user demoted in RBAC is correctly denied.)

        That holds for a single-organization deployment, where the org admin is
        the instance owner. When several organizations share the instance,
        administering one of them says nothing about the instance, so only
        superusers keep the password break-glass.
        """
        if user.is_superuser:
            return True
        if settings.bow_config.features.allow_multiple_organizations:
            return False
        from app.dependencies import async_session_maker
        from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
        async with async_session_maker() as session:
            org_rows = (await session.execute(
                select(Membership.organization_id).where(
                    Membership.user_id == str(user.id),
                    Membership.organization_id.isnot(None),
                    Membership.deleted_at.is_(None),
                )
            )).all()
            for (org_id,) in org_rows:
                resolved = await resolve_permissions(session, str(user.id), str(org_id))
                if FULL_ADMIN in resolved.org_permissions:
                    return True
            return False

    async def _ldap_authenticate(self, email: str, password: str) -> User | str:
        """
        Try LDAP bind auth.

        Returns:
            "success" — LDAP bind succeeded and user is ready
            "failed"  — LDAP reachable but credentials rejected
            "unreachable" — could not connect to LDAP server
        """
        from app.ee.ldap.connection import LDAPConnectionManager
        import logging
        _logger = logging.getLogger(__name__)

        ldap_config = settings.bow_config.ldap
        manager = LDAPConnectionManager(ldap_config)

        import asyncio
        try:
            identity = await asyncio.to_thread(manager.authenticate_identity, email, password)
        except Exception as e:
            _logger.warning("Directory authentication rejected: %s", type(e).__name__)
            return "failed"
        if not identity or not ldap_config.organization_id:
            return "failed"
        user = await admit_directory_identity(self.user_db.session, identity, ldap_config)
        if user is None:
            return "failed"
        user._directory_authenticated = True
        return user

    async def on_after_login(
        self,
        user: User,
        request: Optional[Request] = None,
        json_body: Optional[dict] = None
    ) -> None:
        try:
            from app.dependencies import async_session_maker
            async with async_session_maker() as session:
                await session.execute(
                    update(User).where(User.id == str(user.id)).values(last_login=datetime.now(timezone.utc).replace(tzinfo=None))
                )
                await session.commit()
        except Exception:
            pass

        # Instance administrators follow BOW_SUPERUSER_EMAILS at each sign-in.
        from app.core.instance_admin import apply_superuser_policy
        await apply_superuser_policy(user)

        # The OAuth/OIDC/SAML handlers record their own auth.login with the
        # provider; the local form (password or LDAP bind) is recorded here.
        if not (request and request.url.path.endswith("/callback")):
            from app.ee.audit import context as audit_context
            from app.ee.audit.security_events import record_security_event, session_id_of
            ctx = audit_context.current()
            if ctx is not None:
                audit_context.bind(ctx.with_(session_id=session_id_of(user) or ctx.session_id))
            await record_security_event(
                "auth.login", request=request, user=user, outcome="success",
                method="ldap" if getattr(user, "_directory_authenticated", False) else "password",
                details={"email": user.email},
            )

        # Handle redirect for any OAuth/OIDC callback
        if request and request.url.path.endswith("/callback"):
            import json
            try:
                response_data = json.loads(json_body.body.decode()) if json_body else {}
            except Exception:
                response_data = {}
            token = response_data.get('access_token')
            if token:
                # Hand back a single-use code, never the token itself: a URL
                # ends up in browser history, Referer headers and proxy logs.
                from app.services.login_exchange_service import issue_login_code

                login_code = await issue_login_code(str(user.id), token)
                redirect_url = (
                    f"{settings.bow_config.base_url}/users/sign-in?login_code={login_code}"
                )
                raise HTTPException(status_code=303, headers={"Location": redirect_url})

    async def _attach_open_memberships(
        self,
        user: User,
        session: AsyncSession,
        organization_ids: Optional[set] = None,
        mark_verified: bool = True,
    ):
        """Claim the open invites addressed to ``user.email``.

        ``organization_ids`` limits the claim to those organizations (an empty
        set claims nothing); ``None`` claims in every organization and is only
        for callers that have proof the person owns the address. An invite
        token proves it for the token's own organization and no other.
        ``mark_verified=False`` leaves ``is_verified`` alone, for callers that
        admit by a bare email match.
        """
        if organization_ids is not None and not organization_ids:
            return
        # Order by role privilege (then oldest, then id) so that when several open
        # invites exist for one org, the most-privileged is the one CLAIMED (the
        # surviving row's legacy `role`); the rest are retired. Deterministic, and
        # identical to the dedup migration / claim_pending_invite ordering.
        role_rank = case((Membership.role == "admin", 0), (Membership.role == "member", 1), else_=2)
        from app.core.permission_resolver import live_pending_invite
        conditions = [
            func.lower(Membership.email) == (user.email or "").strip().lower(),
            live_pending_invite(),
        ]
        if organization_ids is not None:
            conditions.append(Membership.organization_id.in_([str(o) for o in organization_ids]))
        stmt = select(Membership).where(
            and_(*conditions)
        ).order_by(role_rank, Membership.created_at.asc(), Membership.id.asc())
        open_memberships = (await session.execute(stmt)).scalars().all()

        if open_memberships:
            if mark_verified:
                user.is_verified = True
            # An invite is also the way back in for someone who was deactivated
            # when their last membership was removed (app/core/user_lifecycle).
            from app.core.user_lifecycle import reactivate_user_for_membership
            await reactivate_user_for_membership(session, str(user.id))

        # Update each open membership with the new user
        from app.models.role import Role
        from app.models.role_assignment import RoleAssignment

        from sqlalchemy.exc import IntegrityError
        from app.core.permission_resolver import ensure_system_role_assignment

        for membership in open_memberships:
            org_id = membership.organization_id
            invite_role = membership.role
            # Try to CLAIM this invite as the user's membership. If the user
            # already has a live row in the org (SCIM/OIDC/LDAP provisioned it, or
            # a concurrent create wins the race below), fall back to RETIRING the
            # invite instead: move its pending RBAC + base role onto the existing
            # row and hard-delete the invite. Two live rows for one (user, org) is
            # the duplicate that broke principal_belongs_to_org ("no models").
            existing = (await session.execute(
                select(Membership.id).where(
                    Membership.user_id == user.id,
                    Membership.organization_id == org_id,
                    Membership.deleted_at.is_(None),
                ).limit(1)
            )).first()

            claimed = False
            if existing is None:
                # SAVEPOINT: a concurrent create between the check above and here
                # would hit the unique index; don't 500 the registration/login.
                # Flush the caller's unrelated pending state (is_verified,
                # reactivation, the OAuth link) FIRST, in the outer transaction —
                # ``begin_nested()`` pre-flushes on open, so a claim stamped before
                # the savepoint would write in the OUTER transaction and a lost
                # race would roll back the whole registration. Stamp the claim
                # strictly INSIDE the savepoint instead.
                await session.flush()
                try:
                    async with session.begin_nested():
                        membership.user_id = user.id
                        membership.email = None
                        membership.invite_token = None
                        await session.flush([membership])
                    claimed = True
                except IntegrityError:
                    # Lost the race — a live row now exists. The savepoint rolled
                    # back only the claim; the outer transaction is still alive, so
                    # refresh the invite and retire it onto the existing row.
                    await session.refresh(membership)

            # Move any pre-assigned (pending) RBAC and the invite's base role onto
            # the user — needed whether we claimed the invite or are retiring it
            # (the pre-existing row may hold a lower role than the invite granted;
            # ensure_system_role_assignment is a no-op when already held).
            await self._materialize_pending_rbac(session, membership, user.id)
            if invite_role:
                # Built-ins only: custom roles came across with the pending RBAC
                # above, as the admin last edited them.
                await ensure_system_role_assignment(
                    session, org_id, str(user.id), invite_role, allow_custom=False)

            from app.core.permissions_registry import APP_USER_ROLE
            if not claimed and invite_role and invite_role != APP_USER_ROLE:
                # The existing row may be an app user's (created by an embedded
                # artifact's sign-in). An invite is an admin's explicit grant —
                # of a system role or the org's own custom role — so it replaces
                # the app_user role instead of sitting beside it with the
                # membership still labelled an app user.
                from app.core.permission_resolver import lift_app_user
                await lift_app_user(session, org_id, str(user.id), invite_role, allow_custom=False)

            if not claimed:
                # Hard-delete the retired invite — membership removal is a hard
                # delete everywhere else, and a soft-deleted row would reappear in
                # the many (user, org) reads that don't filter deleted_at.
                await session.delete(membership)
                await session.flush()
                continue

            # Telemetry: invited user accepted invite and signed up
            try:
                await telemetry.capture(
                    "organization_member_joined_via_invite",
                    {
                        "organization_id": str(org_id),
                        "membership_id": str(membership.id),
                        "user_id": str(user.id),
                    },
                    user_id=str(user.id),
                    org_id=str(org_id),
                )
            except Exception:
                pass

    @staticmethod
    async def _materialize_pending_rbac(session: AsyncSession, membership: Membership, user_id: str) -> None:
        """Rewrite pending (membership-keyed) RBAC rows onto the registered user.

        Thin wrapper over ``permission_resolver.materialize_pending_rbac`` so the
        invite-acceptance path and the provisioning paths (SCIM/OIDC via
        ``claim_pending_invite``) share one implementation.
        """
        from app.core.permission_resolver import materialize_pending_rbac
        await materialize_pending_rbac(session, membership, user_id)

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        print(f"User {user.id} has registered.")

        # Get open memberships and attach user
        async with self.user_db.session as session:
            # Materialize any domain-based auto-invites before attaching
            domain_org_ids = await self._create_domain_invites(user.email, session)
            token_org_id = getattr(self, "_invite_organization_id", None)
            if token_org_id:
                # The invite link proves the address for the organization that
                # issued it. Invites from other organizations stay pending until
                # they are accepted with their own link or a trusted sign-in.
                await self._attach_open_memberships(
                    user, session, organization_ids={token_org_id, *domain_org_ids}
                )
            elif settings.bow_config.features.allow_multiple_organizations:
                await self._attach_open_memberships(user, session, organization_ids=domain_org_ids)
            else:
                # No link was presented (open signups / domain policy): the
                # email was only typed, so it is not marked verified by an
                # invite alone.
                await self._attach_open_memberships(
                    user, session, mark_verified=bool(domain_org_ids)
                )

            if not settings.bow_config.features.verify_emails:
                user.is_verified = True

            await session.commit()
            # Auto-create organization for the first uninvited user
            await self._ensure_org_for_first_uninvited_user(session, user)

            # A new account named in BOW_SUPERUSER_EMAILS is an instance
            # administrator from the start (and no other new account is).
            from app.core.instance_admin import apply_superuser_policy
            await apply_superuser_policy(user)

            # Audit the signup against each org the user now belongs to.
            try:
                from app.ee.audit.service import audit_service
                memberships = (
                    await session.execute(
                        select(Membership).where(Membership.user_id == user.id)
                    )
                ).scalars().all()
                for m in memberships:
                    await audit_service.log(
                        db=session,
                        organization_id=str(m.organization_id),
                        action="user.registered",
                        user_id=str(user.id),
                        resource_type="user",
                        resource_id=str(user.id),
                        details={"email": user.email},
                        request=request,
                    )
            except Exception:
                pass

        # Welcome email (best-effort, non-blocking): summarizes the agents the
        # user can access + a CTA. Fired after commit so memberships are visible.
        # An invite link names the organization the user just joined; without
        # one the email service picks deterministically among the memberships.
        self._fire_welcome_email(user.id, getattr(self, "_invite_organization_id", None))

    @staticmethod
    def _fire_welcome_email(user_id, organization_id=None) -> None:
        try:
            import asyncio
            from app.services.welcome_email import send_welcome_email
            asyncio.create_task(send_welcome_email(
                str(user_id), str(organization_id) if organization_id else None))
        except Exception:
            pass

    async def _has_domain_invite(self, email: str, session: AsyncSession) -> bool:
        """Return True if some org's signup_policy would admit this email's domain."""
        from app.models.organization_settings import OrganizationSettings
        from app.ee.license import has_feature
        if not has_feature("domain_signup"):
            return False
        if not email or "@" not in email:
            return False
        domain = email.split("@", 1)[-1].lower()
        result = await session.execute(select(OrganizationSettings))
        for s in result.scalars().all():
            policy = (s.config or {}).get("signup_policy") or {}
            if not policy.get("enabled"):
                continue
            domains = [str(d).lower() for d in (policy.get("allowed_domains") or []) if isinstance(d, str)]
            if domain in domains:
                return True
        return False

    async def _create_domain_invites(self, email: str, session: AsyncSession) -> set:
        """For every org whose signup_policy admits this email's domain,
        create an open Membership invite if one doesn't already exist.

        Returns the ids of the organizations an invite was created for."""
        from app.models.organization_settings import OrganizationSettings
        from app.ee.license import has_feature
        from sqlalchemy import or_
        created_for: set = set()
        if not has_feature("domain_signup"):
            return created_for
        if not email or "@" not in email:
            return created_for
        domain = email.split("@", 1)[-1].lower()
        result = await session.execute(select(OrganizationSettings))
        admitting: list = []
        for s in result.scalars().all():
            policy = (s.config or {}).get("signup_policy") or {}
            if not policy.get("enabled"):
                continue
            domains = [str(d).lower() for d in (policy.get("allowed_domains") or []) if isinstance(d, str)]
            if domain not in domains:
                continue
            admitting.append(str(s.organization_id))
            if len(admitting) == 2:
                # New claims are limited to one organization per domain; this
                # is an overlap saved before that rule and left as it is.
                logging.getLogger(__name__).warning(
                    "Domain signup: domain %s is claimed by more than one organization "
                    "(%s, ...); an instance administrator should keep it on one",
                    domain, ", ".join(admitting),
                )
            # Skip if this email is already linked to this org (invite or attached user)
            email_norm = (email or "").strip().lower()
            existing_user = (await session.execute(
                select(User).where(func.lower(User.email) == email_norm)
            )).scalar_one_or_none()
            conditions = [func.lower(Membership.email) == email_norm]
            if existing_user is not None:
                conditions.append(Membership.user_id == existing_user.id)
            dupe = (await session.execute(
                select(Membership).where(
                    Membership.organization_id == s.organization_id,
                    or_(*conditions),
                )
            )).scalar_one_or_none()
            if dupe:
                continue
            # Respect the license seat cap: auto-provisioning must not push an org
            # past its paid seat count. Full org → skip this invite (and log), don't
            # abort the whole signup — other admitting orgs may still have room.
            from app.core.seats import has_seat_for
            if not await has_seat_for(session, s.organization_id):
                logging.getLogger(__name__).warning(
                    "Domain signup: org %s is at its license seat limit; "
                    "skipping auto-invite for %s", s.organization_id, email
                )
                continue
            from app.services.organization_settings_service import resolve_signup_role
            role = await resolve_signup_role(session, str(s.organization_id), policy)
            invite = Membership(
                email=email_norm,
                organization_id=s.organization_id,
                role=role,
            )
            session.add(invite)
            # A custom role travels as a pending RBAC row (acceptance grants
            # only built-ins from the name).
            from app.core.permission_resolver import stage_invite_role
            await stage_invite_role(session, str(s.organization_id), invite, role)
            created_for.add(str(s.organization_id))
        await session.flush()
        return created_for

    async def oauth_callback(
        self: "UserManager[User, str]",
        oauth_name: str,
        access_token: str,
        account_id: str,
        account_email: str,
        expires_at: Optional[int] = None,
        refresh_token: Optional[str] = None,
        request: Optional[Request] = None,
        *args,
        identity_claims: Optional[dict] = None,
        **kwargs
    ) -> User:
        # Whether this provider's word is enough to tie the email it asserts
        # to an existing account or to that email's pending invites.
        email_trusted = email_linking_trusted(oauth_name, identity_claims)
        try:
            # First try to get user by OAuth account
            user = await self.get_by_oauth_account(oauth_name, account_id)
            # Keep the stored tokens current. They used to be written only on
            # the account's first login, so anything reading them later (Entra
            # profile sync/preview) worked against a long-expired token.
            async with self.user_db.session as session:
                stmt = select(OAuthAccount).where(
                    and_(
                        OAuthAccount.oauth_name == oauth_name,
                        OAuthAccount.account_id == account_id,
                    )
                )
                acc = (await session.execute(stmt)).scalar_one_or_none()
                claim_invites = email_trusted and _same_email(account_email, user.email)
                if acc:
                    acc.access_token = access_token
                    acc.expires_at = expires_at
                    if refresh_token:
                        acc.refresh_token = refresh_token
                    session.add(acc)
                    await session.commit()
                if claim_invites:
                    # Invites issued after the account was created (or left
                    # pending by a registration through another org's link).
                    await self._attach_open_memberships(user, session)
                    await session.commit()
            return user
        except exceptions.UserNotExists:
            # If OAuth account doesn't exist, check if user exists by email
            try:
                user = await self.get_by_email(account_email)
                if not email_trusted:
                    # An account is never claimed on an email match the
                    # provider is not trusted for, and no second account is
                    # created for the same address either.
                    from app.ee.audit.security_events import record_security_event
                    await record_security_event(
                        "auth.login_failed", request=request, user=user, outcome="failure",
                        failure_reason="account_link_refused", method=f"oauth:{oauth_name}",
                        details={"provider": oauth_name, "email": (account_email or "")[:255]},
                    )
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "account_link_required",
                            "message": "An account with this email already exists. "
                                       "Ask your administrator to link it.",
                        },
                    )
                # User exists, let's link the OAuth account
                async with self.user_db.session as session:
                    oauth_account = OAuthAccount(
                        oauth_name=oauth_name,
                        access_token=access_token,
                        account_id=account_id,
                        account_email=account_email,
                        expires_at=expires_at,
                        refresh_token=refresh_token,
                        user_id=user.id
                    )
                    session.add(oauth_account)
                    await self._attach_open_memberships(user, session)
                    await session.commit()
                return user
            except exceptions.UserNotExists:
                # User doesn't exist at all, create new user with OAuth
                # Enforce invite policy similar to regular registration
                from app.core.instance_admin import is_reserved_instance_admin_email
                async with self.user_db.session as session:
                    if not email_trusted:
                        # The asserted email proves nothing here, so it cannot
                        # stand in for the person an invite, a domain policy or
                        # the instance-administrator list names: the account
                        # would then be waiting for whoever holds the address.
                        users_exist = await any_user_exists(session)
                        reserved = is_reserved_instance_admin_email(account_email)
                        addressed = users_exist and (
                            (await session.execute(
                                select(Membership.id).where(
                                    func.lower(Membership.email) == (account_email or "").strip().lower(),
                                    Membership.user_id.is_(None),
                                ).limit(1)
                            )).first() is not None
                            or await self._has_domain_invite(account_email, session)
                        )
                        if reserved or addressed:
                            from app.ee.audit.security_events import record_security_event
                            await record_security_event(
                                "auth.login_failed", request=request, outcome="failure",
                                failure_reason="untrusted_provider_email", method=f"oauth:{oauth_name}",
                                details={"provider": oauth_name, "email": (account_email or "")[:255]},
                            )
                            # A reserved address gets the same answer as any
                            # uninvited one.
                            raise HTTPException(
                                status_code=403,
                                detail={
                                    "code": "invite_link_required" if addressed else "invitation_required",
                                    "message": "This sign-in method cannot accept your invitation. "
                                               "Use your invite link or ask your administrator."
                                    if addressed else "Sign-up is disabled. Ask your admin for an invite.",
                                },
                            )
                    # If uninvited signups are disabled and not first user, require invite
                    if await any_user_exists(session) and not settings.bow_config.features.allow_uninvited_signups:
                        # Existence check only, and limited: the same address can
                        # hold open invites in several orgs, so scalar_one_or_none()
                        # would raise MultipleResultsFound and 500 the OAuth signup.
                        # An expired invite admits nobody until it is resent.
                        from app.core.permission_resolver import live_pending_invite
                        stmt = select(Membership.id).where(
                            and_(
                                func.lower(Membership.email) == (account_email or "").strip().lower(),
                                live_pending_invite(),
                            )
                        ).limit(1)
                        open_membership = (await session.execute(stmt)).first()
                        if not open_membership and await self._has_domain_invite(account_email, session):
                            open_membership = True
                        if not open_membership and kwargs.get("org_mapping_admits"):
                            # The operator's OIDC org_mapping places this person
                            # in an organization (see services/oidc_org_mapping).
                            open_membership = True
                        if not open_membership:
                            raise HTTPException(
                                status_code=403,
                                detail={
                                    "code": "invitation_required",
                                    "message": "Sign-up is disabled. Ask your admin for an invite.",
                                },
                            )
                # Fetch user info if needed (e.g., from Google)
                fetched_name = None
                if oauth_name == "google":
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            "https://www.googleapis.com/oauth2/v1/userinfo",
                            headers={"Authorization": f"Bearer {access_token}"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        fetched_name = user_info.get("name")
                if not fetched_name:
                    fetched_name = account_email.split("@")[0]

                # Create the new user
                async with self.user_db.session as session:
                    user = await self.user_db.create(
                        {
                            "email": account_email,
                            "name": fetched_name,
                            "hashed_password": self.password_helper.hash(self.password_helper.generate()),
                            "is_active": True,
                            # An untrusted provider does not verify the address
                            # any more than typing it into the sign-up form does.
                            "is_verified": email_trusted or not settings.bow_config.features.verify_emails,
                            "is_superuser": False,
                        }
                    )

                    # Materialize domain-based auto-invites, then attach memberships.
                    # Only for a trusted email: an untrusted one got this far
                    # because nothing is addressed to it (checked above).
                    if email_trusted:
                        await self._create_domain_invites(account_email, session)
                        await self._attach_open_memberships(user, session)

                    oauth_account = OAuthAccount(
                        oauth_name=oauth_name,
                        access_token=access_token,
                        account_id=account_id,
                        account_email=account_email,
                        expires_at=expires_at,
                        refresh_token=refresh_token,
                        user_id=user.id
                    )
                    session.add(oauth_account)
                    await session.commit()
                    await session.refresh(user)
                    # Auto-create organization for the first uninvited user
                    await self._ensure_org_for_first_uninvited_user(session, user)
                # New OAuth/OIDC sign-up → welcome email (best-effort).
                self._fire_welcome_email(user.id)
                return user

    async def _ensure_org_for_first_uninvited_user(self, session: AsyncSession, user: User) -> None:
        """Create an organization automatically if this is the first user without an invite.

        Conditions:
        - User has no memberships (not invited/attached)
        - Total users == 1
        - Total organizations == 0
        """
        # If user already has a membership, skip
        user_membership = (
            await session.execute(
                select(Membership).where(Membership.user_id == user.id)
            )
        ).scalars().first()
        if user_membership:
            return

        total_users = (await session.execute(select(User))).scalars().all().__len__()
        total_orgs = (await session.execute(select(Organization))).scalars().all().__len__()

        if total_users != 1 or total_orgs != 0:
            return

        org_name = DEFAULT_ORG_NAME
        description = DEFAULT_ORG_DESCRIPTION

        organization_service = OrganizationService()
        await organization_service.create_organization(
            session,
            OrganizationCreate(name=org_name, description=description),
            user,
        )

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        await self._send_reset_password_email(user, token, request)
        from app.ee.audit.security_events import record_security_event
        await record_security_event("auth.password_reset_requested", request=request, user=user,
                                    outcome="success", method="password", details={"email": user.email})

    async def on_after_reset_password(self, user: User, request: Optional[Request] = None):
        # A password reset is the one moment where the old sessions are most
        # likely the attacker's, so they must not survive it.
        await revoke_all_user_sessions(user.id)
        from app.ee.audit.security_events import record_security_event
        await record_security_event("auth.password_reset", request=request, user=user, outcome="success",
                                    method="password", details={"email": user.email, "sessions_revoked": True})

    async def update(self, user_update, user: User, safe: bool = False, request: Optional[Request] = None) -> User:
        """Self-service profile update (``safe=True``, PATCH /users/me) rules.

        The login email is the key invites and first sign-in match on, so a user
        cannot change their own; an administrator can (PATCH /users/{id}).
        A new password is refused when the deployment is ``sso_only``; a
        ``current_password`` sent with it must be correct.
        """
        if safe:
            new_email = getattr(user_update, "email", None)
            if new_email is not None and new_email != user.email:
                raise HTTPException(
                    status_code=400,
                    detail="Your email address cannot be changed here. Ask your administrator.",
                )
            if getattr(user_update, "password", None) is not None:
                if settings.bow_config.auth.mode == "sso_only":
                    raise HTTPException(
                        status_code=400,
                        detail="Passwords are managed by your sign-in provider.",
                    )
                # Optional (existing API clients send only the new password);
                # when supplied it has to be right.
                current = getattr(user_update, "current_password", None)
                if current is not None:
                    verified, _ = self.password_helper.verify_and_update(current, user.hashed_password)
                    if not verified:
                        raise HTTPException(status_code=400, detail="Your current password is incorrect.")
        return await super().update(user_update, user, safe, request)

    async def accept_invite(self, user: User, token: str) -> Membership:
        """Join the organization whose invite link ``token`` belongs to.

        For someone who already has an account: the link was sent to the
        invited address, and it must be the address of the signed-in account.
        Claims that organization's invite only.
        """
        async with self.user_db.session as session:
            invite = (await session.execute(
                select(Membership).where(
                    Membership.invite_token == token,
                    Membership.user_id.is_(None),
                    Membership.deleted_at.is_(None),
                )
            )).scalar_one_or_none() if token else None
            if invite is None:
                raise HTTPException(status_code=400, detail="This invite link is invalid. Ask your admin to resend it.")
            # Fetched without the expiry filter only to tell "expired" from
            # "invalid"; the claim below applies live_pending_invite().
            expires = invite.invite_expires_at
            if expires is not None and expires <= datetime.utcnow():
                raise HTTPException(status_code=400, detail="This invite link has expired. Ask your admin to resend it.")
            if not _same_email(invite.email, user.email):
                raise HTTPException(status_code=403, detail="This invite was sent to a different email address.")
            organization_id = str(invite.organization_id)
            db_user = await session.get(User, user.id)
            await self._attach_open_memberships(db_user, session, organization_ids={organization_id})
            await session.commit()
            membership = (await session.execute(
                select(Membership).where(
                    Membership.user_id == user.id,
                    Membership.organization_id == organization_id,
                    Membership.deleted_at.is_(None),
                )
            )).scalars().first()
            try:
                from app.ee.audit.service import audit_service
                await audit_service.log(
                    db=session,
                    organization_id=organization_id,
                    action="member.invite_accepted",
                    user_id=str(user.id),
                    resource_type="membership",
                    resource_id=str(membership.id) if membership else None,
                    details={"email": user.email},
                )
            except Exception:
                pass
            return membership

    async def on_after_update(
        self,
        user: User,
        update_dict: dict,
        request: Optional[Request] = None,
    ):
        # Same reasoning for a self-service password change. Other profile
        # edits (name, avatar) leave sessions alone.
        if "password" in update_dict or "hashed_password" in update_dict:
            await revoke_all_user_sessions(user.id)
            from app.ee.audit.security_events import record_security_event
            await record_security_event("auth.password_changed", request=request, user=user, outcome="success",
                                        method="password", details={"email": user.email, "sessions_revoked": True})

    async def _send_reset_password_email(self, user: User, token: str, request: Optional[Request] = None):
        base_url = settings.bow_config.base_url
        reset_url = f"{base_url}/users/reset-password?token={token}"
        body = (
            f"Hello {user.name},<br /><br />You have requested to reset your password for BOW. "
            f"Click the link below to reset your password:<br /><br /> "
            f"<a href='{reset_url}'>{reset_url}</a><br /><br />"
            f"If you didn't request this, please ignore this email.<br /><br />"
            f"Best regards,<br />BOW team"
        )
        await self._send_account_email(user, "Reset your password", body, "reset password")

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        await self._send_verification_email(user, token, request)

    async def _send_verification_email(self, user: User, token: str, request: Optional[Request] = None):
        base_url = settings.bow_config.base_url
        verification_url = f"{base_url}/users/verify?token={token}"
        body = (
            "Welcome to BOW! You are almost ready to start using our platform. "
            f"Click to verify your email: <br /> {verification_url}"
        )
        await self._send_account_email(user, "Verify your email", body, "verification")

    async def _send_account_email(self, user: User, subject: str, body: str, kind: str) -> None:
        """Send an account email (reset / verification) via the resolved transport.

        Account mail has no organization in scope — fastapi-users knows the user,
        not the tenant — so the org is derived from the user's membership when
        that is unambiguous. Without this these two emails always left through
        the global bow-config SMTP, ignoring whatever the organization had
        configured, and simply vanished when no global SMTP existed.

        Fire-and-forget by design (the caller is an auth flow that must not block
        or leak whether an address exists), but the outcome is logged rather than
        printed, and a missing transport no longer raises inside the task.
        """
        import asyncio

        from app.services.email_client_resolver import sole_organization_id
        from app.services.notification_service import notification_service

        # Read the ORM instance now, while its session is still open. The task
        # below outlives the request, and touching a detached User there raises
        # DetachedInstanceError instead of sending the mail.
        user_id = str(user.id)
        recipient = user.email

        async def _send() -> None:
            try:
                from app.dependencies import async_session_maker

                async with async_session_maker() as db:
                    organization_id = await sole_organization_id(db, user_id)
                    result = await notification_service.send_custom_email(
                        recipients=[recipient],
                        subject=subject,
                        body=body,
                        subtype="html",
                        retries=2,
                        timeout=15,
                        db=db,
                        organization_id=organization_id,
                        purpose="system",
                    )
                if result.status == "sent":
                    logger.info("Sent %s email to %s via %s", kind, recipient, result.source)
                else:
                    logger.error(
                        "Failed to send %s email to %s: %s", kind, recipient, result.error
                    )
            except Exception as e:  # noqa: BLE001 — must never break the auth flow
                logger.error("Error sending %s email to %s: %s", kind, recipient, e)

        asyncio.create_task(_send())

    async def create(
        self,
        user_create: UserCreate,
        safe: bool = False,
        request: Optional[Request] = None,
    ) -> User:
        # Your pre-registration logic here
        # For example:
        await self._validate_user_creation(user_create)
        
        # Call parent create method
        user = await super().create(user_create, safe, request)

        # First user's product-updates checkbox (best-effort: never fails sign-up).
        opted_in = getattr(user_create, "product_updates", None)
        if opted_in is not None:
            try:
                from app.services.product_updates_service import record_signup_choice
                async with self.user_db.session as session:
                    await record_signup_choice(session, user, opted_in)
            except Exception:
                logger.exception("product_updates: failed to record sign-up choice")

        return user

    async def _validate_user_creation(self, user_create: UserCreate) -> None:
        """Pre-registration gate (runs before the User row is created).

        Invite-token rules for the local/password path (SSO has its own check in
        ``oauth_callback`` and is intentionally not affected):

          - token supplied  -> must match a pending invite for this email and not
            be expired; otherwise reject (no user is created).
          - no token, pending invite exists, signups closed -> reject and point
            the user at their invite link (closes the "claim by typing an
            invited email" gap). Under open signups, email-match onboarding is
            still allowed.
          - no token, no invite -> existing behaviour (first user / domain
            policy / open signups allowed; otherwise rejected).
        """
        from datetime import datetime

        token = getattr(user_create, "invite_token", None)
        email = user_create.email

        async with self.user_db.session as session:
            users_exist = await any_user_exists(session)

            # 1) A token was presented — validate it strictly.
            if token:
                membership = (await session.execute(
                    select(Membership).where(
                        Membership.invite_token == token,
                        Membership.user_id.is_(None),
                    )
                )).scalar_one_or_none()
                if not membership:
                    raise HTTPException(status_code=400, detail="This invite link is invalid. Ask your admin to resend it.")
                expires = membership.invite_expires_at
                if expires is not None and expires <= datetime.utcnow():
                    raise HTTPException(status_code=400, detail="This invite link has expired. Ask your admin to resend it.")
                if membership.email and membership.email.lower() != (email or "").lower():
                    raise HTTPException(status_code=400, detail="This invite was sent to a different email address.")
                # Remembered for on_after_register: the link admits the new
                # user to the organization that issued it, and only that one.
                self._invite_organization_id = str(membership.organization_id)
                return  # valid invite — allow creation

            # 2) No token. First user always allowed (bootstrap).
            if not users_exist:
                return

            # An address on the instance-administrator list carries that role,
            # so an account under it needs the invite link sent to the address
            # even where anyone else may sign up. Same answer as a closed sign-up.
            from app.core.instance_admin import is_reserved_instance_admin_email
            if is_reserved_instance_admin_email(email):
                raise HTTPException(
                    status_code=400,
                    detail="Sign-up is disabled. Ask your admin for an invite.",
                )

            # A pending invite exists for this email but no token was supplied.
            # Existence check only: the address can hold open invites in
            # several organizations, and invites are stored lowercase.
            pending = (await session.execute(
                select(Membership.id).where(
                    and_(
                        func.lower(Membership.email) == (email or "").strip().lower(),
                        Membership.user_id.is_(None),
                    )
                ).limit(1)
            )).first()

            if pending:
                if settings.bow_config.features.allow_uninvited_signups:
                    return  # open signups: email-match onboarding still allowed
                raise HTTPException(
                    status_code=400,
                    detail="Please sign up using your invite link, or ask your admin to resend it.",
                )

            # 3) No pending invite.
            if not settings.bow_config.features.allow_uninvited_signups:
                if await self._has_domain_invite(email, session):
                    return
                raise HTTPException(
                    status_code=400,
                    detail="Sign-up is disabled. Ask your admin for an invite.",
                )

        return


def _same_email(a: Optional[str], b: Optional[str]) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a) and a == b


def _claim_denies(value: Any) -> bool:
    """True when a verification claim is present and says "not verified"
    (some providers send the boolean as a string)."""
    if isinstance(value, str):
        return value.strip().lower() == "false"
    return value is False


_MULTI_TENANT_MICROSOFT = {"common", "organizations", "consumers"}
# Providers already warned about (once per process each).
_warned_email_trust: set = set()


def email_linking_trusted(oauth_name: str, identity_claims: Optional[dict] = None) -> bool:
    """Whether a sign-in through ``oauth_name`` proves ownership of the email
    it asserts — enough to link an existing account or claim pending invites.

    The provider's ``trust_email_for_linking`` setting decides when set.
    Otherwise a single-organization deployment trusts its providers, as it
    always has (the operator chose every one of them for the one organization).
    When several organizations share the instance it is trusted only when all
    of these hold: it is the single enabled sign-in provider (Google + OIDC;
    with several, an address proven at one says nothing about the account
    another created), the token does not mark the email unverified
    (``email_verified`` / Entra ``xms_edov``), and it is not a Microsoft
    endpoint that accepts accounts from any tenant.
    ``identity_claims`` is None when the provider gave no id_token claims.
    """
    from urllib.parse import urlsplit
    from app.settings.bow_config import brand_for_issuer

    google = getattr(settings.bow_config, "google_oauth", None)
    google_enabled = bool(google and google.enabled)
    oidc = [p for p in (getattr(settings.bow_config, "oidc_providers", None) or []) if p.enabled]
    if oauth_name == "google":
        cfg = google if google_enabled else None
    else:
        cfg = next((p for p in oidc if p.name == oauth_name), None)
    if cfg is None:
        return False

    explicit = getattr(cfg, "trust_email_for_linking", None)
    if explicit is not None:
        return bool(explicit)

    strict = len(oidc) + (1 if google_enabled else 0) == 1
    claims = identity_claims or {}
    if _claim_denies(claims.get("email_verified")) or _claim_denies(claims.get("xms_edov")):
        strict = False
    issuer = getattr(cfg, "issuer", None)
    if issuer and brand_for_issuer(issuer) == "microsoft":
        segments = [seg for seg in urlsplit(issuer.lower()).path.split("/") if seg]
        if segments and segments[0] in _MULTI_TENANT_MICROSOFT:
            strict = False
    if settings.bow_config.features.allow_multiple_organizations:
        return strict
    if not strict and oauth_name not in _warned_email_trust:
        _warned_email_trust.add(oauth_name)
        logger.warning(
            "Sign-in provider %r: linking by email on an untrusted provider "
            "(several providers enabled, an unverified email, or a multi-tenant issuer); "
            "set trust_email_for_linking: false to disable",
            oauth_name,
        )
    return True


async def admit_directory_identity(session: AsyncSession, identity: dict, ldap_config) -> Optional[User]:
    """The BOW user for a verified Active Directory identity, created when the
    deployment auto-provisions directory users. None when it must be refused.

    ``identity`` comes from LDAPConnectionManager.read_identity (an enabled,
    unexpired object inside the admission group). Shared by password login
    and embedded sign-in (services/host_identity.py), so both admit exactly
    the same people: the objectGUID binds the account, an existing account
    with the same email is never claimed, seats are enforced.
    """
    from pydantic import TypeAdapter, EmailStr, ValidationError
    try:
        email = str(TypeAdapter(EmailStr).validate_python(identity["email"]))
    except (ValidationError, KeyError):
        return None
    subject = identity["provider"] + ":" + identity["guid"]
    user = (await session.execute(select(User).where(User.ldap_subject == subject))).scalar_one_or_none()
    collision = (await session.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()
    if collision is not None and (user is None or collision.id != user.id):
        return None
    if user is None:
        if not ldap_config.auto_provision_users:
            return None
        org = await session.get(Organization, ldap_config.organization_id)
        if org is None:
            return None
        from app.core.seats import seats_remaining
        remaining = await seats_remaining(session, ldap_config.organization_id)
        if remaining is not None and remaining <= 0:
            return None
        from fastapi_users.password import PasswordHelper
        ph = PasswordHelper()
        user = User(email=email.lower(), name=identity["name"],
            hashed_password=ph.hash(ph.generate()), is_active=True,
            is_verified=True, is_superuser=False, ldap_subject=subject,
            ldap_identity=identity, ldap_dn=identity["dn"])
        session.add(user)
        await session.flush()
        from app.core.permission_resolver import ensure_system_role_assignment, insert_membership_if_absent
        from app.services.organization_settings_service import resolve_default_member_role
        role = await resolve_default_member_role(session, str(ldap_config.organization_id))
        await insert_membership_if_absent(session, Membership(
            user_id=user.id, organization_id=ldap_config.organization_id,
            role=role, directory_provider=identity["provider"]))
        await ensure_system_role_assignment(session, ldap_config.organization_id, str(user.id), role)
    else:
        from app.core.instance_admin import directory_sign_in_refused_for_superuser
        if not user.is_active or directory_sign_in_refused_for_superuser(user):
            return None
        membership = (await session.execute(select(Membership.id).where(
            Membership.user_id == user.id, Membership.organization_id == ldap_config.organization_id,
            Membership.deleted_at.is_(None)))).first()
        if not membership:
            return None
        user.email = email.lower()
        user.ldap_identity = identity
        user.ldap_dn = identity["dn"]
    await session.commit()
    return user


async def any_user_exists(session: AsyncSession) -> bool:
    """Whether this instance has been claimed yet.

    Registration bootstraps the first user without an invite, so "no users yet"
    is a real state that both the invite policy and the setup screen key off.

    Deliberately uncached: a module-level flag would survive the database resets
    between tests and tell a fresh-instance test that users already exist. A
    COUNT over an indexed primary key is cheap enough not to need one.
    """
    return bool(await session.scalar(select(func.count()).select_from(User)))


async def _org_signup_policy(db: AsyncSession, organization_id: str) -> dict:
    from app.models.organization_settings import OrganizationSettings
    from app.ee.license import has_feature
    if not has_feature("domain_signup"):
        return {}
    result = await db.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id == organization_id)
    )
    s = result.scalar_one_or_none()
    if not s:
        return {}
    policy = (s.config or {}).get("signup_policy") or {}
    if not policy.get("enabled"):
        return {}
    return policy


async def auto_provision_user_for_org(
    db: AsyncSession,
    organization_id: str,
    email: str,
    name: Optional[str] = None,
) -> Optional[User]:
    """Provision (or attach) a user for chat-onboarding into a specific org.

    Returns the User if admitted via (a) an existing open invite scoped to this
    org or (b) this org's domain_signup policy. Returns None if neither applies
    (caller should block the chat user with an "ask your admin" message).
    """
    from sqlalchemy import func
    from fastapi_users.password import PasswordHelper

    if not email or "@" not in email:
        return None
    email_norm = email.strip().lower()

    # Step 1 (find existing user) is the caller's responsibility. Here we
    # handle: existing user with no membership in this org, or no user at all.
    existing_user = (await db.execute(
        select(User).where(func.lower(User.email) == email_norm)
    )).scalar_one_or_none()

    # Multiple open invites for one email in an org are allowed, so this must not
    # use scalar_one_or_none() (it would raise MultipleResultsFound). It is only a
    # presence flag here; the actual claim re-queries deterministically.
    from app.core.permission_resolver import live_pending_invite
    open_invite = (await db.execute(
        select(Membership).where(
            Membership.organization_id == organization_id,
            func.lower(Membership.email) == email_norm,
            live_pending_invite(),
        ).limit(1)
    )).scalars().first()

    policy = await _org_signup_policy(db, organization_id)
    domain = email_norm.split("@", 1)[-1]
    allowed_domains = [str(d).lower() for d in (policy.get("allowed_domains") or []) if isinstance(d, str)]
    domain_admitted = domain in allowed_domains

    if not open_invite and not domain_admitted:
        return None

    # A brand-new membership is minted only when there's no open invite to attach
    # to (an existing invite already occupies a seat). Enforce the license seat cap
    # before creating anything, so a full org can't be grown via chat onboarding —
    # and so we never leave an orphan User with no membership.
    if not open_invite:
        from app.core.seats import has_seat_for
        if not await has_seat_for(db, organization_id):
            logging.getLogger(__name__).warning(
                "Chat auto-provision: org %s is at its license seat limit; "
                "refusing to provision %s", organization_id, email_norm
            )
            return None

    if existing_user:
        if open_invite:
            # Claim via the shared, savepoint-guarded primitive — a bare
            # ``open_invite.user_id = ...`` would 500 at commit if a concurrent
            # SCIM/OIDC provision of the same user won the race against the unique
            # index. On a lost race it returns None (the user is already a member).
            from app.core.permission_resolver import claim_pending_invite
            await claim_pending_invite(db, existing_user, organization_id)
        else:
            from app.services.organization_settings_service import resolve_signup_role
            from app.core.permission_resolver import ensure_system_role_assignment, insert_membership_if_absent
            role = await resolve_signup_role(db, organization_id, policy)
            _, created = await insert_membership_if_absent(db, Membership(
                user_id=existing_user.id,
                organization_id=organization_id,
                role=role,
            ))
            if created:
                # A real RBAC assignment: the legacy-role net only understands
                # member/admin, so a custom default role would otherwise grant nothing.
                await ensure_system_role_assignment(db, organization_id, str(existing_user.id), role)
        from app.core.user_lifecycle import reactivate_user_for_membership
        await reactivate_user_for_membership(db, str(existing_user.id), organization_id)
        await db.commit()
        return existing_user

    ph = PasswordHelper()
    user = User(
        email=email_norm,
        name=name or email_norm.split("@")[0],
        hashed_password=ph.hash(ph.generate()),
        is_active=True,
        is_verified=True,
        is_superuser=False,
    )
    db.add(user)
    await db.flush()

    if open_invite:
        # Claim via the shared, savepoint-guarded primitive (see the existing-user
        # branch above) rather than a bare assignment that would 500 on a race.
        from app.core.permission_resolver import claim_pending_invite
        await claim_pending_invite(db, user, organization_id)
    else:
        from app.services.organization_settings_service import resolve_signup_role
        from app.core.permission_resolver import ensure_system_role_assignment, insert_membership_if_absent
        role = await resolve_signup_role(db, organization_id, policy)
        await insert_membership_if_absent(db, Membership(
            user_id=user.id,
            organization_id=organization_id,
            role=role,
        ))
        await ensure_system_role_assignment(db, organization_id, str(user.id), role)

    await db.commit()
    await db.refresh(user)

    try:
        await telemetry.capture(
            "user_auto_provisioned_from_chat",
            {
                "organization_id": str(organization_id),
                "user_id": str(user.id),
                "via": "domain_signup" if not open_invite else "open_invite",
            },
            user_id=str(user.id),
            org_id=str(organization_id),
        )
    except Exception:
        pass

    return user


async def get_user_manager(user_db: SQLAlchemyUserDatabase = Depends(get_user_db)):
    yield UserManager(user_db)


bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")

# Claim carrying the user's `session_epoch` at the moment the token was minted.
# Tokens issued before this claim existed decode to 0, which never matches the
# seeded epoch of 1 — so upgrading signs everyone out once, on purpose.
SESSION_EPOCH_CLAIM = "session_epoch"


def _session_epoch_of(user: User) -> int:
    return int(getattr(user, "session_epoch", 0) or 0)


async def bump_session_epoch(user_id: str) -> None:
    """Invalidate every session token already issued to a user.

    The increment runs in SQL so concurrent bumps can't clobber each other. Used
    by logout, password change/reset, and the admin force-signout endpoint.
    """
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        await db.execute(
            update(User)
            .where(User.id == str(user_id))
            .values(session_epoch=User.session_epoch + 1)
        )
        await db.commit()


async def revoke_all_user_sessions(user_id: str) -> None:
    """Bump the session epoch and revoke issued OAuth codes/bearer tokens.

    Used on password change/reset: OAuth access tokens are validated by hash,
    not by session epoch, so bumping the epoch alone leaves them working.
    """
    from app.core.session_revocation import revoke_user_sessions
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        await revoke_user_sessions(db, user_id)


class SessionEpochJWTStrategy(JWTStrategy):
    """JWTStrategy that makes its tokens revocable.

    Plain `JWTStrategy` is stateless: once minted, a token is valid until it
    expires, and `destroy_token` raises `StrategyDestroyNotSupportedError` — so
    fastapi-users' logout route silently does nothing and a stolen bearer token
    survives any number of logout/login cycles for its full lifetime.

    Stamping the user's `session_epoch` into the token and re-checking it on
    every request turns that into a real revocation: bumping the column rejects
    every token minted before the bump. The user row is already loaded to
    authenticate the request, so this costs a comparison, not a query.
    """

    async def write_token(self, user: User) -> str:
        from fastapi_users.jwt import generate_jwt

        # Opaque per-login correlation id for audit events: random, never
        # derived from the credential, safe to hand to a collector.
        import uuid as _uuid
        session_id = _uuid.uuid4().hex
        try:
            user._session_id = session_id
        except Exception:
            pass
        data = {
            "saml_identity": getattr(user, "_saml_identity_hash", None),
            "sub": str(user.id),
            "aud": self.token_audience,
            SESSION_EPOCH_CLAIM: _session_epoch_of(user),
            "directory_subject": user.ldap_subject if getattr(user, "_directory_authenticated", False) else None,
            "sid": session_id,
        }
        return generate_jwt(
            data, self.encode_key, self.lifetime_seconds, algorithm=self.algorithm
        )

    async def read_token(self, token: Optional[str], user_manager) -> Optional[User]:
        from fastapi_users.jwt import decode_jwt

        user = await super().read_token(token, user_manager)
        if user is None:
            return None

        try:
            claims = decode_jwt(
                token, self.decode_key, self.token_audience, algorithms=[self.algorithm]
            )
        except Exception:
            return None

        if int(claims.get(SESSION_EPOCH_CLAIM, 0) or 0) != _session_epoch_of(user):
            logger.info(
                "Rejected session token for user %s: revoked (stale session_epoch)",
                user.id,
            )
            return None

        if claims.get("saml_identity"):
            from app.models.saml import SAMLIdentity
            async with user_manager.user_db.session as session:
                identity = (await session.execute(select(SAMLIdentity).where(
                    SAMLIdentity.identity_hash == claims["saml_identity"],
                    SAMLIdentity.user_id == user.id, SAMLIdentity.deleted_at.is_(None),
                ))).scalar_one_or_none()
                if identity is None:
                    return None
                cfg = next((p for p in settings.bow_config.saml_providers
                            if p.enabled and p.name == identity.provider
                            and p.organization_id == identity.organization_id), None)
                if cfg is None:
                    return None
                member = (await session.execute(select(Membership.id).where(
                    Membership.user_id == user.id,
                    Membership.organization_id == identity.organization_id,
                    Membership.deleted_at.is_(None),
                ))).first()
                if member is None:
                    return None

        if user.ldap_subject:
            if claims.get("directory_subject") != user.ldap_subject or not settings.bow_config.ldap.enabled:
                return None
            import asyncio
            from app.ee.ldap.connection import LDAPConnectionManager
            try:
                identity = user.ldap_identity or {}
                manager = LDAPConnectionManager(settings.bow_config.ldap)
                fresh = await asyncio.to_thread(manager.read_identity, identity["dn"])
                if any(fresh.get(k) != identity.get(k) for k in ("provider", "guid", "sid_hex", "principal")):
                    return None
            except Exception:
                return None
            user._directory_authenticated = True

        return user


def get_jwt_strategy() -> SessionEpochJWTStrategy:
    # Align JWT lifetime with frontend cookie (7 days) to avoid desync logout
    return SessionEpochJWTStrategy(secret=SECRET, lifetime_seconds=60 * 60 * 24 * 7)


class RevocableAuthenticationBackend(AuthenticationBackend):
    """Authentication backend whose logout actually invalidates the token.

    The stock backend calls `strategy.destroy_token`, swallows the
    "not supported" error a stateless JWT raises, and returns 204 — leaving the
    token usable. Bumping the session epoch first makes the 204 honest.
    """

    async def logout(self, strategy, user, token):
        await bump_session_epoch(user.id)
        from app.ee.audit.security_events import record_security_event
        await record_security_event("auth.logout", user=user, outcome="success", method="bearer",
                                    details={"email": user.email, "sessions_revoked": True})
        return await super().logout(strategy, user, token)


auth_backend = RevocableAuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)


def create_fastapi_users(
    get_user_manager: Callable,
    auth_backend: AuthenticationBackend,
    oauth_providers: List[BaseOAuth2] = None
) -> FastAPIUsers:
    if oauth_providers is None:
        oauth_providers = []
    return FastAPIUsers(get_user_manager, [auth_backend])
# verified user only!
fapi = create_fastapi_users(get_user_manager, auth_backend)
_jwt_current_user = fapi.current_user(active=True, optional=True)

# API Key authentication
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


_LAST_SEEN_DEBOUNCE = timedelta(hours=1)

async def _update_last_seen(user: User, db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    if user.last_seen and now - user.last_seen.replace(tzinfo=timezone.utc) < _LAST_SEEN_DEBOUNCE:
        return
    try:
        await db.execute(update(User).where(User.id == str(user.id)).values(last_seen=now))
        await db.commit()
    except Exception:
        pass


async def current_session_user(
    jwt_user: Optional[User] = Depends(_jwt_current_user),
) -> User:
    """Interactive login session only: no OAuth bearer tokens, no API keys.

    For endpoints that grant authority (OAuth consent). A delegated credential
    must never be able to mint itself, or another client, a new grant.
    """
    if jwt_user is None:
        raise HTTPException(
            status_code=401,
            detail="An interactive login session is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return jwt_user


def _remember_api_key(request: Request, key_row) -> None:
    """Record which organization a personal API key was created in, for
    ``get_current_organization``. Service-account keys are bound to their
    organization by the ``ServiceAccount`` row and need nothing here."""
    if key_row is not None and not key_row.service_account_id:
        request.state.personal_api_key_organization_id = str(key_row.organization_id)


async def current_user(
    request: Request,
    jwt_user: Optional[User] = Depends(_jwt_current_user),
    api_key: Optional[str] = Depends(api_key_header),
    db: AsyncSession = Depends(get_async_db),
) -> User:
    """The authenticated caller (JWT, OAuth token or API key), refused when
    they are an app user reaching outside their apps (core/workspace_access)."""
    user = await _authenticate(request, jwt_user, api_key, db)
    from app.core.workspace_access import gate_user

    await gate_user(request, db, user)
    return user


async def _authenticate(
    request: Request,
    jwt_user: Optional[User],
    api_key: Optional[str],
    db: AsyncSession,
) -> User:
    """
    Get the current user from either JWT token or API key.

    Tries JWT first, then OAuth, then falls back to API key authentication.
    API keys can be passed via X-API-Key header or Authorization: Bearer <key> (for bow_ prefixed keys).
    """
    # Try JWT first
    if jwt_user is not None:
        await _update_last_seen(jwt_user, db)
        return jwt_user

    # OAuth access tokens must be recognized before the generic ``bow_`` API
    # key path because ``bow_oauth_`` intentionally shares that broad prefix.
    # Main-app routes require the app scope; MCP performs its own mcp-scoped
    # authentication in routes/mcp.py.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer bow_oauth_"):
        from app.services.oauth_server_service import OAuthServerService

        token = auth_header[7:]
        context = await OAuthServerService().validate_access_token_context(
            db,
            token,
            required_scope="app",
        )
        if context:
            request.state.oauth_token_context = context
            from app.core.request_origin import OAUTH_APP, set_origin

            set_origin(OAUTH_APP, str(context.client.id))
            return context.user
        raise HTTPException(
            status_code=401,
            detail="Invalid, expired, or insufficiently scoped OAuth token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    
    # Try API key from X-API-Key header
    if api_key:
        from app.services.api_key_service import ApiKeyService
        api_key_service = ApiKeyService()
        user, key_row = await api_key_service.get_user_and_key_by_api_key(db, api_key)
        if user is not None:
            _remember_api_key(request, key_row)
            from app.core.request_origin import mark_api_key_request
            mark_api_key_request()  # runs this request starts are unattended
            return user

    # Try API key from Authorization header (for MCP clients that use Bearer format)
    if auth_header.startswith("Bearer bow_") and not auth_header.startswith("Bearer bow_oauth_"):
        from app.services.api_key_service import ApiKeyService
        api_key_service = ApiKeyService()
        bearer_api_key = auth_header[7:]  # Remove "Bearer " prefix
        user, key_row = await api_key_service.get_user_and_key_by_api_key(db, bearer_api_key)
        if user is not None:
            _remember_api_key(request, key_row)
            from app.core.request_origin import mark_api_key_request
            mark_api_key_request()  # runs this request starts are unattended
            return user
    
    # No valid authentication
    raise HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user_optional(
    request: Request,
    jwt_user: Optional[User] = Depends(_jwt_current_user),
    api_key: Optional[str] = Depends(api_key_header),
    db: AsyncSession = Depends(get_async_db),
) -> Optional[User]:
    """Same as current_user but returns None instead of raising 401."""
    try:
        return await current_user(request, jwt_user, api_key, db)
    except HTTPException:
        return None
    except AppError as e:
        # An app user outside their apps is nobody here, exactly as a signed-
        # out visitor is: a public page stays public, the rest asks to sign in.
        if e.error_code == ErrorCode.WORKSPACE_ACCESS_REQUIRED.value:
            return None
        raise


async def get_current_organization(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
) -> Organization:
    """Resolve the request's organization AND enforce that the caller still
    belongs to it.

    This is the single membership chokepoint for the HTTP surface: every route
    that depends on it inherits the check, including the ~138 org-scoped routes
    that carry no ``@requires_permission`` decorator. A user whose membership was
    removed (directly, or via LDAP/OIDC/SCIM sync) is rejected with 403 the
    moment they touch any org-scoped endpoint, instead of retaining access to
    everything gated only on "public within the org".

    Defined in core.auth (next to ``current_user``) rather than in dependencies
    to avoid a circular import; ``app.dependencies`` re-exports it lazily so
    ``from app.dependencies import get_current_organization`` keeps working.

    Membership-bootstrap flows (invite acceptance, org creation) do not depend on
    this — they establish the membership and so must run above the invariant.
    """
    from app.core.permission_resolver import assert_principal_belongs_to_org

    org = await resolve_organization(request, db)
    # A personal API key acts only in the organization it was created in, even
    # when its owner belongs to several. Multi-organization deployments only:
    # with a single organization the key's organization is the only one.
    key_org_id = getattr(request.state, "personal_api_key_organization_id", None)
    if (
        key_org_id is not None
        and key_org_id != str(org.id)
        and settings.bow_config.features.allow_multiple_organizations
    ):
        raise AppError.forbidden(
            ErrorCode.ACCESS_DENIED,
            "This API key belongs to a different organization",
        )
    await assert_principal_belongs_to_org(db, user, org.id)
    from app.core.workspace_access import gate_organization
    await gate_organization(request, db, user, str(org.id))
    from app.core.request_org import set_request_organization_id
    set_request_organization_id(str(org.id))
    return org


async def forbid_service_account_principal(user: User = Depends(current_user)) -> User:
    """Block service-account principals from privilege-management surfaces.

    A service account must never be able to mint keys, create/assign roles,
    invite members, or manage other service accounts — otherwise a leaked key
    could self-escalate. Applied to the RBAC, member-management, API-key, and
    service-account routers regardless of the account's RBAC role.
    """
    if getattr(user, "is_service_account", False):
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot manage identities, roles, or keys",
        )
    return user
