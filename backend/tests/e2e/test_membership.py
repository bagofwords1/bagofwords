import pytest
import uuid


@pytest.mark.e2e
def test_membership_create_and_list(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Test creating a membership via email invitation and listing members."""
    # Create admin user and login (first user gets auto-created org)
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)['organizations'][0]['id']
    
    # Generate email for second user
    second_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    
    # First INVITE the second user by email (creates pending membership)
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert invite_response.status_code == 200, invite_response.json()
    membership = invite_response.json()
    assert membership["email"] == second_email
    assert membership["role"] == "member"
    assert membership["user_id"] is None  # Not yet registered
    # The invite-email outcome is surfaced on the response. The test env has no
    # SMTP configured, so it should report skipped rather than silently "ok".
    assert membership["invite_email_status"] == "skipped_no_smtp"
    
    # Now the second user can register with that invited email
    second_user = create_user(email=second_email, password="test123")
    second_token = login_user(second_user["email"], second_user["password"])
    second_user_info = whoami(second_token)
    second_user_id = second_user_info['id']  # User fields are at top level
    
    # Verify second user is now in the org
    second_user_org_ids = [o['id'] for o in second_user_info['organizations']]
    assert org_id in second_user_org_ids, "Invited user should be in the organization after registration"
    
    # List members from admin's perspective
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    members = response.json()
    member_user_ids = [m["user_id"] for m in members if m["user_id"]]
    assert second_user_id in member_user_ids


@pytest.mark.e2e
def test_membership_delete(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Test deleting a membership."""
    # Create admin user
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)['organizations'][0]['id']
    
    # Invite second user
    second_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert invite_response.status_code == 200
    membership_id = invite_response.json()["id"]
    
    # Second user registers
    create_user(email=second_email, password="test123")
    
    # Delete membership
    response = test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 204
    
    # Verify member is gone
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    members = response.json()
    member_ids = [m["id"] for m in members]
    assert membership_id not in member_ids


@pytest.mark.e2e
def test_invite_email_is_normalized_and_case_insensitive(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Invited emails are stored lowercase and de-duplicated case-insensitively.

    Regression: SSO/OIDC providers (Entra, Okta) return the email in whatever
    casing the directory holds (e.g. ``HishamHl@Fattal.co.il``). The invite match
    at login is case-sensitive equality, so an invite typed in a different case
    would never match and the user got ``invitation_required`` despite being
    invited. Invites are now normalized to lowercase on write and matched
    case-insensitively.
    """
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)['organizations'][0]['id']
    hdr = {"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}

    mixed = f"Hisham.{uuid.uuid4().hex[:8]}@Fattal.CO.IL"

    # Invite with mixed-case email → stored lowercase.
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": mixed, "role": "member"},
        headers=hdr,
    )
    assert invite.status_code == 200, invite.json()
    assert invite.json()["email"] == mixed.lower()

    # Re-inviting the same address in a different case is rejected as a duplicate.
    dup = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": mixed.upper(), "role": "member"},
        headers=hdr,
    )
    assert dup.status_code == 400, dup.json()

    # Registering with the invited email attaches the pending membership.
    invited_user = create_user(email=mixed.lower(), password="test123")
    invited_token = login_user(invited_user["email"], invited_user["password"])
    org_ids = [o["id"] for o in whoami(invited_token)["organizations"]]
    assert org_id in org_ids, "Invited user should be attached to the org after registration"


@pytest.mark.e2e
def test_user_loses_access_after_membership_removal(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Test that a user cannot access org resources after their membership is removed."""
    # Create admin user
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)['organizations'][0]['id']
    
    # Invite second user by email first
    second_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert invite_response.status_code == 200
    membership_id = invite_response.json()["id"]
    
    # Now second user registers with invited email
    second_user = create_user(email=second_email, password="test123")
    second_token = login_user(second_user["email"], second_user["password"])
    
    # Verify second user CAN access org resources (e.g., list members)
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200, "User should have access while member"
    
    # Remove second user's membership
    response = test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 204
    
    # Verify second user CANNOT access org resources anymore. In a single-org
    # install losing your last membership deactivates the account, so the
    # rejection happens at authentication (401) rather than at the org check —
    # the outstanding token stops working rather than merely losing its org.
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 401, "Removed user's token should no longer authenticate"

    # ...and they cannot mint a fresh token either, so the removal is not
    # something a re-login walks around. The refusal names the real reason:
    # their credentials are correct, so "wrong email or password" would send
    # them off to reset a password that was never the problem.
    relogin = test_client.post(
        "/api/auth/jwt/login",
        data={"username": second_email, "password": "test123"},
    )
    assert relogin.status_code == 403, relogin.json()
    assert relogin.json().get("error_code") == "account.disabled"

    # A wrong password on the same account still gets the generic rejection, so
    # the disabled state is only ever disclosed to someone who already proved
    # they hold the credentials.
    bad = test_client.post(
        "/api/auth/jwt/login",
        data={"username": second_email, "password": "not-the-password"},
    )
    assert bad.status_code == 400
    assert bad.json().get("error_code") != "account.disabled"


