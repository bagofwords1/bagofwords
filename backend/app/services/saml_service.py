"""SAML 2.0 Web Browser SSO (Redirect AuthnRequest, POST response).

Only operator-configured IdPs are trusted. Every login is correlated with a
browser-bound, single-use database request; unsolicited assertions are refused.
"""

import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import delete, select, update, func
from fastapi_users.password import PasswordHelper

from app.errors import AppError, ErrorCode
from app.models.saml import SAMLIdentity, SAMLRequest
from app.models.user import User
from app.models.membership import Membership
from app.models.organization import Organization
from app.settings.config import settings


class SAMLFailure(ValueError):
    """Safe, fixed diagnostic reason; never contains assertion or user data."""


MAX_XML_BYTES = 2 * 1024 * 1024
_metadata_cache = {}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def provider_config(name):
    for cfg in settings.bow_config.saml_providers:
        if cfg.name == name and cfg.enabled:
            return cfg
    raise AppError.not_found(ErrorCode.SAML_UNAVAILABLE, "SSO provider is unavailable")


def config_hash(cfg):
    return digest(cfg.model_dump_json() + str(settings.bow_config.base_url))


def endpoints(cfg):
    base = settings.bow_config.base_url.rstrip("/")
    return {
        "entity_id": cfg.sp.entity_id or f"{base}/saml/{cfg.name}",
        "acs": f"{base}/api/auth/saml/{cfg.name}/acs",
    }


async def toolkit_settings(cfg):
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

    idp = cfg.idp
    if idp.metadata_file or idp.metadata_url:
        key = config_hash(cfg)
        cached = _metadata_cache.get(key)
        if idp.metadata_file:
            raw = await asyncio.to_thread(_read_bounded, idp.metadata_file)
        elif cached and cached[0] > time.monotonic():
            raw = cached[1]
        else:
            # URL is operator-controlled HTTPS config. No redirects, no
            # assertion-supplied URLs, bounded fetch and response size.
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async with client.stream("GET", idp.metadata_url) as response:
                    response.raise_for_status()
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_XML_BYTES:
                            raise SAMLFailure("SAML metadata too large")
                    raw = bytes(chunks)
            _metadata_cache[key] = (time.monotonic() + 300, raw)
        # Require unambiguous metadata selection; operator may pin an entity.
        from defusedxml.ElementTree import fromstring

        tree = fromstring(raw)
        # Honor metadata expiry, including enclosing federation descriptors.
        for descriptor in tree.iter():
            expiry = descriptor.get("validUntil")
            if expiry and datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise SAMLFailure("IdP metadata expired")
        entities = [
            e
            for e in tree.iter()
            if e.tag == "{urn:oasis:names:tc:SAML:2.0:metadata}EntityDescriptor"
            and e.find("{urn:oasis:names:tc:SAML:2.0:metadata}IDPSSODescriptor") is not None
        ]
        if idp.entity_id:
            entities = [e for e in entities if e.get("entityID") == idp.entity_id]
        if len(entities) != 1:
            raise SAMLFailure("SAML metadata must identify exactly one configured IdP")
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(raw, entity_id=entities[0].get("entityID"))
        trusted = parsed["idp"]
    else:
        trusted = {
            "entityId": idp.entity_id,
            "singleSignOnService": {"url": idp.sso_url, "binding": C.BINDING_HTTP_REDIRECT},
            "x509certMulti": {"signing": idp.certificates},
        }
    sso = trusted.get("singleSignOnService", {})
    if urlsplit(sso.get("url", "")).scheme != "https" or sso.get("binding") != C.BINDING_HTTP_REDIRECT:
        raise SAMLFailure("IdP must expose an HTTPS Redirect SSO endpoint")
    if not (trusted.get("x509cert") or trusted.get("x509certMulti", {}).get("signing")):
        raise SAMLFailure("IdP signing certificate required")
    urls = endpoints(cfg)
    sp = {
        "entityId": urls["entity_id"],
        "assertionConsumerService": {"url": urls["acs"], "binding": C.BINDING_HTTP_POST},
        "NameIDFormat": cfg.sp.name_id_format,
    }
    if cfg.sp.private_key_file:
        sp["privateKey"] = (await asyncio.to_thread(_read_bounded, cfg.sp.private_key_file)).decode()
        sp["x509cert"] = (await asyncio.to_thread(_read_bounded, cfg.sp.certificate_file)).decode()
    return {
        "strict": True,
        "debug": False,
        "sp": sp,
        "idp": trusted,
        "security": {
            "authnRequestsSigned": cfg.sp.sign_requests,
            "wantAssertionsEncrypted": cfg.sp.want_assertions_encrypted,
            # Toolkit requires a valid signature on the response OR
            # assertion even when both 'want' options are false.
            "wantAssertionsSigned": False,
            "wantMessagesSigned": False,
            "wantAttributeStatement": True,
            "wantNameId": True,
            "requestedAuthnContext": False,
            "rejectDeprecatedAlgorithm": True,
            "signatureAlgorithm": C.RSA_SHA256,
            "digestAlgorithm": C.SHA256,
        },
    }


