import uuid
import pytest


def _setup_owner_and_member(create_user, login_user, whoami, test_client):
    """Create two users in the same org: owner + member. Returns (owner_token, member_token, org_id)."""
    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    owner_info = whoami(owner_token)
    org_id = owner_info["organizations"][0]["id"]

    # Invite member by email first (registration may be restricted)
    member_email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    invite_resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {owner_token}", "X-Organization-Id": org_id},
    )
    assert invite_resp.status_code == 200, invite_resp.json()

    member = create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")

    return owner_token, member_token, org_id


@pytest.mark.e2e
def test_create_scheduled_prompt(
    create_scheduled_prompt,
    create_report,
    create_user,
    login_user,
    whoami,
):
    """Test creating a scheduled prompt for a report."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="SP Test Report", user_token=user_token, org_id=org_id)

    sp = create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Check for anomalies"},
        cron_schedule="0 8 * * *",
        user_token=user_token,
        org_id=org_id,
    )

    assert sp["report_id"] == report["id"]
    assert sp["prompt"]["content"] == "Check for anomalies"
    assert sp["cron_schedule"] == "0 8 * * *"
    assert sp["is_active"] is True
    assert sp["id"] is not None


@pytest.mark.e2e
def test_list_scheduled_prompts(
    create_scheduled_prompt,
    list_scheduled_prompts,
    create_report,
    create_user,
    login_user,
    whoami,
):
    """Test listing scheduled prompts for a report."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="SP List Report", user_token=user_token, org_id=org_id)

    create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Prompt A"},
        cron_schedule="0 8 * * *",
        user_token=user_token,
        org_id=org_id,
    )
    create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Prompt B"},
        cron_schedule="0 12 * * *",
        user_token=user_token,
        org_id=org_id,
    )

    prompts = list_scheduled_prompts(report_id=report["id"], user_token=user_token, org_id=org_id)
    assert len(prompts) == 2
    contents = {p["prompt"]["content"] for p in prompts}
    assert contents == {"Prompt A", "Prompt B"}


@pytest.mark.e2e
def test_update_scheduled_prompt(
    create_scheduled_prompt,
    update_scheduled_prompt,
    list_scheduled_prompts,
    create_report,
    create_user,
    login_user,
    whoami,
):
    """Test updating a scheduled prompt's cron, prompt, and active state."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="SP Update Report", user_token=user_token, org_id=org_id)

    sp = create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Original prompt"},
        cron_schedule="0 8 * * *",
        user_token=user_token,
        org_id=org_id,
    )

    updated = update_scheduled_prompt(
        report_id=report["id"],
        sp_id=sp["id"],
        prompt={"content": "Updated prompt"},
        cron_schedule="0 12 * * 1",
        is_active=False,
        user_token=user_token,
        org_id=org_id,
    )

    assert updated["prompt"]["content"] == "Updated prompt"
    assert updated["cron_schedule"] == "0 12 * * 1"
    assert updated["is_active"] is False

    # Verify via list
    prompts = list_scheduled_prompts(report_id=report["id"], user_token=user_token, org_id=org_id)
    assert len(prompts) == 1
    assert prompts[0]["is_active"] is False


@pytest.mark.e2e
def test_delete_scheduled_prompt(
    create_scheduled_prompt,
    delete_scheduled_prompt,
    list_scheduled_prompts,
    create_report,
    create_user,
    login_user,
    whoami,
):
    """Test soft-deleting a scheduled prompt removes it from the list."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="SP Delete Report", user_token=user_token, org_id=org_id)

    sp = create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "To be deleted"},
        cron_schedule="0 8 * * *",
        user_token=user_token,
        org_id=org_id,
    )

    delete_scheduled_prompt(
        report_id=report["id"],
        sp_id=sp["id"],
        user_token=user_token,
        org_id=org_id,
    )

    prompts = list_scheduled_prompts(report_id=report["id"], user_token=user_token, org_id=org_id)
    assert len(prompts) == 0


