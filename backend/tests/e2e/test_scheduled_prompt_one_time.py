"""One-time scheduled tasks run exactly once, at an absolute instant.

A one-time task used to be stored as a cron pinned to one minute/hour/day/month
— a yearly schedule — and nothing deactivated it after it fired. It is now
stored as `run_once_at` (a UTC instant) and consumed by its first run,
scheduled or manual.

Deterministic: the agent run is stubbed at the CompletionService boundary and
the cross-worker claim is patched (it coordinates processes, not behavior).
The scheduler is not started under tests, so `next_run_at` is always empty;
what will fire is read from the job registered with the scheduler instead.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup(create_user, login_user, whoami, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="One-time host", user_token=token, org_id=org_id)
    return token, org_id, report["id"]


def _stub_agent_run(monkeypatch, calls):
    from app.models.completion import Completion
    from app.services.completion_service import CompletionService

    async def fake_create_completion(self, db, report_id, completion_data,
                                     current_user, organization, background=False, **kw):
        calls.append(str(report_id))
        db.add(Completion(
            prompt={"content": completion_data.prompt.content},
            completion={"content": "done"},
            model="stub", report_id=str(report_id), turn_index=0,
            role="system", status="success", user_id=str(current_user.id),
        ))
        await db.commit()
        return None

    monkeypatch.setattr(CompletionService, "create_completion", fake_create_completion)


def _fire(sp_id, force=False):
    """What the scheduler (force=False) or "Run now" (force=True) invokes."""
    from app.services.scheduled_prompt_service import scheduled_prompt_service
    with patch("app.services.scheduled_prompt_service.claim_scheduled_run", return_value=True):
        asyncio.run(scheduled_prompt_service.scheduled_run_prompt(sp_id, force=force))


def _job(sp_id):
    """The scheduler job that will fire this task, or None."""
    from app.core.scheduler import scheduler
    return scheduler.get_job(f"scheduled_prompt_{sp_id}")


def _assert_fires_once_at(sp_id, when):
    from apscheduler.triggers.date import DateTrigger
    job = _job(sp_id)
    assert job is not None
    assert isinstance(job.trigger, DateTrigger)
    assert abs(job.trigger.run_date - when) < timedelta(seconds=1)


def _iso(dt):
    return dt.isoformat()


def _as_utc(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _create(test_client, token, org_id, report_id, expect=200, **body):
    resp = test_client.post(
        f"/api/reports/{report_id}/scheduled-prompts",
        json={"prompt": {"content": "One-off revenue check"}, **body},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


def _get(test_client, token, org_id, sp_id):
    resp = test_client.get("/api/scheduled-prompts", params={"status": "all"},
                           headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    return next(sp for sp in resp.json()["scheduled_prompts"] if sp["id"] == sp_id)


@pytest.mark.e2e
@pytest.mark.parametrize("delay", [timedelta(minutes=5), timedelta(hours=2), timedelta(days=3)])
def test_one_time_task_runs_once_then_stays_inactive(
    delay, monkeypatch, create_user, login_user, whoami, create_report, test_client,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    run_at = datetime.now(timezone.utc) + delay

    sp = _create(test_client, token, org_id, report_id, run_once_at=_iso(run_at))
    assert sp["is_active"] is True
    assert abs(_as_utc(sp["run_once_at"]) - run_at) < timedelta(seconds=1)
    _assert_fires_once_at(sp["id"], run_at)

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _fire(sp["id"])
    assert len(calls) == 1

    after = _get(test_client, token, org_id, sp["id"])
    assert after["is_active"] is False
    assert _job(sp["id"]) is None

    # A second fire (another worker, a stale job) must not run it again.
    _fire(sp["id"])
    assert len(calls) == 1


@pytest.mark.e2e
def test_concurrent_fires_run_a_one_time_task_once(
    monkeypatch, create_user, login_user, whoami, create_report, test_client,
):
    """Every worker's scheduler fires the same job; both load the task as
    active before either has run it. Only one may proceed."""
    from app.services.scheduled_prompt_service import scheduled_prompt_service

    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    sp = _create(test_client, token, org_id, report_id,
                 run_once_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)))
    calls = []
    _stub_agent_run(monkeypatch, calls)

    async def _both():
        await asyncio.gather(*(scheduled_prompt_service.scheduled_run_prompt(sp["id"]) for _ in range(2)))
    with patch("app.services.scheduled_prompt_service.claim_scheduled_run", return_value=True):
        asyncio.run(_both())

    assert len(calls) == 1


@pytest.mark.e2e
def test_run_now_consumes_an_active_one_time_task(
    monkeypatch, create_user, login_user, whoami, create_report, test_client,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    run_at = datetime.now(timezone.utc) + timedelta(hours=6)
    sp = _create(test_client, token, org_id, report_id, run_once_at=_iso(run_at))
    _assert_fires_once_at(sp["id"], run_at)

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _fire(sp["id"], force=True)
    assert len(calls) == 1

    after = _get(test_client, token, org_id, sp["id"])
    assert after["is_active"] is False
    assert _job(sp["id"]) is None

    # The scheduled fire that was planned for later no longer runs.
    _fire(sp["id"])
    assert len(calls) == 1


@pytest.mark.e2e
def test_recurring_task_stays_active_after_a_run(
    monkeypatch, create_user, login_user, whoami, create_report, test_client,
):
    """Consuming is for one-time tasks only."""
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    sp = _create(test_client, token, org_id, report_id, cron_schedule="30 16 14 * *")

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _fire(sp["id"])
    _fire(sp["id"], force=True)
    assert len(calls) == 2
    after = _get(test_client, token, org_id, sp["id"])
    assert after["is_active"] is True
    assert after["run_once_at"] is None
    assert _job(sp["id"]) is not None


@pytest.mark.e2e
@pytest.mark.parametrize("offset_hours", [3, -5, 0])
def test_run_once_at_is_an_absolute_instant(
    offset_hours, create_user, login_user, whoami, create_report, test_client,
):
    """The browser's UTC offset must not shift the run: the same instant written
    in any offset is stored as the same UTC time."""
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    instant = (datetime.now(timezone.utc) + timedelta(hours=10)).replace(microsecond=0)
    local = instant.astimezone(timezone(timedelta(hours=offset_hours)))

    sp = _create(test_client, token, org_id, report_id, run_once_at=local.isoformat())
    assert _as_utc(sp["run_once_at"]) == instant
    _assert_fires_once_at(sp["id"], instant)


@pytest.mark.e2e
def test_switching_between_one_time_and_recurring(
    create_user, login_user, whoami, create_report, test_client,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    sp = _create(test_client, token, org_id, report_id,
                 run_once_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)))
    url = f"/api/reports/{report_id}/scheduled-prompts/{sp['id']}"

    resp = test_client.put(url, json={"cron_schedule": "0 9 * * 1"}, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["cron_schedule"] == "0 9 * * 1"
    assert resp.json()["run_once_at"] is None
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(_job(sp["id"]).trigger, CronTrigger)

    run_at = datetime.now(timezone.utc) + timedelta(days=2)
    resp = test_client.put(url, json={"run_once_at": _iso(run_at)}, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    assert abs(_as_utc(resp.json()["run_once_at"]) - run_at) < timedelta(seconds=1)
    _assert_fires_once_at(sp["id"], run_at)

    # Updating anything else leaves the schedule as it is.
    resp = test_client.put(url, json={"title": "Renamed"}, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    assert abs(_as_utc(resp.json()["run_once_at"]) - run_at) < timedelta(seconds=1)
    _assert_fires_once_at(sp["id"], run_at)


@pytest.mark.e2e
def test_schedule_must_be_exactly_one_kind(
    create_user, login_user, whoami, create_report, test_client,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    later = _iso(datetime.now(timezone.utc) + timedelta(hours=1))

    _create(test_client, token, org_id, report_id, expect=400)
    _create(test_client, token, org_id, report_id, expect=400,
            cron_schedule="0 9 * * *", run_once_at=later)
    # The placeholder stored for one-time tasks is not a schedule on its own.
    _create(test_client, token, org_id, report_id, expect=400, cron_schedule="@once")


@pytest.mark.e2e
def test_an_active_one_time_task_cannot_be_in_the_past(
    create_user, login_user, whoami, create_report, test_client,
):
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    past = _iso(datetime.now(timezone.utc) - timedelta(hours=2))

    _create(test_client, token, org_id, report_id, expect=400, run_once_at=past)

    # Paused is fine: that is how "Run now" records a one-time run.
    sp = _create(test_client, token, org_id, report_id, run_once_at=past, is_active=False)
    url = f"/api/reports/{report_id}/scheduled-prompts/{sp['id']}"

    resp = test_client.put(url, json={"is_active": True}, headers=_headers(token, org_id))
    assert resp.status_code == 400, resp.text

    resp = test_client.put(url, json={"title": "Still editable"}, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text

    later = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
    resp = test_client.put(url, json={"run_once_at": later, "is_active": True},
                           headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True


@pytest.mark.e2e
@pytest.mark.parametrize("passed_by", [timedelta(seconds=20), timedelta(minutes=3)])
def test_a_one_time_task_cannot_be_resumed_once_its_time_passed(
    passed_by, create_user, login_user, whoami, create_report, test_client,
):
    """No clock-skew grace on resume: a task whose instant passed already ran
    (or was dropped), and re-arming it would run it again. Typical source: an
    edit modal left open while the task fired."""
    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    sp = _create(test_client, token, org_id, report_id, is_active=False,
                 run_once_at=_iso(datetime.now(timezone.utc) - passed_by))
    url = f"/api/reports/{report_id}/scheduled-prompts/{sp['id']}"

    resp = test_client.put(url, json={"is_active": True, "title": "Stale save"},
                           headers=_headers(token, org_id))
    assert resp.status_code == 400, resp.text
    assert _job(sp["id"]) is None


@pytest.mark.e2e
def test_project_automations_carry_the_one_time_instant(
    create_user, login_user, whoami, create_project, create_report, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    project = create_project(user_token=token, org_id=org_id)
    report = create_report(title="In project", user_token=token, org_id=org_id, project_id=project["id"])
    run_at = datetime.now(timezone.utc) + timedelta(hours=5)
    once = _create(test_client, token, org_id, report["id"], run_once_at=_iso(run_at))
    weekly = _create(test_client, token, org_id, report["id"], cron_schedule="0 9 * * 1")

    resp = test_client.get(f"/api/projects/{project['id']}/automations", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    items = {i["id"]: i for i in resp.json()}
    assert abs(_as_utc(items[once["id"]]["run_once_at"]) - run_at) < timedelta(seconds=1)
    assert items[weekly["id"]]["run_once_at"] is None


@pytest.mark.e2e
@pytest.mark.parametrize("missed_by,stays_active", [
    (timedelta(minutes=30), True),
    (timedelta(hours=23), True),
    (timedelta(hours=25), False),
    (timedelta(days=30), False),
])
def test_missed_one_time_run_on_startup(
    missed_by, stays_active, create_user, login_user, whoami, create_report, test_client,
):
    """Down at the planned time: run late within 24h, otherwise drop the run
    and deactivate the task so it is not re-registered on every start."""
    from sqlalchemy import update as sa_update
    from app.dependencies import async_session_maker
    from app.models.scheduled_prompt import ScheduledPrompt
    from app.services.scheduled_prompt_service import scheduled_prompt_service

    token, org_id, report_id = _setup(create_user, login_user, whoami, create_report)
    past = datetime.now(timezone.utc) - missed_by
    sp = _create(test_client, token, org_id, report_id, run_once_at=_iso(past), is_active=False)

    # The API refuses an active task in the past, but that is exactly the state
    # a task is in when the server was down at its run time — write it directly.
    async def _activate():
        async with async_session_maker() as db:
            await db.execute(sa_update(ScheduledPrompt)
                             .where(ScheduledPrompt.id == sp["id"]).values(is_active=True))
            await db.commit()
    asyncio.run(_activate())

    asyncio.run(scheduled_prompt_service.register_all_jobs())

    after = _get(test_client, token, org_id, sp["id"])
    assert after["is_active"] is stays_active
    if stays_active:
        # Registered with the 24h grace, so the scheduler still runs it late.
        _assert_fires_once_at(sp["id"], past)
        assert _job(sp["id"]).misfire_grace_time >= missed_by.total_seconds()
    else:
        assert _job(sp["id"]) is None