def _read_bounded(path):
    with Path(path).open("rb") as f:
        raw = f.read(MAX_XML_BYTES + 1)
    if len(raw) > MAX_XML_BYTES:
        raise SAMLFailure("SAML file too large")
    return raw


def request_data(cfg, post=None):
    # Use the canonical operator-configured origin, never untrusted Host or
    # X-Forwarded-* input to construct an audience or a callback URL.
    url = urlsplit(endpoints(cfg)["acs"])
    return {
        "https": "on",
        "http_host": url.netloc,
        "server_port": str(url.port or 443),
        "script_name": url.path,
        "get_data": {},
        "post_data": post or {},
    }


async def metadata(cfg):
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    conf = await toolkit_settings(cfg)
    saml = OneLogin_Saml2_Settings(conf)
    document = saml.get_sp_metadata()
    if saml.validate_metadata(document):
        raise SAMLFailure("Invalid SP metadata")
    return document


async def start_login(cfg, db):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    conf = await toolkit_settings(cfg)
    auth = OneLogin_Saml2_Auth(request_data(cfg), old_settings=conf)
    relay, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    url = auth.login(return_to=relay)
    now = datetime.utcnow()
    # Keep consumed assertion IDs for one day (well beyond the five-minute
    # request window), preventing replays while bounding database growth.
    await db.execute(delete(SAMLRequest).where(SAMLRequest.expires_at < now - timedelta(days=1)))
    db.add(
        SAMLRequest(
            request_id=auth.get_last_request_id(),
            provider=cfg.name,
            config_hash=config_hash(cfg),
            relay_hash=digest(relay),
            browser_hash=digest(browser),
            expires_at=now + timedelta(minutes=5),
        )
    )
    await db.commit()
    return url, browser


