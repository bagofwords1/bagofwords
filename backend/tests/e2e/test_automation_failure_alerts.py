"""Unattended runs that fail must say so.

A scheduled task or trigger runs with nobody watching, and the agent's usual
failure mode is not an exception — it returns normally with an errored system
completion. Before this, that shape produced a *success* email ("your report
ran — 0 steps, 0 queries, 0 artifacts"), so a schedule could be dead for weeks
while looking healthy.

What is asserted here:

  * a failed scheduled run alerts the OWNER (inbox + email) and sends the
    subscribers nothing — that list means "send me the results", and a
    provider error is not a result,
  * a successful run is untouched: subscribers notified, owner not alarmed,
  * repeated failures collapse into one inbox row rather than stacking,
  * the same holds for triggers, and the event entry is marked errored,
  * the classified cause survives onto the alert as an actionable hint,
  * the failure email renders in every locale we ship.

Deterministic — the agent run is stubbed (see `_stub_agent_run`), and the run
is invoked directly with force=True to bypass the cross-worker claim.

Run:
    cd backend
    uv run pytest tests/e2e/test_automation_failure_alerts.py -v
"""
import asyncio

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup_user(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    return token, me["organizations"][0]["id"], me["id"]


AUTH_ERROR = {"code": "auth", "summary": "The model provider rejected the request",
              "provider_message": "invalid x-api-key"}


def _stub_agent_run(monkeypatch, status="success", error=None):
    """Stand in for the agent, writing the system completion it would write.

    The completion is the whole point: detection reads its status, so a stub
    that only returns a value cannot exercise the code under test.
    """
    from app.models.completion import Completion
    from app.services.completion_service import CompletionService

    async def fake_create_completion(self, db, report_id, completion_data,
                                     current_user, organization, background=False, **kw):
        db.add(Completion(
            prompt={"content": completion_data.prompt.content},
            completion={"content": "done", **({"error": error} if error else {})},
            model="stub", report_id=str(report_id), turn_index=0,
            role="system", status=status, user_id=str(current_user.id),
        ))
        await db.commit()
        return None

    monkeypatch.setattr(CompletionService, "create_completion", fake_create_completion)


def _capture_emails(monkeypatch):
    """Record both outbound emails without touching a transport.

    `send_scheduled_prompt_results` is fired through `asyncio.create_task`, so
    the recorder appends at CALL time (not await time) — the loop closes before
    a scheduled task necessarily gets to run, and the assertion here is about
    whether we asked to send at all.
    """
    from app.services.notification_service import notification_service
    sent = {"failure": [], "results": []}

    def fake_results(*a, **kw):
        sent["results"].append(kw)

        async def _noop():
            return None
        return _noop()

    async def fake_failure(*a, **kw):
        sent["failure"].append(kw)

    monkeypatch.setattr(notification_service, "send_scheduled_prompt_results", fake_results)
    monkeypatch.setattr(notification_service, "send_automation_failure", fake_failure)
    return sent


def _run_schedule(sp_id):
    from app.services.scheduled_prompt_service import scheduled_prompt_service
    asyncio.run(scheduled_prompt_service.scheduled_run_prompt(sp_id, force=True))


def _notifications(test_client, token, org_id, type_=None):
    items = test_client.get("/api/notifications", headers=_headers(token, org_id)).json()["items"]
    return [n for n in items if type_ is None or n.get("type") == type_]


def _create_schedule(test_client, token, org_id, report_id, user_id, **overrides):
    body = {
        "prompt": {"content": "Summarize yesterday's revenue"},
        "cron_schedule": "0 9 * * *",
        "title": "Daily revenue",
        "notification_subscribers": [{"type": "user", "id": user_id},
                                     {"type": "email", "address": "sub@example.com"}],
    }
    body.update(overrides)
    resp = test_client.post(f"/api/reports/{report_id}/scheduled-prompts",
                            json=body, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_failed_scheduled_run_alerts_the_owner_and_spares_subscribers(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """The failure shape that returns normally: no exception, errored completion.

    The owner hears about it once, with the classified cause; the subscriber
    list — which asked for results — gets neither an email nor an inbox row."""
    token, org_id, user_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Revenue", user_token=token, org_id=org_id, data_sources=[])
    sp = _create_schedule(test_client, token, org_id, report["id"], user_id)

    sent = _capture_emails(monkeypatch)
    _stub_agent_run(monkeypatch, status="error", error=AUTH_ERROR)
    _run_schedule(sp["id"])

    assert sent["results"] == [], "a failed run must not email the results subscribers"
    assert len(sent["failure"]) == 1
    email = sent["failure"][0]
    assert email["automation_name"] == "Daily revenue"
    assert "invalid x-api-key" in email["error_message"]
    assert "check the API key" in (email["hint"] or "")

    assert _notifications(test_client, token, org_id, "scheduled_run") == []
    failed = _notifications(test_client, token, org_id, "automation_failed")
    assert len(failed) == 1
    assert failed[0]["severity"] == "error"
    assert "Daily revenue" in failed[0]["title"]
    assert failed[0]["link"] == f"/reports/{report['id']}"
    assert "check the API key" in failed[0]["body"]


@pytest.mark.e2e
def test_successful_scheduled_run_still_notifies_subscribers(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """The happy path is unchanged — failure detection must not cost a result."""
    token, org_id, user_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Revenue OK", user_token=token, org_id=org_id, data_sources=[])
    sp = _create_schedule(test_client, token, org_id, report["id"], user_id)

    sent = _capture_emails(monkeypatch)
    _stub_agent_run(monkeypatch, status="success")
    _run_schedule(sp["id"])

    assert len(sent["results"]) == 1
    assert sent["failure"] == []
    assert _notifications(test_client, token, org_id, "automation_failed") == []
    assert len(_notifications(test_client, token, org_id, "scheduled_run")) == 1


@pytest.mark.e2e
def test_repeated_failures_collapse_into_one_inbox_row(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """A schedule broken on Monday is still broken on Friday. One live row per
    automation, refreshed — not five identical ones burying everything else."""
    token, org_id, user_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Repeat", user_token=token, org_id=org_id, data_sources=[])
    sp = _create_schedule(test_client, token, org_id, report["id"], user_id)

    _capture_emails(monkeypatch)
    _stub_agent_run(monkeypatch, status="error", error=AUTH_ERROR)
    _run_schedule(sp["id"])
    _run_schedule(sp["id"])
    _run_schedule(sp["id"])

    assert len(_notifications(test_client, token, org_id, "automation_failed")) == 1


@pytest.mark.e2e
def test_an_unclassified_failure_still_reaches_the_owner(
    monkeypatch, create_user, login_user, whoami, test_client, create_report,
):
    """No classified code and no error payload — the alert must still fire,
    carrying whatever the run did say rather than being swallowed."""
    token, org_id, user_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Mystery", user_token=token, org_id=org_id, data_sources=[])
    sp = _create_schedule(test_client, token, org_id, report["id"], user_id,
                          notification_subscribers=[])

    sent = _capture_emails(monkeypatch)
    _stub_agent_run(monkeypatch, status="error")  # errored, but nothing classified
    _run_schedule(sp["id"])

    assert len(sent["failure"]) == 1
    assert len(_notifications(test_client, token, org_id, "automation_failed")) == 1


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _create_trigger(test_client, token, org_id):
    resp = test_client.post("/api/triggers", json={
        "name": "Alert trigger", "source": "generic", "auth_mode": "token",
        "classify_enabled": False, "task_template": "Investigate the alert.",
        "mode": "chat", "data_source_ids": [],
    }, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _deliver(trigger_id, payload, delivery_id):
    from app.services.webhook_service import webhook_service
    asyncio.run(webhook_service.process_delivery(
        trigger_id, payload, {"x-bow-delivery": delivery_id},
    ))


@pytest.mark.e2e
def test_failed_trigger_run_alerts_the_owner(
    monkeypatch, create_user, login_user, whoami, test_client, get_reports, get_completions,
):
    """Same contract for the event-driven half: the owner is told the run died,
    the event entry is marked errored, and no cheerful 'fired' row is left
    behind claiming findings that don't exist."""
    token, org_id, _ = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id)

    sent = _capture_emails(monkeypatch)
    _stub_agent_run(monkeypatch, status="error", error=AUTH_ERROR)
    _deliver(trig["id"], {"type": "alert", "title": "Latency spike"}, "d-fail-1")

    reports = get_reports(user_token=token, org_id=org_id)
    items = reports.get("reports", reports) if isinstance(reports, dict) else reports
    spawned = [r for r in items if r.get("webhook_id") == trig["id"]]
    assert len(spawned) == 1

    events = [c for c in get_completions(report_id=spawned[0]["id"], user_token=token, org_id=org_id)
              if c.get("role") == "external"]
    assert events and events[0]["status"] == "error"

    assert _notifications(test_client, token, org_id, "trigger_run") == []
    failed = _notifications(test_client, token, org_id, "automation_failed")
    assert len(failed) == 1
    assert failed[0]["severity"] == "error"
    assert "Alert trigger" in failed[0]["title"]
    assert len(sent["failure"]) == 1


@pytest.mark.e2e
def test_successful_trigger_run_is_unchanged(
    monkeypatch, create_user, login_user, whoami, test_client,
):
    token, org_id, _ = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id)

    sent = _capture_emails(monkeypatch)
    _stub_agent_run(monkeypatch, status="success")
    _deliver(trig["id"], {"type": "alert", "title": "All good"}, "d-ok-1")

    assert sent["failure"] == []
    assert len(_notifications(test_client, token, org_id, "trigger_run")) == 1
    assert _notifications(test_client, token, org_id, "automation_failed") == []


# ---------------------------------------------------------------------------
# Detection + rendering
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_run_outcome_reads_the_completion_not_the_exception(
    monkeypatch, create_user, login_user, whoami, create_report,
):
    """Detection contract, directly: success → ok, errored → the classified
    code, no completion at all → a failure rather than a silent pass."""
    from app.dependencies import async_session_maker
    from app.models.completion import Completion
    from app.services.automation_alerts import run_outcome

    token, org_id, user_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Outcome", user_token=token, org_id=org_id, data_sources=[])

    async def _scenario():
        async with async_session_maker() as db:
            empty = await run_outcome(db, report["id"])

            db.add(Completion(prompt={"content": "x"}, completion={"content": "ok"},
                              model="stub", report_id=report["id"], turn_index=0,
                              role="system", status="success", user_id=user_id))
            await db.commit()
            good = await run_outcome(db, report["id"])

            db.add(Completion(prompt={"content": "x"},
                              completion={"content": "", "error": {**AUTH_ERROR, "message": "boom"}},
                              model="stub", report_id=report["id"], turn_index=1,
                              role="system", status="error", user_id=user_id))
            await db.commit()
            bad = await run_outcome(db, report["id"])
            return empty, good, bad

    empty, good, bad = asyncio.run(_scenario())
    assert empty.ok is False, "a run that produced nothing is not a success"
    assert good.ok is True
    assert bad.ok is False
    assert bad.error_code == "auth"
    assert bad.error_message == "boom"
    assert "check the API key" in bad.hint


@pytest.mark.e2e
@pytest.mark.parametrize("locale", ["en", "es", "he", "fr", "sv", "ar", "ru", "de", "pt", "it"])
def test_failure_email_renders_in_every_shipped_locale(locale):
    """The catalogs are ten parallel dicts — a missing block shows up as an
    email with holes in it, which nobody sees until a schedule breaks."""
    from app.services.email_renderer import render_automation_failure_email
    from app.services.email_strings import AUTOMATION_FAILED, STRINGS

    block = STRINGS[locale].get(AUTOMATION_FAILED)
    assert block, "locale ships without a failure block"
    assert set(block) >= {"subject", "greeting", "intro", "next_run", "cta_text", "footer"}

    subject, html = render_automation_failure_email(
        locale,
        automation_name="Daily revenue",
        link="https://example.test/reports/abc",
        error_message="invalid x-api-key",
        hint="Check the API key on the provider.",
        next_run_at="2026-08-02 09:00 UTC",
    )
    assert "Daily revenue" in subject
    assert "{" not in subject, "an unfilled placeholder leaked into the subject"
    assert "Daily revenue" in html
    assert "invalid x-api-key" in html
    assert "Check the API key on the provider." in html
    assert "https://example.test/reports/abc" in html
    assert f'dir="{"rtl" if locale in ("he", "ar") else "ltr"}"' in html
    # The timestamp is rendered in the org timezone by the caller; the sentence
    # around it has to come from THIS locale, or a translated email ends with an
    # English clause bolted on.
    assert "2026-08-02 09:00 UTC" in html
    assert "{time}" not in html
    if locale != "en":
        assert "The schedule will run again" not in html
