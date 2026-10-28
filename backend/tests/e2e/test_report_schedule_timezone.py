"""
A dashboard refresh fires at the chosen wall-clock time in the org's timezone.

The org timezone (Settings → General) governs wall-clock schedules. Scheduled
prompts honoured it; dashboard refreshes (report.cron_schedule) were registered
without a timezone, so APScheduler used the server clock — on a UTC server an
Israeli org's "16:00" fired at 19:00 local. The invariants:

- with an org timezone, a refresh saved for HH:MM fires at HH:MM in that zone;
- without one, it keeps the server clock (unchanged behaviour);
- refresh jobs registered on another clock (saved before the fix, or before the
  org changed its timezone) move to the org's zone at startup, and a job that is
  already right is left alone.
"""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _setup(create_user, login_user, whoami, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Refresh tz", user_token=token, org_id=org_id, data_sources=[])
    return token, org_id, report["id"]


def _set_org_timezone(test_client, token, org_id, tz):
    resp = test_client.put("/api/organization/timezone", json={"timezone": tz}, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text


def _job(report_id):
    from app.core.scheduler import scheduler
    job = scheduler.get_job(f"report_{report_id}")
    assert job is not None, "saving a schedule must register a refresh job"
    return job


def _next_fire(report_id):
    return _job(report_id).trigger.get_next_fire_time(None, datetime.now(timezone.utc))


def _assert_fires_at_wall_clock(report_id, tz, hour, minute):
    fire = _next_fire(report_id).astimezone(ZoneInfo(tz))
    assert (fire.hour, fire.minute) == (hour, minute), f"fires at {fire.isoformat()}, expected {hour:02d}:{minute:02d} {tz}"


def _register_schedule_timezones():
    from app.services.report_service import ReportService
    asyncio.run(ReportService().register_schedule_timezones())


@pytest.mark.e2e
@pytest.mark.parametrize("tz,hour,minute", [
    ("Asia/Jerusalem", 16, 0),
    ("Asia/Tel_Aviv", 8, 30),
    ("America/New_York", 23, 15),
    ("Asia/Tokyo", 0, 45),
])
def test_refresh_fires_at_wall_clock_in_org_timezone(
    tz, hour, minute, test_client, create_user, login_user, whoami, create_report, schedule_report,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    _set_org_timezone(test_client, token, org_id, tz)

    schedule_report(report_id, f"{minute} {hour} * * *", user_token=token, org_id=org_id)

    _assert_fires_at_wall_clock(report_id, tz, hour, minute)


@pytest.mark.e2e
def test_refresh_without_org_timezone_keeps_server_clock(
    create_user, login_user, whoami, create_report, schedule_report,
):
    from app.core.scheduler import scheduler
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)

    schedule_report(report_id, "0 9 * * 1-5", user_token=token, org_id=org_id)

    assert str(_job(report_id).trigger.timezone) == str(scheduler.timezone)
    _assert_fires_at_wall_clock(report_id, str(scheduler.timezone), 9, 0)


@pytest.mark.e2e
def test_startup_moves_refresh_jobs_onto_org_timezone(
    test_client, create_user, login_user, whoami, create_report, schedule_report,
):
    from app.core.scheduler import scheduler
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    # Pick a zone that differs from the server clock, so "moved" is observable.
    tz = "Asia/Tokyo" if str(scheduler.timezone) != "Asia/Tokyo" else "America/New_York"

    # Saved while the org had no timezone -> registered on the server clock.
    schedule_report(report_id, "0 16 * * *", user_token=token, org_id=org_id)
    assert str(_job(report_id).trigger.timezone) == str(scheduler.timezone)

    # The org sets its timezone later; the job is not re-saved by anyone.
    _set_org_timezone(test_client, token, org_id, tz)
    _register_schedule_timezones()

    _assert_fires_at_wall_clock(report_id, tz, 16, 0)
    # The schedule itself and the job's arguments (who it runs as) survive.
    job = _job(report_id)
    assert job.args[0] == report_id
    resp = test_client.get(f"/api/reports/{report_id}", headers=_headers(token, org_id))
    assert resp.json()["cron_schedule"] == "0 16 * * *"


@pytest.mark.e2e
def test_startup_keeps_correct_refresh_jobs_on_org_timezone(
    test_client, create_user, login_user, whoami, create_report, schedule_report,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    _set_org_timezone(test_client, token, org_id, "Asia/Jerusalem")
    schedule_report(report_id, "0 16 * * *", user_token=token, org_id=org_id)

    # Every restart runs the pass again; it must be a no-op for a correct job.
    _register_schedule_timezones()
    _register_schedule_timezones()

    assert str(_job(report_id).trigger.timezone) == "Asia/Jerusalem"
    _assert_fires_at_wall_clock(report_id, "Asia/Jerusalem", 16, 0)


@pytest.mark.e2e
def test_startup_leaves_jobs_alone_when_schedules_cannot_be_read(
    test_client, create_user, login_user, whoami, create_report, schedule_report,
):
    """A failed read is not "no org timezone": nothing may move to the server clock."""
    from unittest.mock import patch
    from sqlalchemy.exc import OperationalError
    from app.core.scheduler import scheduler
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    tz = "Asia/Tokyo" if str(scheduler.timezone) != "Asia/Tokyo" else "America/New_York"
    _set_org_timezone(test_client, token, org_id, tz)
    schedule_report(report_id, "0 16 * * *", user_token=token, org_id=org_id)

    def _db_down():
        raise OperationalError("SELECT", {}, Exception("database is unavailable"))

    # Fault injection at the DB boundary: the startup pass's session cannot connect.
    with patch("app.dependencies.async_session_maker", side_effect=_db_down):
        _register_schedule_timezones()  # must not raise either

    assert str(_job(report_id).trigger.timezone) == tz
    _assert_fires_at_wall_clock(report_id, tz, 16, 0)


@pytest.mark.e2e
def test_invalid_stored_org_timezone_falls_back_to_server_clock(
    test_client, create_user, login_user, whoami, create_report, schedule_report,
):
    from app.core.scheduler import scheduler
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    _set_org_timezone(test_client, token, org_id, "Asia/Jerusalem")
    # The API rejects unknown zones, so a bad value can only come from an older
    # release or a manual edit — write it directly to reproduce that state.
    _store_raw_org_timezone(org_id, "Mars/Olympus_Mons")

    schedule_report(report_id, "15 7 * * *", user_token=token, org_id=org_id)

    assert str(_job(report_id).trigger.timezone) == str(scheduler.timezone)
    _assert_fires_at_wall_clock(report_id, str(scheduler.timezone), 7, 15)


def _store_raw_org_timezone(org_id, tz):
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from app.dependencies import async_session_maker
    from app.models.organization_settings import OrganizationSettings

    async def _write():
        async with async_session_maker() as db:
            row = (await db.execute(
                select(OrganizationSettings).where(OrganizationSettings.organization_id == org_id)
            )).scalar_one()
            row.config = {**(row.config or {}), "timezone": tz}
            flag_modified(row, "config")
            await db.commit()

    asyncio.run(_write())