async def finish_login(cfg, post, browser, db):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    if not browser or not post.get("RelayState") or not post.get("SAMLResponse"):
        raise SAMLFailure("Missing browser correlation")
    row = (
        await db.execute(
            select(SAMLRequest).where(
                SAMLRequest.relay_hash == digest(post["RelayState"]),
                SAMLRequest.provider == cfg.name,
                SAMLRequest.browser_hash == digest(browser),
                SAMLRequest.config_hash == config_hash(cfg),
                SAMLRequest.consumed_at.is_(None),
                SAMLRequest.expires_at > datetime.utcnow(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SAMLFailure("Unknown or expired request")
    conf = await toolkit_settings(cfg)
    auth = OneLogin_Saml2_Auth(request_data(cfg, post), old_settings=conf)
    await asyncio.to_thread(auth.process_response, request_id=row.request_id)
    if auth.get_errors() or not auth.is_authenticated():
        # Never log raw assertions, attributes, or library error text.
        raise SAMLFailure("Invalid SAML response")
    if not auth.get_last_assertion_id():
        raise SAMLFailure("Missing assertion ID")
    # Require explicit request correlation in both the response and bearer
    # confirmation; the toolkit permits absent InResponseTo in some profiles.
    from defusedxml.ElementTree import fromstring

    xml = fromstring(auth.get_last_response_xml())
    if xml.get("Destination") != endpoints(cfg)["acs"]:
        raise SAMLFailure("Wrong destination")
    if xml.get("InResponseTo") != row.request_id:
        raise SAMLFailure("Uncorrelated response")
    subject_data = xml.findall(".//{urn:oasis:names:tc:SAML:2.0:assertion}SubjectConfirmationData")
    if not subject_data or any(
        x.get("InResponseTo") != row.request_id
        or x.get("Recipient") != endpoints(cfg)["acs"]
        or not x.get("NotOnOrAfter")
        for x in subject_data
    ):
        raise SAMLFailure("Uncorrelated subject")
    audiences = xml.findall(".//{urn:oasis:names:tc:SAML:2.0:assertion}Audience")
    if endpoints(cfg)["entity_id"] not in [a.text for a in audiences]:
        raise SAMLFailure("Audience required")
    conditions = xml.findall(".//{urn:oasis:names:tc:SAML:2.0:assertion}Conditions")
    if len(conditions) != 1 or not conditions[0].get("NotOnOrAfter"):
        raise SAMLFailure("Assertion expiry required")
    attrs = auth.get_attributes()

    def single(name, required=True):
        vals = attrs.get(name, []) if name else []
        if len(vals) != 1 or not isinstance(vals[0], str) or not vals[0].strip():
            if required:
                raise SAMLFailure("Missing or ambiguous identity attribute")
            return None
        return vals[0].strip()

    subject = auth.get_nameid() if cfg.attributes.subject == "name_id" else single(cfg.attributes.subject)
    if (
        not subject
        or len(subject) > 2048
        or (
            cfg.attributes.subject == "name_id"
            and auth.get_nameid_format() == "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
        )
    ):
        raise SAMLFailure("Stable subject required")
    email = str(TypeAdapter(EmailStr).validate_python(single(cfg.attributes.email))).lower()
    name = single(cfg.attributes.name, required=False) or email.split("@")[0]
    issuer = conf["idp"]["entityId"]
    # NameID qualifiers distinguish subjects in IdPs that use them.
    identity_hash = digest(
        json.dumps(
            [
                cfg.name,
                cfg.organization_id,
                issuer,
                cfg.attributes.subject,
                subject,
                auth.get_nameid_nq() if cfg.attributes.subject == "name_id" else None,
                auth.get_nameid_spnq() if cfg.attributes.subject == "name_id" else None,
            ]
        )
    )
    result = await db.execute(
        update(SAMLRequest)
        .where(SAMLRequest.id == row.id, SAMLRequest.consumed_at.is_(None), SAMLRequest.expires_at > datetime.utcnow())
        .values(consumed_at=datetime.utcnow(), assertion_hash=digest(issuer + "\0" + auth.get_last_assertion_id()))
    )
    if result.rowcount != 1:
        raise SAMLFailure("Request already consumed")
    # Consume before admission, including failed admissions. A retry must start
    # a fresh authentication request, not re-submit a previously valid response.
    await db.commit()
    user = await admit_user(cfg, issuer, subject, identity_hash, email, name, db)
    user._saml_identity_hash = identity_hash
    return user


async def admit_user(cfg, issuer, subject, identity_hash, email, name, db):
    from app.core.permission_resolver import ensure_system_role_assignment
    from app.core.seats import seats_remaining

    org = await db.get(Organization, cfg.organization_id)
    if not org or org.deleted_at:
        raise SAMLFailure("Organization unavailable")
    identity = (
        await db.execute(select(SAMLIdentity).where(SAMLIdentity.identity_hash == identity_hash))
    ).scalar_one_or_none()
    user = await db.get(User, identity.user_id) if identity else None
    collision = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if identity:
        if identity.deleted_at or not user or (collision and collision.id != user.id):
            raise SAMLFailure("Identity unavailable")
    elif collision:
        # Only an explicit operator link can attach an existing account.
        if cfg.account_links.get(subject, "").lower() != email:
            raise SAMLFailure("Explicit account link required")
        user = collision
    if user:
        if not user.is_active or user.is_superuser or user.is_service_account or user.ldap_subject:
            raise SAMLFailure("Account unavailable")
        membership = (
            await db.execute(
                select(Membership).where(
                    Membership.user_id == user.id,
                    Membership.organization_id == cfg.organization_id,
                    Membership.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not membership:
            raise SAMLFailure("Organization admission required")
    else:
        invite = (
            (
                await db.execute(
                    select(Membership).where(
                        func.lower(Membership.email) == email,
                        Membership.organization_id == cfg.organization_id,
                        Membership.user_id.is_(None),
                        Membership.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        valid_invite = invite and (invite.invite_expires_at is None or invite.invite_expires_at > datetime.utcnow())
        if not cfg.auto_provision_users and not valid_invite:
            raise SAMLFailure("Invitation required")
        remaining = await seats_remaining(db, cfg.organization_id)
        if not valid_invite and remaining is not None and remaining <= 0:
            raise SAMLFailure("No seats available")
        helper = PasswordHelper()
        user = User(
            email=email,
            name=name[:255],
            hashed_password=helper.hash(helper.generate()),
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        db.add(user)
        await db.flush()
        if valid_invite:
            invite.user_id = user.id
            invite.invite_token = None
            invite.directory_provider = "saml:" + cfg.name
            role = invite.role
        else:
            db.add(
                Membership(
                    user_id=user.id,
                    organization_id=cfg.organization_id,
                    role="member",
                    directory_provider="saml:" + cfg.name,
                )
            )
            role = "member"
        await ensure_system_role_assignment(db, cfg.organization_id, str(user.id), role)
    if not identity:
        db.add(
            SAMLIdentity(
                identity_hash=identity_hash,
                provider=cfg.name,
                issuer=issuer,
                subject=subject,
                organization_id=cfg.organization_id,
                user_id=user.id,
            )
        )
    user.email = email
    user.name = name[:255]
    user.last_login = datetime.utcnow()
    await db.commit()
    return user