@pytest.mark.e2e
def test_create_scheduled_prompt_with_subscribers(
    create_scheduled_prompt,
    create_report,
    create_user,
    login_user,
    whoami,
):
    """Test creating a scheduled prompt with notification subscribers."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    user_info = whoami(user_token)
    org_id = user_info["organizations"][0]["id"]

    report = create_report(title="SP Subscribers Report", user_token=user_token, org_id=org_id)

    sp = create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Check data"},
        cron_schedule="0 8 * * *",
        notification_subscribers=[
            {"type": "email", "address": "test@example.com"},
            {"type": "user", "id": user_info["id"]},
        ],
        user_token=user_token,
        org_id=org_id,
    )

    assert sp["notification_subscribers"] is not None
    assert len(sp["notification_subscribers"]) == 2


@pytest.mark.e2e
def test_create_scheduled_prompt_invalid_cron(
    create_report,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that an invalid cron expression returns 400."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="SP Invalid Cron", user_token=user_token, org_id=org_id)

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }
    response = test_client.post(
        f"/api/reports/{report['id']}/scheduled-prompts",
        json={"prompt": {"content": "test"}, "cron_schedule": "bad cron"},
        headers=headers,
    )
    assert response.status_code == 400


# ============================================================================
# OWNER-ONLY PERMISSION TESTS
# ============================================================================


@pytest.mark.e2e
def test_non_owner_cannot_create_scheduled_prompt(
    create_report,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that a non-owner member cannot create a scheduled prompt on someone else's report."""
    owner_token, member_token, org_id = _setup_owner_and_member(create_user, login_user, whoami, test_client)

    report = create_report(title="Owner's Report", user_token=owner_token, org_id=org_id)

    headers = {
        "Authorization": f"Bearer {member_token}",
        "X-Organization-Id": str(org_id),
    }
    response = test_client.post(
        f"/api/reports/{report['id']}/scheduled-prompts",
        json={"prompt": {"content": "sneaky"}, "cron_schedule": "0 8 * * *"},
        headers=headers,
    )
    assert response.status_code == 403


@pytest.mark.e2e
def test_non_owner_cannot_update_scheduled_prompt(
    create_scheduled_prompt,
    create_report,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that a non-owner cannot update a scheduled prompt on someone else's report."""
    owner_token, member_token, org_id = _setup_owner_and_member(create_user, login_user, whoami, test_client)

    report = create_report(title="Owner's Report", user_token=owner_token, org_id=org_id)
    sp = create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Original"},
        cron_schedule="0 8 * * *",
        user_token=owner_token,
        org_id=org_id,
    )

    headers = {
        "Authorization": f"Bearer {member_token}",
        "X-Organization-Id": str(org_id),
    }
    response = test_client.put(
        f"/api/reports/{report['id']}/scheduled-prompts/{sp['id']}",
        json={"is_active": False},
        headers=headers,
    )
    assert response.status_code == 403


@pytest.mark.e2e
def test_non_owner_cannot_delete_scheduled_prompt(
    create_scheduled_prompt,
    create_report,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that a non-owner cannot delete a scheduled prompt on someone else's report."""
    owner_token, member_token, org_id = _setup_owner_and_member(create_user, login_user, whoami, test_client)

    report = create_report(title="Owner's Report", user_token=owner_token, org_id=org_id)
    sp = create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Do not delete me"},
        cron_schedule="0 8 * * *",
        user_token=owner_token,
        org_id=org_id,
    )

    headers = {
        "Authorization": f"Bearer {member_token}",
        "X-Organization-Id": str(org_id),
    }
    response = test_client.delete(
        f"/api/reports/{report['id']}/scheduled-prompts/{sp['id']}",
        headers=headers,
    )
    assert response.status_code == 403


@pytest.mark.e2e
def test_non_owner_cannot_list_scheduled_prompts(
    create_scheduled_prompt,
    create_report,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that a non-owner cannot list scheduled prompts on someone else's report."""
    owner_token, member_token, org_id = _setup_owner_and_member(create_user, login_user, whoami, test_client)

    report = create_report(title="Owner's Report", user_token=owner_token, org_id=org_id)
    create_scheduled_prompt(
        report_id=report["id"],
        prompt={"content": "Secret prompt"},
        cron_schedule="0 8 * * *",
        user_token=owner_token,
        org_id=org_id,
    )

    headers = {
        "Authorization": f"Bearer {member_token}",
        "X-Organization-Id": str(org_id),
    }
    response = test_client.get(
        f"/api/reports/{report['id']}/scheduled-prompts",
        headers=headers,
    )
    assert response.status_code == 403
