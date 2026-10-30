"""Map a host application's user onto a BOW user, for every SSO BOW supports.

A host application names one *identity source*; the subject it sends (the
assertion ``sub``, or the OIDC provider's subject in ``idp_token`` mode) is
looked up only there:

==========================  ==================================================
``oidc:<provider>``         ``oauth_accounts (oauth_name, account_id)`` -- what
                            OIDC login stores (``account_id`` = the provider's
                            ``uid_claim``). Entra, Okta, Keycloak, ADFS-OIDC,
                            Google.
``saml:<provider>``         ``saml_identities (provider, subject)`` -- what SAML
                            login stores.
``ldap``                    ``users.ldap_subject`` = ``<directory id>:<objectGUID>``
                            -- what Active Directory login stores.
``scim``                    ``users.scim_external_id`` -- what the IdP pushed.
``member_attribute:<key>``  ``memberships.custom_attributes[key]`` set by an
                            admin (deployments without a directory).
==========================  ==================================================

Rules shared by all sources: exactly one match, an active user, a live member
of the host application's organization. Email is never used to match.

Unknown subjects are refused unless the host application has
``create_users`` on; then the source's OWN admission rules decide, never the
host: invitations / domain sign-up policy for OIDC, the SAML provider's
``auto_provision_users``, and for Active Directory a directory lookup of the
objectGUID that must pass the admission group (directory-verified, not
host-trusted). An existing BOW account with the same email is never claimed.
SCIM and member attributes cannot create users.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.errors import AppError, ErrorCode
from app.models.membership import Membership
from app.models.oauth_account import OAuthAccount
from app.models.user import User
from app.settings.config import settings

logger = logging.getLogger(__name__)


def _unmapped(reason: str = "unmapped") -> AppError:
    return AppError.unauthorized(ErrorCode.EMBED_IDENTITY_UNMAPPED, "No account is mapped to this identity",
                                 reason=reason)


def _unavailable(message: str) -> AppError:
    return AppError.bad_request(ErrorCode.EMBED_IDENTITY_UNAVAILABLE, message)


def _split(source: Optional[str]) -> tuple[str, str]:
    if not source:
        raise _unavailable("This host application has no identity source")
    kind, _, name = source.partition(":")
    return kind, name


def _oidc_provider(name: str):
    return next((p for p in settings.bow_config.oidc_providers if p.name == name and p.enabled), None)


def _saml_provider(name: str, organization_id: str):
    return next((p for p in settings.bow_config.saml_providers
                 if p.name == name and p.enabled and str(p.organization_id) == str(organization_id)), None)


def _ldap_for(organization_id: str):
    cfg = settings.bow_config.ldap
    if cfg.enabled and cfg.organization_id and str(cfg.organization_id) == str(organization_id):
        return cfg
    return None


def source_options(organization_id: str) -> List[Dict[str, Any]]:
    """The identity sources this organization can use, for the settings form."""
    out: List[Dict[str, Any]] = []
    google = getattr(settings.bow_config, "google_oauth", None)
    if google is not None and getattr(google, "enabled", False):
        out.append({"value": "oidc:google", "label": "Google", "kind": "oidc",
                    "supports_create_users": True, "supports_idp_token": False})
    for p in settings.bow_config.oidc_providers:
        if p.enabled:
            out.append({"value": f"oidc:{p.name}", "label": p.display_label(), "kind": "oidc",
                        "supports_create_users": True, "supports_idp_token": True})
    for p in settings.bow_config.saml_providers:
        if p.enabled and str(p.organization_id) == str(organization_id):
            out.append({"value": f"saml:{p.name}", "label": p.label or p.name, "kind": "saml",
                        "supports_create_users": p.attributes.subject != "name_id"})
    if _ldap_for(organization_id):
        out.append({"value": "ldap", "label": "Active Directory (LDAP)", "kind": "ldap",
                    "supports_create_users": True})
    out.append({"value": "scim", "label": "SCIM external id", "kind": "scim", "supports_create_users": False})
    out.append({"value": "member_attribute:", "label": "Member attribute", "kind": "member_attribute",
                "supports_create_users": False})
    return out


def validate_source(source: Optional[str], organization_id: str, *, mode: str, create_users: bool) -> None:
    """Raise HOST_APP_CONFIG_INVALID unless ``source`` can work for this
    organization and identity mode."""
    def bad(message):
        return AppError.bad_request(ErrorCode.HOST_APP_CONFIG_INVALID, message, field="identity_source")

    if mode == "sso":
        return  # the viewer signs in to BOW itself; no host subject to map
    if not source:
        raise bad("An identity source is required")
    kind, name = _split(source)
    if mode == "idp_token" and kind != "oidc":
        raise bad("IdP tokens need an OIDC identity source")
    if kind == "oidc":
        if name == "google":
            google = getattr(settings.bow_config, "google_oauth", None)
            if not (google is not None and getattr(google, "enabled", False)):
                raise bad("Google sign-in is not configured")
            if mode == "idp_token":
                raise bad("Google tokens are not supported for this mode")
        elif _oidc_provider(name) is None:
            raise bad("Unknown or disabled OIDC provider")
    elif kind == "saml":
        cfg = _saml_provider(name, organization_id)
        if cfg is None:
            raise bad("Unknown or disabled SAML provider for this organization")
        if create_users and cfg.attributes.subject == "name_id":
            raise bad("Creating users needs a SAML attribute subject, not the NameID")
    elif kind == "ldap":
        if _ldap_for(organization_id) is None:
            raise bad("Active Directory is not configured for this organization")
    elif kind in ("scim", "member_attribute"):
        if create_users:
            raise bad("This identity source cannot create users")
        if kind == "member_attribute" and not name:
            raise bad("Name the member attribute, e.g. member_attribute:employee_id")
    else:
        raise bad("Unknown identity source")


async def _candidate_user_ids(db, source: str, subject: str, organization_id: str) -> List[str]:
    kind, name = _split(source)
    if kind == "oidc":
        rows = (await db.execute(select(OAuthAccount.user_id).where(
            OAuthAccount.oauth_name == name, OAuthAccount.account_id == subject))).scalars().all()
    elif kind == "saml":
        from app.models.saml import SAMLIdentity
        rows = (await db.execute(select(SAMLIdentity.user_id).where(
            SAMLIdentity.provider == name, SAMLIdentity.subject == subject,
            SAMLIdentity.organization_id == str(organization_id), SAMLIdentity.deleted_at.is_(None)))).scalars().all()
    elif kind == "ldap":
        cfg = _ldap_for(organization_id)
        if cfg is None:
            raise _unavailable("Active Directory is not configured for this organization")
        from app.ee.ldap.connection import LDAPConnectionManager
        provider = LDAPConnectionManager(cfg).provider_id
        rows = (await db.execute(select(User.id).where(User.ldap_subject == f"{provider}:{subject}"))).scalars().all()
    elif kind == "scim":
        rows = (await db.execute(select(User.id).join(Membership, Membership.user_id == User.id).where(
            User.scim_external_id == subject, Membership.organization_id == str(organization_id)))).scalars().all()
    elif kind == "member_attribute":
        members = (await db.execute(select(Membership).where(
            Membership.organization_id == str(organization_id), Membership.user_id.isnot(None),
            Membership.custom_attributes.isnot(None)))).scalars().all()
        rows = [m.user_id for m in members
                if str((m.custom_attributes or {}).get(name, "") or "").strip() == subject]
    else:
        raise _unavailable("Unknown identity source")
    return sorted({str(r) for r in rows if r})


async def _active_member(db, user_id: str, organization_id: str) -> User:
    from app.core.permission_resolver import principal_belongs_to_org

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise AppError.forbidden(ErrorCode.EMBED_USER_INACTIVE, "This account is inactive")
    if not await principal_belongs_to_org(db, user, organization_id):
        raise _unmapped("not_a_member")
    return user


async def resolve_user(db, host, subject: str, claims: Dict[str, Any]) -> User:
    """The one active member ``subject`` names in the host's identity source,
    created first when the host allows it and the source admits them."""
    subject = str(subject or "").strip()
    if not subject:
        raise _unmapped()
    org_id = str(host.organization_id)
    ids = await _candidate_user_ids(db, host.identity_source, subject, org_id)
    if len(ids) > 1:
        raise AppError.unauthorized(ErrorCode.EMBED_IDENTITY_AMBIGUOUS, "More than one account matches this identity")
    if ids:
        return await _active_member(db, ids[0], org_id)
    if not host.create_users:
        raise _unmapped()
    try:
        user_id = await _admit(db, host, subject, claims)
    except AppError:
        raise
    except IntegrityError:
        # A concurrent first sign-in created the binding first: use theirs.
        await db.rollback()
        ids = await _candidate_user_ids(db, host.identity_source, subject, org_id)
        if len(ids) != 1:
            raise _unmapped()
        user_id = ids[0]
    return await _active_member(db, user_id, org_id)


def _email_claim(claims: Dict[str, Any]) -> str:
    from pydantic import EmailStr, TypeAdapter, ValidationError

    for key in ("email", "preferred_username", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and "@" in value:
            try:
                return str(TypeAdapter(EmailStr).validate_python(value.strip())).lower()
            except ValidationError:
                continue
    raise _unmapped("email_required")


async def _email_taken(db, email: str) -> bool:
    return (await db.execute(select(User.id).where(func.lower(User.email) == email))).first() is not None


async def _admit(db, host, subject: str, claims: Dict[str, Any]) -> str:
    kind, name = _split(host.identity_source)
    org_id = str(host.organization_id)
    if kind == "oidc":
        return await _admit_oidc(db, org_id, name, subject, claims)
    if kind == "saml":
        return await _admit_saml(db, org_id, name, subject, claims)
    if kind == "ldap":
        return await _admit_ldap(db, org_id, subject)
    raise _unmapped()


async def _admit_oidc(db, org_id: str, provider: str, subject: str, claims: Dict[str, Any]) -> str:
    from app.core.auth import auto_provision_user_for_org
    from app.core.permission_resolver import ensure_system_role_assignment

    email = _email_claim(claims)
    if await _email_taken(db, email):
        # An existing account is only ever bound by signing in through the
        # provider itself, never by a host claiming its email.
        raise _unmapped("email_in_use")
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    user = await auto_provision_user_for_org(db, org_id, email, name=name)
    if user is None:
        raise _unmapped("not_admitted")
    db.add(OAuthAccount(oauth_name=provider, access_token="", account_id=subject, account_email=email,
                        user_id=str(user.id)))
    membership = (await db.execute(select(Membership).where(
        Membership.user_id == str(user.id), Membership.organization_id == org_id))).scalars().first()
    await ensure_system_role_assignment(db, org_id, str(user.id), (membership.role if membership else None) or "member")
    await db.commit()
    return str(user.id)


async def _admit_saml(db, org_id: str, provider: str, subject: str, claims: Dict[str, Any]) -> str:
    from app.services.saml_service import SAMLFailure, admit_user, digest, toolkit_settings

    cfg = _saml_provider(provider, org_id)
    if cfg is None or cfg.attributes.subject == "name_id":
        raise _unavailable("SAML first sign-in needs an attribute subject")
    email = _email_claim(claims)
    if await _email_taken(db, email):
        raise _unmapped("email_in_use")
    issuer = cfg.idp.entity_id or (await toolkit_settings(cfg))["idp"]["entityId"]
    # The same identity hash SAML login computes for an attribute subject,
    # so a later browser login through the IdP finds this identity.
    identity_hash = digest(json.dumps([cfg.name, cfg.organization_id, issuer, cfg.attributes.subject,
                                       subject, None, None]))
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    try:
        user = await admit_user(cfg, issuer, subject, identity_hash, email, name or email.split("@")[0], db)
    except SAMLFailure:
        raise _unmapped("not_admitted")
    return str(user.id)


async def _admit_ldap(db, org_id: str, guid: str) -> str:
    import uuid as _uuid

    from app.core.auth import admit_directory_identity
    from app.ee.ldap.connection import LDAPConnectionManager, LDAPSecurityError

    cfg = _ldap_for(org_id)
    if cfg is None:
        raise _unavailable("Active Directory is not configured for this organization")
    try:
        _uuid.UUID(guid)
    except ValueError:
        raise _unmapped("invalid_subject")
    manager = LDAPConnectionManager(cfg)
    try:
        identity = await asyncio.to_thread(manager.identity_by_guid, guid)
    except LDAPSecurityError as e:
        logger.info("Directory admission refused for an embedded sign-in: %s", type(e).__name__)
        raise _unmapped("not_admitted")
    if not identity:
        raise _unmapped("not_in_directory")
    user = await admit_directory_identity(db, identity, cfg)
    if user is None:
        raise _unmapped("not_admitted")
    return str(user.id)


def subject_digest(value: str) -> str:
    """Logged instead of the subject itself."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]