@pytest.mark.e2e
def test_membership_re_add_after_removal(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Test that a user can be re-added to an organization after removal."""
    # Create admin user
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)['organizations'][0]['id']
    
    # Invite second user by email
    second_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert invite_response.status_code == 200
    membership_id = invite_response.json()["id"]
    
    # Second user registers
    second_user = create_user(email=second_email, password="test123")
    second_token = login_user(second_user["email"], second_user["password"])
    
    # Remove membership
    response = test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 204
    
    # Confirm no access (removal deactivated the account, so it fails at auth)
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 401
    
    # Re-add by email (user already exists, so it will link to existing user)
    response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    
    # Confirm access restored: the invite reactivates the account, so they can
    # sign in again with the credentials they always had.
    second_token = login_user(second_email, "test123")
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200, "User should have access after re-adding membership"


def _invite_and_register(test_client, admin_token, org_id, create_user, email):
    """Invite `email` into `org_id` and register that user. Returns membership_id."""
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert invite.status_code == 200, invite.json()
    create_user(email=email, password="test123")
    return invite.json()["id"]


@pytest.mark.e2e
def test_removal_deactivates_the_account_without_deleting_the_user(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Removing a member closes their login but keeps their user record.

    The account is deactivated, not deleted: the users row is referenced by
    every report, query, file and audit entry they authored (reports.user_id is
    a non-nullable FK), so deleting it would take the organization's content and
    history with it. The observable promise is that the identity survives — a
    later re-invite lands on the *same* user, not a fresh one.
    """
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    hdr = {"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}

    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    membership_id = _invite_and_register(
        test_client, admin_token, org_id, create_user, member_email
    )
    member_token = login_user(member_email, "test123")
    original_user_id = whoami(member_token)["id"]

    assert test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}", headers=hdr
    ).status_code == 204

    # Removal closes the login...
    assert test_client.post(
        "/api/auth/jwt/login",
        data={"username": member_email, "password": "test123"},
    ).status_code != 200

    # ...but the record behind it is still there. Re-invite: the email still
    # resolves to the existing user, so the invite is attached directly rather
    # than left pending for a new registration.
    readd = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=hdr,
    )
    assert readd.status_code == 200, readd.json()
    assert readd.json()["user_id"] == original_user_id, (
        "Re-invite must reattach the original user record, proving removal "
        "deactivated the account rather than deleting it"
    )

    # And the same credentials work again — deactivation is reversible.
    assert whoami(login_user(member_email, "test123"))["id"] == original_user_id


