"""
Fire-time guard for scheduled artifact refreshes vs. archived reports.

Archiving a report hides the conversation but does NOT unpublish its artifact:
a shared dashboard keeps being served from /r/{id}. The scheduled refresh
(report.cron_schedule → scheduled_rerun_report_steps) must therefore:

- KEEP RUNNING while the artifact is still visible (public/internal/shared),
  even if the report is archived — viewers still consume the dashboard.
- SKIP and SELF-UNSCHEDULE when the report is archived AND the artifact is
  not visible to anyone — refreshing into the void, with no UI left to stop it.
"""
import asyncio
from unittest.mock import patch

import pytest

from app.services.report_service import ReportService


def _fire(report_id, user_id, org_id):
    """Invoke the scheduled entrypoint exactly as APScheduler would, with the
    multi-worker claim forced won (the claim is infrastructure, not behavior)."""
    with patch("app.services.report_service.claim_scheduled_run", return_value=True):
        asyncio.run(
            ReportService().scheduled_rerun_report_steps(report_id, user_id, org_id)
        )


def _get_report(test_client, report_id, token, org_id):
    resp = test_client.get(
        f"/api/reports/{report_id}",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


@pytest.mark.e2e
def test_refresh_skips_and_unschedules_archived_invisible_report(
    test_client, create_report, schedule_report, delete_report,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]

    report = create_report(title="Hidden refresh", user_token=token, org_id=org_id, data_sources=[])
    schedule_report(report["id"], "0 9 * * *", user_token=token, org_id=org_id)
    delete_report(report["id"], user_token=token, org_id=org_id)  # archive; visibility stays 'none'

    _fire(report["id"], me["id"], org_id)

    after = _get_report(test_client, report["id"], token, org_id)
    assert after["status"] == "archived"
    # Guard fired: no refresh ran, and the schedule cleared itself.
    assert after["last_run_at"] is None
    assert after["cron_schedule"] is None


@pytest.mark.e2e
def test_refresh_keeps_running_for_archived_report_with_visible_artifact(
    test_client, create_report, schedule_report, delete_report, set_visibility,
    create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]

    report = create_report(title="Live dashboard", user_token=token, org_id=org_id, data_sources=[])
    set_visibility(report["id"], "artifact", "internal", user_token=token, org_id=org_id)
    schedule_report(report["id"], "0 9 * * *", user_token=token, org_id=org_id)
    delete_report(report["id"], user_token=token, org_id=org_id)  # archive; artifact stays internal

    _fire(report["id"], me["id"], org_id)

    after = _get_report(test_client, report["id"], token, org_id)
    assert after["status"] == "archived"
    # The dashboard is still served, so the refresh ran and the schedule survives.
    assert after["last_run_at"] is not None
    assert after["cron_schedule"] == "0 9 * * *"


@pytest.mark.e2e
def test_refresh_runs_normally_for_active_report(
    test_client, create_report, schedule_report,
    create_user, login_user, whoami,
):
    """Baseline: the guard must not interfere with a plain scheduled report."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]

    report = create_report(title="Active", user_token=token, org_id=org_id, data_sources=[])
    schedule_report(report["id"], "0 9 * * *", user_token=token, org_id=org_id)

    _fire(report["id"], me["id"], org_id)

    after = _get_report(test_client, report["id"], token, org_id)
    assert after["last_run_at"] is not None
    assert after["cron_schedule"] == "0 9 * * *"
