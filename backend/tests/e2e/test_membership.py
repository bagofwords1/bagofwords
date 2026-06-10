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
    
    # Verify second user CANNOT access org resources anymore (should get 403)
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 403, "User should be denied access after membership removal"
    assert "not a member" in response.json().get("detail", "").lower()


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
    
    # Confirm no access
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 403
    
    # Re-add by email (user already exists, so it will link to existing user)
    response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": second_email, "role": "member"},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200
    
    # Confirm access restored
    response = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers={"Authorization": f"Bearer {second_token}", "X-Organization-Id": org_id}
    )
    assert response.status_code == 200, "User should have access after re-adding membership"


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