@pytest.mark.e2e
def test_removal_keeps_account_active_when_multiple_orgs_allowed(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """With multiple organizations enabled, holding no membership is normal.

    Deactivation-on-removal is scoped to the single-org deployment shape, where
    "not in this org" and "has no account here" mean the same thing. When a user
    can belong to several organizations, being between them (or holding an
    unaccepted invite elsewhere) is an ordinary state, so the account keeps
    working and only the org access is lost.
    """
    from app.settings.config import settings as bow_settings

    flags = bow_settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        admin_user = create_user()
        admin_token = login_user(admin_user["email"], admin_user["password"])
        org_id = whoami(admin_token)["organizations"][0]["id"]
        hdr = {"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}

        member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
        membership_id = _invite_and_register(
            test_client, admin_token, org_id, create_user, member_email
        )
        member_token = login_user(member_email, "test123")

        assert test_client.delete(
            f"/api/organizations/{org_id}/members/{membership_id}", headers=hdr
        ).status_code == 204

        # Still authenticated (the account is untouched), just not in the org.
        denied = test_client.get(
            f"/api/organizations/{org_id}/members",
            headers={"Authorization": f"Bearer {member_token}", "X-Organization-Id": org_id},
        )
        assert denied.status_code == 403, "Org access is lost, but the account is not"

        # The account itself still works, and now belongs to no organization.
        assert whoami(login_user(member_email, "test123"))["organizations"] == []
    finally:
        flags.allow_multiple_organizations = saved


@pytest.mark.e2e
def test_removal_revokes_the_members_api_keys(
    test_client,
    create_user,
    login_user,
    whoami,
    create_api_key,
    api_key_request,
):
    """A removed member's personal API key stops working.

    An API key outlives the membership row, so revoking access has to reach it
    too — otherwise a departed member keeps a working credential that no admin
    action visibly took away.
    """
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    hdr = {"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}

    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    membership_id = _invite_and_register(
        test_client, admin_token, org_id, create_user, member_email
    )
    member_token = login_user(member_email, "test123")
    api_key = create_api_key(member_token, org_id)["key"]

    # The key works while they are a member.
    assert api_key_request("GET", "/api/users/whoami", api_key, org_id).status_code == 200

    assert test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}", headers=hdr
    ).status_code == 204

    assert api_key_request("GET", "/api/users/whoami", api_key, org_id).status_code == 401, (
        "A removed member's API key must stop authenticating"
    )


@pytest.mark.e2e
def test_cannot_remove_only_admin(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Test that the last admin cannot be removed from an organization."""
    # Create admin user
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    user_info = whoami(admin_token)
    org_id = user_info['organizations'][0]['id']
    
    # Get admin's membership ID
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    members = response.json()
    admin_membership = next((m for m in members if m["role"] == "admin"), None)
    assert admin_membership is not None
    
    # Try to remove the only admin
    response = test_client.delete(
        f"/api/organizations/{org_id}/members/{admin_membership['id']}",
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code in (400, 409)
    detail = response.json().get("detail", "").lower()
    assert "admin" in detail


@pytest.mark.e2e
def test_membership_role_update(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Test updating a member's role."""
    # Create admin user
    admin_user = create_user()
    admin_token = login_user(admin_user["email"], admin_user["password"])
    org_id = whoami(admin_token)['organizations'][0]['id']
    
    # Invite second user
    second_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert invite_response.status_code == 200
    membership_id = invite_response.json()["id"]
    assert invite_response.json()["role"] == "member"
    
    # Second user registers
    create_user(email=second_email, password="test123")
    
    # Update to admin
    response = test_client.put(
        f"/api/organizations/{org_id}/members/{membership_id}",
        json={"role": "admin"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    
    # Update back to member
    response = test_client.put(
        f"/api/organizations/{org_id}/members/{membership_id}",
        json={"role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "member"


# ── Invite token gate + resend ────────────────────────────────────────────

import os as _os
from sqlalchemy import create_engine as _create_engine, text as _text
from tests.fixtures.user import _pending_invite_token


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _set_invite_expiry_past(email):
    """Force a pending invite to look expired (no API for it)."""
    url = _os.environ.get("TEST_DATABASE_URL")
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    eng = _create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                _text("UPDATE memberships SET invite_expires_at = :past WHERE email = :e AND user_id IS NULL"),
                {"past": "2000-01-01 00:00:00", "e": email},
            )
    finally:
        eng.dispose()


def _invite(test_client, admin_token, org_id, email):
    r = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_hdr(admin_token, org_id),
    )
    assert r.status_code == 200, r.json()
    return r.json()


@pytest.mark.e2e
def test_register_blocked_without_token_when_invited(test_client, create_user, login_user, whoami):
    """Closed signups: an invited email can't register without the invite token."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"gate_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, email)

    # Bare register (no token) -> blocked, no user created.
    resp = test_client.post("/api/auth/register", json={"name": "Gate", "email": email, "password": "test123"})
    assert resp.status_code == 400, resp.json()


@pytest.mark.e2e
def test_register_blocked_with_invalid_token(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"gate_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, email)

    resp = test_client.post("/api/auth/register", json={
        "name": "Gate", "email": email, "password": "test123", "invite_token": "not-a-real-token",
    })
    assert resp.status_code == 400, resp.json()


@pytest.mark.e2e
def test_register_blocked_with_expired_token(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"gate_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, email)
    token = _pending_invite_token(email)
    assert token
    _set_invite_expiry_past(email)

    resp = test_client.post("/api/auth/register", json={
        "name": "Gate", "email": email, "password": "test123", "invite_token": token,
    })
    assert resp.status_code == 400, resp.json()
    assert "expired" in str(resp.json().get("detail", "")).lower()


@pytest.mark.e2e
def test_register_succeeds_with_valid_token(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"gate_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, email)
    token = _pending_invite_token(email)

    resp = test_client.post("/api/auth/register", json={
        "name": "Gate", "email": email, "password": "test123", "invite_token": token,
    })
    assert resp.status_code == 201, resp.json()
    # And they are attached to the org.
    info = whoami(login_user(email, "test123"))
    assert org_id in [o["id"] for o in info["organizations"]]


@pytest.mark.e2e
def test_resend_rotates_token_and_invalidates_old(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"gate_{uuid.uuid4().hex[:8]}@test.com"
    m = _invite(test_client, admin_token, org_id, email)
    old_token = _pending_invite_token(email)

    resend = test_client.post(
        f"/api/organizations/{org_id}/members/{m['id']}/resend",
        headers=_hdr(admin_token, org_id),
    )
    assert resend.status_code == 200, resend.json()
    new_token = _pending_invite_token(email)
    assert new_token and new_token != old_token

    # Old link no longer works; new one does.
    bad = test_client.post("/api/auth/register", json={
        "name": "Gate", "email": email, "password": "test123", "invite_token": old_token,
    })
    assert bad.status_code == 400, bad.json()
    ok = test_client.post("/api/auth/register", json={
        "name": "Gate", "email": email, "password": "test123", "invite_token": new_token,
    })
    assert ok.status_code == 201, ok.json()


@pytest.mark.e2e
def test_resend_requires_manage_members(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    # Invite + register a plain member.
    member_email = f"plain_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, member_email)
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    # A second pending invite to attempt resend against.
    target = _invite(test_client, admin_token, org_id, f"t_{uuid.uuid4().hex[:8]}@test.com")

    resp = test_client.post(
        f"/api/organizations/{org_id}/members/{target['id']}/resend",
        headers=_hdr(member_token, org_id),
    )
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_invite_link_endpoint(test_client, create_user, login_user, whoami):
    """Admin can fetch the tokenized invite link for a pending member."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"link_{uuid.uuid4().hex[:8]}@test.com"
    m = _invite(test_client, admin_token, org_id, email)

    resp = test_client.get(
        f"/api/organizations/{org_id}/members/{m['id']}/invite-link",
        headers=_hdr(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["token"] == _pending_invite_token(email)
    assert "token=" in data["url"] and "sign-up" in data["url"]
    # That token actually works for registration.
    ok = test_client.post("/api/auth/register", json={
        "name": "Linked", "email": email, "password": "test123", "invite_token": data["token"],
    })
    assert ok.status_code == 201, ok.json()


@pytest.mark.e2e
def test_invite_link_requires_manage_members(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    member_email = f"plain_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, member_email)
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    target = _invite(test_client, admin_token, org_id, f"t_{uuid.uuid4().hex[:8]}@test.com")
    resp = test_client.get(
        f"/api/organizations/{org_id}/members/{target['id']}/invite-link",
        headers=_hdr(member_token, org_id),
    )
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_invite_link_regenerates_when_expired(test_client, create_user, login_user, whoami):
    """Copy-link on an EXPIRED invite mints a fresh token + resets expiry."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    email = f"exp_{uuid.uuid4().hex[:8]}@test.com"
    m = _invite(test_client, admin_token, org_id, email)
    old_token = _pending_invite_token(email)
    _set_invite_expiry_past(email)

    resp = test_client.get(
        f"/api/organizations/{org_id}/members/{m['id']}/invite-link",
        headers=_hdr(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["regenerated"] is True
    new_token = _pending_invite_token(email)
    assert new_token and new_token != old_token and data["token"] == new_token
    # Old link is dead, the freshly-copied one works.
    assert test_client.post("/api/auth/register", json={
        "name": "Exp", "email": email, "password": "test123", "invite_token": old_token,
    }).status_code == 400
    assert test_client.post("/api/auth/register", json={
        "name": "Exp", "email": email, "password": "test123", "invite_token": new_token,
    }).status_code == 201


@pytest.mark.e2e
def test_second_live_membership_for_same_user_and_org_is_rejected(
    create_user,
    login_user,
    whoami,
):
    """The database enforces one live membership per (user, organization).

    This is the backstop for the duplicate that broke principal_belongs_to_org
    (two live rows -> MultipleResultsFound -> org-scoped routes 500, i.e. the
    "no available models" the customer hit). Open invites (user_id NULL) are
    intentionally NOT constrained, so this only covers user-bound rows.
    """
    import asyncio
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.dependencies import async_session_maker
    from app.models.membership import Membership

    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    user_id = info["id"]

    async def _attempt_duplicate():
        async with async_session_maker() as db:
            # The admin already holds exactly one live membership (created with
            # the org). A second live row for the same (user, org) must be refused.
            existing = (await db.execute(
                select(Membership).where(
                    Membership.organization_id == org_id,
                    Membership.user_id == user_id,
                    Membership.deleted_at.is_(None),
                )
            )).scalars().all()
            assert len(existing) == 1
            db.add(Membership(user_id=user_id, organization_id=org_id, role="member"))
            with pytest.raises(IntegrityError):
                await db.flush()

    asyncio.run(_attempt_duplicate())


@pytest.mark.e2e
def test_duplicate_membership_does_not_block_querying_models(
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Reproduction of the customer bug ("no available models", cannot query).

    A user with two live membership rows for one org made
    principal_belongs_to_org raise MultipleResultsFound, so
    get_current_organization 500'd and GET /api/llm/models (the model picker's
    source) failed -> the UI showed no models. This asserts the promised
    end-state: the duplicate can no longer be created AND the model endpoint
    stays 200 for the member.

    Against the PRE-FIX code this test fails: the second row inserts and
    GET /api/llm/models returns 500.
    """
    import asyncio
    from sqlalchemy.exc import IntegrityError
    from app.dependencies import async_session_maker
    from app.models.membership import Membership

    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    user_id = info["id"]

    async def _try_make_duplicate():
        async with async_session_maker() as db:
            db.add(Membership(user_id=user_id, organization_id=org_id, role="member"))
            try:
                await db.commit()
                return True   # pre-fix: a duplicate live membership was created
            except IntegrityError:
                await db.rollback()
                return False  # fixed: barred by the partial unique index

    duplicate_created = asyncio.run(_try_make_duplicate())

    # The exact endpoint behind "no available models" must never 500 for a member.
    resp = test_client.get(
        "/api/llm/models",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, f"model list returned {resp.status_code}: {resp.text}"

    # The duplicate state must be unreachable on the fixed code.
    assert duplicate_created is False, "a second live membership should be rejected by the unique index"


@pytest.mark.e2e
def test_attach_dedups_multiple_open_invites_to_one_membership(
    create_user,
    login_user,
    whoami,
    test_client,
):
    """When more than one open invite exists for a user in one org, accepting them
    yields exactly ONE live membership: _attach_open_memberships claims one and
    retires (hard-deletes) the rest onto it, instead of creating duplicate rows.
    """
    import asyncio
    import uuid as _uuid
    from sqlalchemy import select, func
    from app.dependencies import async_session_maker
    from app.models.membership import Membership

    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    email = f"invitee_{_uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert r.status_code == 200, r.json()

    # Seed a SECOND open invite for the same email+org directly (the API guard
    # blocks a duplicate invite; this models a pre-existing duplicate / race),
    # with a different role so the retire path exercises role preservation.
    async def _seed_second_invite():
        async with async_session_maker() as db:
            db.add(Membership(email=email, organization_id=org_id, role="admin"))
            await db.commit()

    asyncio.run(_seed_second_invite())

    # The invitee registers → _attach claims one invite and retires the other.
    create_user(email=email, password="test123")
    invitee_id = whoami(login_user(email, "test123"))["id"]

    async def _check():
        async with async_session_maker() as db:
            rows = (await db.execute(
                select(Membership).where(
                    Membership.organization_id == org_id,
                    Membership.user_id == invitee_id,
                    Membership.deleted_at.is_(None),
                )
            )).scalars().all()
            assert len(rows) == 1, f"expected one membership, got {len(rows)}"
            # Retired invite is hard-deleted and the claimed one cleared its email,
            # so no email-keyed rows linger for this address in the org.
            leftovers = (await db.execute(
                select(func.count(Membership.id)).where(
                    Membership.organization_id == org_id,
                    func.lower(Membership.email) == email.lower(),
                )
            )).scalar()
            assert leftovers == 0

    asyncio.run(_check())


@pytest.mark.e2e
def test_insert_membership_if_absent_survives_race_and_preserves_pending_writes(
    create_user,
    login_user,
    whoami,
):
    """A lost insert race must not poison the outer transaction.

    ``insert_membership_if_absent`` guards the insert with a SAVEPOINT. The guard
    only works if the conflicting row is added strictly INSIDE the savepoint:
    ``begin_nested()`` pre-flushes the whole session on open, so an ``add`` placed
    before the savepoint would write in the OUTER transaction and a lost race would
    roll the whole thing back (and the handler would then fire on a dead session).

    This drives an actual race: an unrelated pending write is staged, then an
    insert that loses to an existing live row. The call must return the existing
    row, and the unrelated write must still commit — proving the outer transaction
    stayed alive. Against the pre-flush bug this raises instead.
    """
    import asyncio
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.membership import Membership
    from app.models.user import User
    from app.core.permission_resolver import insert_membership_if_absent

    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    user_id = info["id"]

    async def _run():
        async with async_session_maker() as db:
            # Unrelated pending write in the SAME transaction.
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            user.name = "RenamedDuringRace"
            # The admin already holds a live membership → this insert loses.
            row, created = await insert_membership_if_absent(
                db, Membership(user_id=user_id, organization_id=org_id, role="member")
            )
            assert created is False
            assert str(row.user_id) == str(user_id)
            await db.commit()  # must not raise — outer transaction still alive

        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            assert user.name == "RenamedDuringRace"  # unrelated write survived
            rows = (await db.execute(select(Membership).where(
                Membership.user_id == user_id,
                Membership.organization_id == org_id,
                Membership.deleted_at.is_(None),
            ))).scalars().all()
            assert len(rows) == 1

    asyncio.run(_run())


@pytest.mark.e2e
def test_claim_pending_invite_survives_race_and_preserves_pending_writes(
    create_user,
    login_user,
    whoami,
):
    """Same SAVEPOINT guarantee for ``claim_pending_invite``.

    When an open invite exists but the user already has a live membership in the
    org (SCIM/OIDC won the race), claiming must stamp the invite INSIDE the
    savepoint, lose cleanly (return None), and leave the outer transaction usable.
    """
    import asyncio
    import uuid as _uuid
    from sqlalchemy import select
    from fastapi_users.password import PasswordHelper
    from app.dependencies import async_session_maker
    from app.models.membership import Membership
    from app.models.user import User
    from app.core.permission_resolver import claim_pending_invite

    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Seed a separate user directly (this config disables uninvited signup), who
    # already holds a live membership in the org, plus a still-open invite for the
    # same email — the exact "SSO won the race, invite left open" state.
    email = f"racer_{_uuid.uuid4().hex[:8]}@test.com"
    ph = PasswordHelper()

    async def _seed():
        async with async_session_maker() as db:
            u = User(
                email=email, name="Racer", hashed_password=ph.hash(ph.generate()),
                is_active=True, is_verified=True, is_superuser=False,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            db.add(Membership(user_id=u.id, organization_id=org_id, role="member"))
            db.add(Membership(email=email, organization_id=org_id, role="admin"))
            await db.commit()
            return u.id

    member_id = asyncio.run(_seed())

    async def _run():
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.id == member_id))).scalar_one()
            user.name = "ClaimRaceRename"  # unrelated pending write
            claimed = await claim_pending_invite(db, user, org_id)
            assert claimed is None  # lost the race, cleanly
            await db.commit()  # must not raise

        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.id == member_id))).scalar_one()
            assert user.name == "ClaimRaceRename"
            rows = (await db.execute(select(Membership).where(
                Membership.user_id == member_id,
                Membership.organization_id == org_id,
                Membership.deleted_at.is_(None),
            ))).scalars().all()
            assert len(rows) == 1  # still exactly one live row

    asyncio.run(_run())
