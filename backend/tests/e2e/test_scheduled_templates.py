"""Built-in scheduled-task templates: catalog listing, one-click enable
(creates host report + task, with optional agent/cron overrides),
pause-on-disable, resume-on-re-enable, and per-user isolation. A template is
a starting point — the created task stays fully editable.

Companion to tests/e2e/test_scheduled_spawn_routing.py.
"""
import uuid

import pytest

from app.schemas.scheduled_task_template_schema import SCHEDULED_TASK_TEMPLATES


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup_user(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _list_templates(test_client, token, org_id):
    resp = test_client.get("/api/scheduled-prompt-templates", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return {t["key"]: t for t in resp.json()}


def _enable(test_client, token, org_id, key, body=None):
    return test_client.post(
        f"/api/scheduled-prompt-templates/{key}/enable",
        headers=_headers(token, org_id),
        **({"json": body} if body is not None else {}),
    )


def _disable(test_client, token, org_id, key):
    return test_client.post(
        f"/api/scheduled-prompt-templates/{key}/disable", headers=_headers(token, org_id)
    )


@pytest.mark.e2e
def test_list_templates_disabled_by_default(create_user, login_user, whoami, test_client):
    token, org_id = _setup_user(create_user, login_user, whoami)
    templates = _list_templates(test_client, token, org_id)

    assert set(templates) == set(SCHEDULED_TASK_TEMPLATES)
    for key, entry in templates.items():
        definition = SCHEDULED_TASK_TEMPLATES[key]
        assert entry["title"] == definition.title
        assert entry["default_cron"] == definition.default_cron
        assert entry["enabled"] is False
        assert entry["paused"] is False
        assert entry["scheduled_prompt_id"] is None
        assert entry["report_id"] is None


@pytest.mark.e2e
def test_enable_creates_report_and_task(create_user, login_user, whoami, test_client):
    token, org_id = _setup_user(create_user, login_user, whoami)
    definition = SCHEDULED_TASK_TEMPLATES["weekly-data-digest"]

    resp = _enable(test_client, token, org_id, "weekly-data-digest")
    assert resp.status_code == 200, resp.json()
    state = resp.json()
    assert state["enabled"] is True
    assert state["paused"] is False
    assert state["duplicate_count"] == 0
    assert state["cron_schedule"] == definition.default_cron

    # The host report was created server-side with the template's title.
    report = test_client.get(f"/api/reports/{state['report_id']}", headers=_headers(token, org_id))
    assert report.status_code == 200, report.json()
    assert report.json()["title"] == definition.title

    # The task itself is a regular scheduled prompt, stamped with template_key.
    sps = test_client.get(
        f"/api/reports/{state['report_id']}/scheduled-prompts", headers=_headers(token, org_id)
    )
    assert sps.status_code == 200, sps.json()
    rows = sps.json()
    assert len(rows) == 1
    sp = rows[0]
    assert sp["id"] == state["scheduled_prompt_id"]
    assert sp["template_key"] == "weekly-data-digest"
    assert sp["cron_schedule"] == definition.default_cron
    assert sp["is_active"] is True
    assert sp["spawn_new_report"] == definition.spawn_new_report
    assert sp["prompt"]["content"] == definition.prompt_content


@pytest.mark.e2e
def test_enable_is_idempotent(create_user, login_user, whoami, test_client):
    token, org_id = _setup_user(create_user, login_user, whoami)

    first = _enable(test_client, token, org_id, "monthly-trends-recap").json()
    second = _enable(test_client, token, org_id, "monthly-trends-recap").json()

    assert second["enabled"] is True
    assert second["scheduled_prompt_id"] == first["scheduled_prompt_id"]
    assert second["report_id"] == first["report_id"]
    assert second["duplicate_count"] == 0


@pytest.mark.e2e
def test_disable_pauses_and_reenable_resumes_same_row(
    create_user, login_user, whoami, test_client
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    enabled = _enable(test_client, token, org_id, "weekly-data-digest").json()

    disabled = _disable(test_client, token, org_id, "weekly-data-digest").json()
    assert disabled["enabled"] is False
    assert disabled["paused"] is True
    # The row survives the disable — paused, not deleted.
    assert disabled["scheduled_prompt_id"] == enabled["scheduled_prompt_id"]
    sps = test_client.get(
        f"/api/reports/{enabled['report_id']}/scheduled-prompts", headers=_headers(token, org_id)
    ).json()
    assert len(sps) == 1 and sps[0]["is_active"] is False

    # Re-enable resumes the same task and report (history kept).
    resumed = _enable(test_client, token, org_id, "weekly-data-digest").json()
    assert resumed["enabled"] is True
    assert resumed["scheduled_prompt_id"] == enabled["scheduled_prompt_id"]
    assert resumed["report_id"] == enabled["report_id"]

    # Disabling an untouched template is a safe no-op.
    untouched = _disable(test_client, token, org_id, "monthly-trends-recap").json()
    assert untouched["enabled"] is False and untouched["paused"] is False


@pytest.mark.e2e
def test_template_rows_are_fully_editable(create_user, login_user, whoami, test_client):
    """A template is only a starting point: prompt, title, routing, schedule
    and notifications are all editable afterwards, and the row keeps counting
    as the enabled instance of the template."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    state = _enable(test_client, token, org_id, "weekly-data-digest").json()
    url = f"/api/reports/{state['report_id']}/scheduled-prompts/{state['scheduled_prompt_id']}"
    headers = _headers(token, org_id)

    resp = test_client.put(url, json={
        "prompt": {"content": "my own twist on the digest", "mode": "chat"},
        "title": "My digest",
        "spawn_new_report": False,
        "cron_schedule": "0 8 * * 2",
        "notification_subscribers": [{"type": "email", "address": "rachel@bagofwords.com"}],
    }, headers=headers)
    assert resp.status_code == 200, resp.json()
    sp = resp.json()
    assert sp["prompt"]["content"] == "my own twist on the digest"
    assert sp["title"] == "My digest"
    assert sp["spawn_new_report"] is False
    assert sp["template_key"] == "weekly-data-digest"

    # Still the enabled instance, with the edited schedule surfaced.
    entry = _list_templates(test_client, token, org_id)["weekly-data-digest"]
    assert entry["enabled"] is True
    assert entry["cron_schedule"] == "0 8 * * 2"


@pytest.mark.e2e
def test_enable_with_agent_and_cron_overrides(
    create_user, login_user, whoami, test_client, create_data_source, dynamic_sqlite_db
):
    """The enable body narrows the agent scope and overrides the shipped cron."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    ds1 = create_data_source(
        name="Tmpl DB One", type="sqlite", config={"database": dynamic_sqlite_db},
        credentials={}, user_token=token, org_id=org_id,
    )
    create_data_source(
        name="Tmpl DB Two", type="sqlite", config={"database": dynamic_sqlite_db},
        credentials={}, user_token=token, org_id=org_id,
    )

    resp = _enable(test_client, token, org_id, "weekly-data-digest",
                   body={"data_source_ids": [ds1["id"]], "cron_schedule": "0 7 * * 5"})
    assert resp.status_code == 200, resp.json()
    state = resp.json()
    assert state["enabled"] is True
    assert state["cron_schedule"] == "0 7 * * 5"

    # Only the picked agent is attached to the host report — not both.
    report = test_client.get(f"/api/reports/{state['report_id']}", headers=_headers(token, org_id)).json()
    assert [d["id"] for d in report["data_sources"]] == [ds1["id"]]

    # An invalid cron override is rejected up front.
    bad = _enable(test_client, token, org_id, "monthly-trends-recap",
                  body={"cron_schedule": "not a cron"})
    assert bad.status_code == 400


@pytest.mark.e2e
def test_customize_flow_creates_a_stamped_task(
    create_user, login_user, whoami, test_client, create_report
):
    """The customize flow creates through the regular endpoint with a
    template_key in the body — the task is stamped and counts as the
    template's enabled instance."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Weekly data digest", user_token=token, org_id=org_id)

    resp = test_client.post(
        f"/api/reports/{report['id']}/scheduled-prompts",
        json={"prompt": {"content": "my customized digest"}, "cron_schedule": "0 10 * * 3",
              "title": "My digest", "template_key": "weekly-data-digest"},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["template_key"] == "weekly-data-digest"

    entry = _list_templates(test_client, token, org_id)["weekly-data-digest"]
    assert entry["enabled"] is True
    assert entry["scheduled_prompt_id"] == resp.json()["id"]
    assert entry["cron_schedule"] == "0 10 * * 3"

    # An unknown template key is rejected — the stamp must stay meaningful.
    bad = test_client.post(
        f"/api/reports/{report['id']}/scheduled-prompts",
        json={"prompt": {"content": "hi"}, "cron_schedule": "0 9 * * 1",
              "template_key": "no-such-template"},
        headers=_headers(token, org_id),
    )
    assert bad.status_code == 400


@pytest.mark.e2e
def test_template_state_is_per_user(create_user, login_user, whoami, test_client):
    """Two users in the same org each get their own instance of a template."""
    token_a, org_id = _setup_user(create_user, login_user, whoami)

    email_b = f"tmpl_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email_b, "role": "member"},
        headers=_headers(token_a, org_id),
    )
    create_user(email=email_b, password="test123")
    token_b = login_user(email=email_b, password="test123")

    _enable(test_client, token_a, org_id, "weekly-data-digest")

    entry_b = _list_templates(test_client, token_b, org_id)["weekly-data-digest"]
    assert entry_b["enabled"] is False

    state_b = _enable(test_client, token_b, org_id, "weekly-data-digest").json()
    state_a = _list_templates(test_client, token_a, org_id)["weekly-data-digest"]
    assert state_a["enabled"] is True and state_b["enabled"] is True
    assert state_b["scheduled_prompt_id"] != state_a["scheduled_prompt_id"]
    assert state_b["report_id"] != state_a["report_id"]


@pytest.mark.e2e
def test_deleting_the_task_resets_the_template(create_user, login_user, whoami, test_client):
    token, org_id = _setup_user(create_user, login_user, whoami)
    state = _enable(test_client, token, org_id, "weekly-data-digest").json()

    resp = test_client.delete(
        f"/api/reports/{state['report_id']}/scheduled-prompts/{state['scheduled_prompt_id']}",
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 204

    entry = _list_templates(test_client, token, org_id)["weekly-data-digest"]
    assert entry["enabled"] is False and entry["paused"] is False

    # Enabling again starts fresh: new report, new task.
    fresh = _enable(test_client, token, org_id, "weekly-data-digest").json()
    assert fresh["enabled"] is True
    assert fresh["scheduled_prompt_id"] != state["scheduled_prompt_id"]
    assert fresh["report_id"] != state["report_id"]


@pytest.mark.e2e
def test_unknown_template_key_404s(create_user, login_user, whoami, test_client):
    token, org_id = _setup_user(create_user, login_user, whoami)
    assert _enable(test_client, token, org_id, "no-such-template").status_code == 404
    assert _disable(test_client, token, org_id, "no-such-template").status_code == 404
