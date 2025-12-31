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
    assert response.status_code == 400
    assert "only admin" in response.json().get("detail", "").lower()


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
