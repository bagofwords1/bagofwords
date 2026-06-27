"""E2E coverage for: data sources are private by default, and adding a member
schedules a delayed "you've been added" notification.

Notify-first: the delayed job delivers a durable in-app notification regardless
of SMTP, and additionally sends an email when SMTP is configured. So the job is
scheduled whenever a genuinely new member is added; the SMTP setting only gates
the email channel at send time, not the scheduling decision.

Exercises the real HTTP routes end to end. The delayed send itself is not driven
here (it fires minutes later via APScheduler); we assert the scheduling decision.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app.core.scheduler as scheduler_mod
from app.settings.config import settings as bow_settings


pytestmark = pytest.mark.e2e

CHINOOK_PATH = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _add_member(test_client, admin, ds, member):
    return test_client.post(
        f"/api/data_sources/{ds['id']}/members",
        json={"principal_type": "user", "principal_id": member["user_id"]},
        headers=_headers(admin["token"], admin["org_id"]),
    )


@pytest.fixture
def restore_email_client():
    """Snapshot/restore the global email client so SMTP tweaks don't leak."""
    saved = bow_settings.email_client
    try:
        yield
    finally:
        bow_settings.email_client = saved


def test_data_source_is_private_by_default(bootstrap_admin, create_data_source):
    """Creating a data source without is_public yields a private one."""
    if not CHINOOK_PATH.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK_PATH}")

    admin = bootstrap_admin("priv_default")
    ds = create_data_source(
        name=f"ds_{uuid.uuid4().hex[:8]}",
        type="sqlite",
        config={"database": str(CHINOOK_PATH)},
        credentials={},
        user_token=admin["token"],
        org_id=admin["org_id"],
    )
    assert ds["is_public"] is False, ds


def test_member_add_schedules_delayed_email_when_smtp_configured(
    monkeypatch, restore_email_client, test_client, bootstrap_admin,
    invite_user_to_org, sqlite_data_source,
):
    admin = bootstrap_admin("ds_email")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    ds = sqlite_data_source(
        name=f"ds_{uuid.uuid4().hex[:8]}",
        user_token=admin["token"],
        org_id=admin["org_id"],
        is_public=False,
    )

    # Spy on the scheduler the email helper uses, and pretend SMTP is configured.
    add_job = MagicMock()
    monkeypatch.setattr(scheduler_mod.scheduler, "add_job", add_job)
    bow_settings.email_client = MagicMock()  # truthy => "SMTP configured"

    resp = _add_member(test_client, admin, ds, member)
    assert resp.status_code == 200, resp.json()

    add_job.assert_called_once()
    kwargs = add_job.call_args.kwargs
    assert kwargs["trigger"] == "date"  # delayed, not immediate
    assert kwargs["args"][0] == ds["id"]
    assert kwargs["args"][1] == member["user_id"]


def test_update_members_emails_only_newly_added(
    monkeypatch, restore_email_client, test_client, bootstrap_admin,
    invite_user_to_org, sqlite_data_source,
):
    """The Members UI saves the full list via PUT; only genuinely new members
    should be notified, never the ones already on the data source."""
    admin = bootstrap_admin("ds_update")
    m1 = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    m2 = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    ds = sqlite_data_source(
        name=f"ds_{uuid.uuid4().hex[:8]}",
        user_token=admin["token"],
        org_id=admin["org_id"],
        is_public=False,
    )

    add_job = MagicMock()
    monkeypatch.setattr(scheduler_mod.scheduler, "add_job", add_job)
    bow_settings.email_client = MagicMock()

    def _put_members(ids):
        return test_client.put(
            f"/api/data_sources/{ds['id']}",
            json={"member_user_ids": ids},
            headers=_headers(admin["token"], admin["org_id"]),
        )

    # First save adds m1 -> exactly one email, for m1.
    r1 = _put_members([m1["user_id"]])
    assert r1.status_code == 200, r1.json()
    add_job.assert_called_once()
    assert add_job.call_args.kwargs["args"][1] == m1["user_id"]

    # Re-saving with m1 + m2 must notify only m2 (m1 already a member).
    add_job.reset_mock()
    r2 = _put_members([m1["user_id"], m2["user_id"]])
    assert r2.status_code == 200, r2.json()
    add_job.assert_called_once()
    assert add_job.call_args.kwargs["args"][1] == m2["user_id"]


def test_member_add_schedules_notification_without_smtp(
    monkeypatch, restore_email_client, test_client, bootstrap_admin,
    invite_user_to_org, sqlite_data_source,
):
    """Notify-first: even without SMTP, adding a member schedules the delayed job
    because it delivers the in-app "added to agent" notification. (The email
    channel is skipped at send time when SMTP is absent.)"""
    admin = bootstrap_admin("ds_nosmtp")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    ds = sqlite_data_source(
        name=f"ds_{uuid.uuid4().hex[:8]}",
        user_token=admin["token"],
        org_id=admin["org_id"],
        is_public=False,
    )

    add_job = MagicMock()
    monkeypatch.setattr(scheduler_mod.scheduler, "add_job", add_job)
    bow_settings.email_client = None  # SMTP not configured

    resp = _add_member(test_client, admin, ds, member)
    assert resp.status_code == 200, resp.json()
    add_job.assert_called_once()
    kwargs = add_job.call_args.kwargs
    assert kwargs["trigger"] == "date"
    assert kwargs["args"][0] == ds["id"]
    assert kwargs["args"][1] == member["user_id"]
