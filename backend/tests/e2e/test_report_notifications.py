import pytest


@pytest.mark.e2e
def test_schedule_report_with_subscribers(
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    schedule_report,
    get_report,
):
    """Schedule a published report with notification subscribers and verify they persist."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    user_id = whoami(user_token)["id"]

    report = create_report(title="Scheduled Notify Test", user_token=user_token, org_id=org_id, data_sources=[])
    publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)

    subscribers = [
        {"type": "user", "id": user_id},
        {"type": "email", "address": "external@example.com"},
    ]

    result = schedule_report(
        report_id=report["id"],
        cron_expression="0 * * * *",
        user_token=user_token,
        org_id=org_id,
        notification_subscribers=subscribers,
    )

    assert result["cron_schedule"] == "0 * * * *"
    assert result["notification_subscribers"] is not None
    assert len(result["notification_subscribers"]) == 2
    assert result["notification_subscribers"][0]["type"] == "user"
    assert result["notification_subscribers"][0]["id"] == user_id
    assert result["notification_subscribers"][1]["type"] == "email"
    assert result["notification_subscribers"][1]["address"] == "external@example.com"

    # Verify persistence via GET
    fetched = get_report(report["id"], user_token=user_token, org_id=org_id)
    assert fetched["notification_subscribers"] is not None
    assert len(fetched["notification_subscribers"]) == 2


@pytest.mark.e2e
def test_schedule_report_clears_subscribers_on_unschedule(
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    schedule_report,
    get_report,
):
    """Unscheduling a report should clear notification subscribers."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Unsched Notify Test", user_token=user_token, org_id=org_id, data_sources=[])
    publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)

    # Schedule with subscribers
    schedule_report(
        report_id=report["id"],
        cron_expression="0 * * * *",
        user_token=user_token,
        org_id=org_id,
        notification_subscribers=[{"type": "email", "address": "test@example.com"}],
    )

    # Unschedule
    result = schedule_report(
        report_id=report["id"],
        cron_expression="None",
        user_token=user_token,
        org_id=org_id,
    )

    assert result["cron_schedule"] is None
    assert result["notification_subscribers"] is None

    fetched = get_report(report["id"], user_token=user_token, org_id=org_id)
    assert fetched["notification_subscribers"] is None


@pytest.mark.e2e
def test_schedule_report_without_subscribers_backwards_compat(
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
    schedule_report,
):
    """Schedule without subscribers (backwards compat) should still work."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="No Notify Test", user_token=user_token, org_id=org_id, data_sources=[])
    publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)

    result = schedule_report(
        report_id=report["id"],
        cron_expression="0 0 * * *",
        user_token=user_token,
        org_id=org_id,
    )

    assert result["cron_schedule"] == "0 0 * * *"


@pytest.mark.e2e
def test_schedule_report_invalid_subscriber_type(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
):
    """Invalid subscriber type should return 422."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Bad Sub Test", user_token=user_token, org_id=org_id, data_sources=[])
    publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    response = test_client.post(
        f"/api/reports/{report['id']}/schedule",
        json={
            "cron_expression": "0 * * * *",
            "notification_subscribers": [{"type": "slack", "address": "#channel"}],
        },
        headers=headers,
    )

    assert response.status_code == 422


@pytest.mark.e2e
def test_notify_share_dashboard(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
):
    """Send a share_dashboard notification via the notify endpoint."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Share Notify Test", user_token=user_token, org_id=org_id, data_sources=[])
    publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    response = test_client.post(
        f"/api/reports/{report['id']}/notify",
        json={
            "type": "share_dashboard",
            "channels": ["email"],
            "recipients": [user["email"]],
            "share_url": f"http://localhost:3000/r/{report['id']}",
        },
        headers=headers,
    )

    # Will succeed if SMTP is configured, otherwise 400
    if response.status_code == 200:
        data = response.json()
        assert "dispatched" in data
    else:
        assert response.status_code == 400
        assert "SMTP" in response.json().get("detail", "")


@pytest.mark.e2e
def test_notify_schedule_requires_published(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
):
    """schedule_report notification type should fail on draft reports."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="Draft Notify Test", user_token=user_token, org_id=org_id, data_sources=[])

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    response = test_client.post(
        f"/api/reports/{report['id']}/notify",
        json={
            "type": "schedule_report",
            "channels": ["email"],
            "recipients": [user["email"]],
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert "published" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_notify_share_requires_url(
    test_client,
    create_report,
    create_user,
    login_user,
    whoami,
    publish_report,
):
    """share_dashboard notification without share_url should fail."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    report = create_report(title="No URL Test", user_token=user_token, org_id=org_id, data_sources=[])
    publish_report(report_id=report["id"], user_token=user_token, org_id=org_id)

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    response = test_client.post(
        f"/api/reports/{report['id']}/notify",
        json={
            "type": "share_dashboard",
            "channels": ["email"],
            "recipients": [user["email"]],
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert "share_url" in response.json()["detail"].lower()
