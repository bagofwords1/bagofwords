"""A scheduled task's host report is named after the task.

Contract: saving a task names its report after it while the report is unnamed
(a placeholder such as the "Scheduled task" the New-task flow creates it with,
in any locale) or still carries the task's previous name; a report the user
renamed keeps their name. An untitled task is named from its prompt, never
"Scheduled task". Each run's spawned report is "<task name> — <date>".
"""
import uuid

import pytest

from tests.e2e.test_scheduled_spawn_routing import (
    _headers, _reports_by_sp, _run, _setup_user, _stub_agent_run,
)

# The New-task button's localized placeholder, plus the other report placeholders.
PLACEHOLDERS = ["Scheduled task", "משימה מתוזמנת", "Tarea programada", "untitled report", ""]


def _create_task(test_client, token, org_id, report_id, expect=200, **fields):
    body = {"prompt": {"content": "Summarize yesterday's signups"}, "cron_schedule": "0 9 * * *", **fields}
    resp = test_client.post(f"/api/reports/{report_id}/scheduled-prompts", json=body,
                            headers=_headers(token, org_id))
    assert resp.status_code == expect, resp.json()
    return resp.json()


def _update_task(test_client, token, org_id, report_id, sp_id, expect=200, **fields):
    resp = test_client.put(f"/api/reports/{report_id}/scheduled-prompts/{sp_id}", json=fields,
                           headers=_headers(token, org_id))
    assert resp.status_code == expect, resp.json()
    return resp.json()


def _title(get_report, token, org_id, report_id):
    return get_report(report_id, user_token=token, org_id=org_id)["title"]


@pytest.mark.e2e
@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_new_task_names_its_unnamed_report(placeholder, test_client, create_user, login_user, whoami,
                                           create_report, get_report):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title=placeholder, user_token=token, org_id=org_id)
    name = f"Churn watch {uuid.uuid4().hex[:6]}"

    _create_task(test_client, token, org_id, report["id"], title=name)

    assert _title(get_report, token, org_id, report["id"]) == name


@pytest.mark.e2e
def test_new_task_keeps_a_report_the_user_named(test_client, create_user, login_user, whoami,
                                                create_report, get_report):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Q3 revenue deep dive", user_token=token, org_id=org_id)

    _create_task(test_client, token, org_id, report["id"], title="Revenue digest")

    assert _title(get_report, token, org_id, report["id"]) == "Q3 revenue deep dive"


@pytest.mark.e2e
def test_untitled_task_names_report_from_prompt(test_client, create_user, login_user, whoami,
                                                create_report, get_report):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Scheduled task", user_token=token, org_id=org_id)
    prompt = "Count failed payments by region and flag spikes over 20 percent compared to last week"

    _create_task(test_client, token, org_id, report["id"], prompt={"content": prompt}, title="  ")

    title = _title(get_report, token, org_id, report["id"])
    assert title.casefold() != "scheduled task"
    assert title.startswith("Count failed payments")
    assert len(title) <= 61


@pytest.mark.e2e
def test_renaming_task_renames_its_report(test_client, create_user, login_user, whoami,
                                          create_report, get_report):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Scheduled task", user_token=token, org_id=org_id)
    sp = _create_task(test_client, token, org_id, report["id"], title="Daily KPIs")

    for new_name in ("Daily KPI digest", "Morning KPIs"):
        _update_task(test_client, token, org_id, report["id"], sp["id"], title=new_name)
        assert _title(get_report, token, org_id, report["id"]) == new_name


@pytest.mark.e2e
def test_renaming_task_keeps_report_the_user_renamed(test_client, create_user, login_user, whoami,
                                                     create_report, update_report, get_report):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Scheduled task", user_token=token, org_id=org_id)
    sp = _create_task(test_client, token, org_id, report["id"], title="Daily KPIs")
    update_report(report["id"], title="My KPI board", user_token=token, org_id=org_id)

    _update_task(test_client, token, org_id, report["id"], sp["id"], title="Daily KPI digest")

    assert _title(get_report, token, org_id, report["id"]) == "My KPI board"


@pytest.mark.e2e
def test_renaming_one_task_leaves_report_named_after_another(test_client, create_user, login_user, whoami,
                                                             create_report, get_report):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Scheduled task", user_token=token, org_id=org_id)
    _create_task(test_client, token, org_id, report["id"], title="First task")
    second = _create_task(test_client, token, org_id, report["id"], title="Second task")

    _update_task(test_client, token, org_id, report["id"], second["id"], title="Second task renamed")

    assert _title(get_report, token, org_id, report["id"]) == "First task"


@pytest.mark.e2e
def test_non_owner_cannot_rename_report_through_task(test_client, create_user, login_user, whoami,
                                                     create_report, get_report):
    from tests.e2e.test_scheduled_prompt import _setup_owner_and_member
    owner_token, member_token, org_id = _setup_owner_and_member(create_user, login_user, whoami, test_client)
    report = create_report(title="Scheduled task", user_token=owner_token, org_id=org_id)
    sp = _create_task(test_client, owner_token, org_id, report["id"], title="Owner task")

    test_client.put(f"/api/reports/{report['id']}/scheduled-prompts/{sp['id']}",
                    json={"title": "Hijacked"}, headers=_headers(member_token, org_id))

    assert _title(get_report, owner_token, org_id, report["id"]) == "Owner task"


@pytest.mark.e2e
@pytest.mark.parametrize("task_title", ["Weekly churn", None])
def test_run_report_is_task_name_and_date(task_title, monkeypatch, test_client, create_user, login_user,
                                          whoami, create_report, get_reports):
    token, org_id = _setup_user(create_user, login_user, whoami)
    host = create_report(title="Scheduled task", user_token=token, org_id=org_id)
    sp = _create_task(test_client, token, org_id, host["id"], spawn_new_report=True,
                      prompt={"content": "Compute weekly churn"}, title=task_title)

    _stub_agent_run(monkeypatch, [])
    _run(sp["id"])

    runs = _reports_by_sp(get_reports, token, org_id, sp["id"])
    assert len(runs) == 1
    name, sep, date = runs[0]["title"].rpartition(" — ")
    assert sep and date
    assert name == (task_title or "Compute weekly churn")
