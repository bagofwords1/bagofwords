"""An invite past its expiry grants nothing, whichever way it would be claimed.

Contract: an expired invite is not turned into a membership by a sign-in that
matches its email, by a first sign-in through a provider, or by its link. It
stays pending, and works again once an admin resends it.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from tests.e2e.org_isolation.identity_helpers import (
    FakeIdentityProvider, bootstrap_admin, feature, hdr, invite, login, oidc_provider, org_ids,
    pending_invite_emails, register, second_org_admin, unique_email, whoami,
)


def _expire(membership_id):
    # No API moves an invite's expiry into the past.
    from app.dependencies import async_session_maker
    from app.models.membership import Membership

    async def _set():
        async with async_session_maker() as db:
            await db.execute(update(Membership).where(Membership.id == membership_id).values(
                invite_expires_at=datetime.utcnow() - timedelta(days=1)))
            await db.commit()

    asyncio.run(_set())


def _resend(client, admin, membership_id):
    resp = client.post(
        f"/api/organizations/{admin['org_id']}/members/{membership_id}/resend",
        headers=hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("role", ["admin", "member"])
def test_existing_users_trusted_sign_in_does_not_claim_an_expired_invite(test_client, monkeypatch, role):
    with feature("allow_multiple_organizations", True):
        first = bootstrap_admin(test_client)
        second = second_org_admin(test_client, first)
        email = unique_email("invitee")
        _, first_token = invite(test_client, first["token"], first["org_id"], email)
        expired_id, _ = invite(test_client, second["token"], second["org_id"], email, role=role)
        assert register(test_client, email, invite_token=first_token).status_code == 201
        _expire(expired_id)
        idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

        outcome, session = idp.sign_in(test_client, "corp", email)

        assert outcome == "ok"
        assert org_ids(test_client, session) == {first["org_id"]}
        assert email in pending_invite_emails(test_client, second["token"], second["org_id"])

        # Resent by the admin, it is live again and the next sign-in claims it.
        _resend(test_client, second, expired_id)
        outcome, session = idp.sign_in(test_client, "corp", email)
        assert outcome == "ok"
        assert org_ids(test_client, session) == {first["org_id"], second["org_id"]}
        roles = {o["id"]: o["role"] for o in whoami(test_client, session)["organizations"]}
        assert roles[second["org_id"]] == role


def test_new_provider_user_is_not_admitted_by_an_expired_invite(test_client, monkeypatch):
    admin = bootstrap_admin(test_client)
    expired_email, live_email = unique_email("late"), unique_email("on_time")
    expired_id, _ = invite(test_client, admin["token"], admin["org_id"], expired_email, role="admin")
    invite(test_client, admin["token"], admin["org_id"], live_email, role="admin")
    _expire(expired_id)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

    assert idp.sign_in(test_client, "corp", expired_email) == ("error", "invitation_required")
    assert expired_email in pending_invite_emails(test_client, admin["token"], admin["org_id"])
    # Where anyone may sign up the account is created, without the invite's access.
    with feature("allow_uninvited_signups", True):
        outcome, session = idp.sign_in(test_client, "corp", expired_email)
    assert outcome == "ok" and org_ids(test_client, session) == set()
    assert expired_email in pending_invite_emails(test_client, admin["token"], admin["org_id"])

    # An invite inside its window is claimed as before.
    outcome, session = idp.sign_in(test_client, "corp", live_email)
    assert outcome == "ok" and org_ids(test_client, session) == {admin["org_id"]}

    _resend(test_client, admin, expired_id)
    outcome, session = idp.sign_in(test_client, "corp", expired_email)
    assert outcome == "ok" and org_ids(test_client, session) == {admin["org_id"]}


def test_expired_link_is_refused_for_a_signed_in_user_until_resent(test_client):
    with feature("allow_multiple_organizations", True):
        first = bootstrap_admin(test_client)
        second = second_org_admin(test_client, first)
        email = unique_email("invitee")
        _, first_token = invite(test_client, first["token"], first["org_id"], email)
        expired_id, expired_token = invite(test_client, second["token"], second["org_id"], email)
        assert register(test_client, email, invite_token=first_token).status_code == 201
        session = login(test_client, email).json()["access_token"]
        _expire(expired_id)

        refused = test_client.post("/api/invites/accept", json={"token": expired_token}, headers=hdr(session))
        assert refused.status_code == 400, refused.text
        assert org_ids(test_client, session) == {first["org_id"]}

        _resend(test_client, second, expired_id)
        _, fresh_token = invite_link(test_client, second, expired_id)
        assert test_client.post("/api/invites/accept", json={"token": expired_token}, headers=hdr(session)).status_code == 400
        assert test_client.post("/api/invites/accept", json={"token": fresh_token}, headers=hdr(session)).status_code == 200
        assert org_ids(test_client, session) == {first["org_id"], second["org_id"]}


def test_open_sign_up_by_email_does_not_claim_an_expired_invite(test_client):
    """Single organization, open sign-ups: a typed address matches invites by email."""
    admin = bootstrap_admin(test_client)
    email = unique_email("late")
    expired_id, _ = invite(test_client, admin["token"], admin["org_id"], email)
    _expire(expired_id)

    with feature("allow_uninvited_signups", True):
        assert register(test_client, email).status_code == 201
    session = login(test_client, email).json()["access_token"]

    assert org_ids(test_client, session) == set()
    assert email in pending_invite_emails(test_client, admin["token"], admin["org_id"])


def invite_link(client, admin, membership_id):
    link = client.get(
        f"/api/organizations/{admin['org_id']}/members/{membership_id}/invite-link",
        headers=hdr(admin["token"], admin["org_id"]),
    )
    assert link.status_code == 200, link.text
    return link.json()["url"], link.json()["token"]
