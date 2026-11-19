"""Directory (SAML, LDAP) sign-in for instance administrators.

Contract:
  * An account already linked to a directory identity that is an instance
    administrator because its email is in ``BOW_SUPERUSER_EMAILS`` keeps
    signing in through that directory, including after the startup pass has
    already made it an instance administrator.
  * Taking the address off the list removes the role, not the sign-in.
  * With no configured list, an instance administrator (flag set by hand)
    stays a local break-glass account: its directory sign-in is refused.
  * A directory identity never takes over an existing local account by email
    match, instance administrator or not, on the list or not.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import update

from app.core.instance_admin import sync_superusers_at_startup
from app.dependencies import async_session_maker
from app.ee.ldap.connection import LDAPConnectionManager
from app.models.user import User
from app.settings.bow_config import LDAPConfig
from app.settings.config import settings
from tests.e2e.test_saml_auth import login_form, redeem, saml_env, submit  # noqa: F401 (fixture)

pytestmark = pytest.mark.e2e


@pytest.fixture
def superuser_emails(monkeypatch):
    def _set(*emails):
        monkeypatch.setattr(settings.bow_config.auth, "superuser_emails", [e.lower() for e in emails])

    return _set


def _set_superuser_directly(email, value=True):
    # The hand-set flag has no API (it is the "edited in the database" setup).
    async def _run():
        async with async_session_maker() as db:
            await db.execute(update(User).where(User.email == email).values(is_superuser=value))
            await db.commit()

    asyncio.run(_run())


def _email(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


# --- SAML ---------------------------------------------------------------------


def _saml_sign_in(env, email):
    return submit(env, login_form(env, email=email))


def test_saml_user_named_as_instance_admin_keeps_signing_in(saml_env, superuser_emails, whoami):
    email = _email("saml-root")
    first = whoami(redeem(saml_env, _saml_sign_in(saml_env, email)))
    assert first["is_superuser"] is False

    superuser_emails(email)
    asyncio.run(sync_superusers_at_startup())  # restart: the role is set before the next sign-in

    me = whoami(redeem(saml_env, _saml_sign_in(saml_env, email)))
    assert me["id"] == first["id"]
    assert me["is_superuser"] is True

    # Off the list again: still signs in, without the role.
    superuser_emails(_email("someone-else"))
    assert whoami(redeem(saml_env, _saml_sign_in(saml_env, email)))["is_superuser"] is False


def test_saml_user_named_as_instance_admin_gets_the_role_at_sign_in(saml_env, superuser_emails, whoami):
    email = _email("saml-root")
    redeem(saml_env, _saml_sign_in(saml_env, email))
    superuser_emails(email)

    assert whoami(redeem(saml_env, _saml_sign_in(saml_env, email)))["is_superuser"] is True
    assert whoami(redeem(saml_env, _saml_sign_in(saml_env, email)))["is_superuser"] is True


def test_saml_sign_in_is_refused_for_a_hand_set_instance_admin(saml_env, superuser_emails):
    email = _email("saml-user")
    redeem(saml_env, _saml_sign_in(saml_env, email))
    _set_superuser_directly(email)

    assert "login_code=" not in _saml_sign_in(saml_env, email).headers["location"]


@pytest.mark.parametrize("on_list", [False, True], ids=["hand-set", "named-in-configuration"])
def test_saml_identity_cannot_take_over_a_local_instance_admin_by_email(
    saml_env, superuser_emails, create_user, login_user, whoami, on_list
):
    local = create_user(email=_email("local-root"))
    if on_list:
        superuser_emails(local["email"])
    else:
        _set_superuser_directly(local["email"])
    assert whoami(login_user(local["email"], local["password"]))["is_superuser"] is True

    assert "login_code=" not in _saml_sign_in(saml_env, local["email"]).headers["location"]


# --- LDAP ---------------------------------------------------------------------


@pytest.fixture
def directory(test_client, create_user, login_user, whoami, monkeypatch):
    """LDAP sign-in with only the directory's network calls replaced."""
    admin = create_user()
    org_id = whoami(login_user(admin["email"], admin["password"]))["organizations"][0]["id"]
    config = LDAPConfig(
        enabled=True, url="ldaps://ad.example.test", organization_id=org_id,
        admission_group_dn="cn=admitted,dc=example,dc=test", kerberos_realm="EXAMPLE.TEST",
        auto_provision_users=True,
    )
    monkeypatch.setattr(settings.bow_config, "ldap", config)
    people = {}

    def _identity(*args):
        return people.get(str(args[1]).lower()) if len(args) > 1 else None

    monkeypatch.setattr(LDAPConnectionManager, "authenticate_identity", _identity)
    monkeypatch.setattr(
        LDAPConnectionManager, "read_identity",
        lambda *a: next(iter(people.values()), None) if len(people) == 1 else None,
    )

    def _add(email):
        people[email.lower()] = {
            "provider": LDAPConnectionManager(config).provider_id, "guid": str(uuid.uuid4()),
            "sid_hex": "010100000000000501000000", "principal": "person@EXAMPLE.TEST", "email": email,
            "name": "Directory person", "dn": "cn=person,dc=example,dc=test", "organization_id": org_id,
        }

    def _sign_in(email):
        return test_client.post("/api/auth/jwt/login", data={"username": email, "password": "directory-password"})

    return {"add": _add, "sign_in": _sign_in}


def test_ldap_user_named_as_instance_admin_keeps_signing_in(directory, superuser_emails, whoami):
    email = _email("ldap-root")
    directory["add"](email)
    first = directory["sign_in"](email)
    assert first.status_code == 200, first.text
    first_me = whoami(first.json()["access_token"])
    assert first_me["is_superuser"] is False

    superuser_emails(email)
    asyncio.run(sync_superusers_at_startup())  # restart: the role is set before the next sign-in

    again = directory["sign_in"](email)
    assert again.status_code == 200, again.text
    me = whoami(again.json()["access_token"])
    assert me["id"] == first_me["id"]
    assert me["is_superuser"] is True


def test_ldap_sign_in_is_refused_for_a_hand_set_instance_admin(directory):
    email = _email("ldap-user")
    directory["add"](email)
    assert directory["sign_in"](email).status_code == 200
    _set_superuser_directly(email)

    assert directory["sign_in"](email).status_code in (400, 401, 403)


@pytest.mark.parametrize("on_list", [False, True], ids=["hand-set", "named-in-configuration"])
def test_ldap_identity_cannot_take_over_a_local_instance_admin_by_email(
    directory, superuser_emails, create_user, monkeypatch, on_list
):
    # Created while the directory is on, so registered straight in the database
    # path the app offers: sign-up, then the flag.
    local_email = _email("local-root")
    monkeypatch.setattr(settings.bow_config.features, "allow_uninvited_signups", True)
    create_user(email=local_email)
    if on_list:
        superuser_emails(local_email)
        asyncio.run(sync_superusers_at_startup())
    else:
        _set_superuser_directly(local_email)

    directory["add"](local_email)

    assert directory["sign_in"](local_email).status_code in (400, 401, 403)
