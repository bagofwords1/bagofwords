"""Loop A — deterministic trigger tests (no LLM).

Standalone triggers = user-owned webhooks (report_id NULL) that SPAWN a new
session per accepted delivery. See docs/design/agent-triggers.md and
docs/feedback-loops/trigger-webhooks.md.

The model-dependent steps are stubbed (precedent: test_scheduled_prompt's
monkeypatched scheduled_run_prompt):
- CompletionService.create_completion → recorded, not executed
- WebhookClassifier.classify → canned act/decline decisions

Delivery processing is invoked directly (asyncio.run on
webhook_service.process_delivery) because the receiver route schedules it as
a fire-and-forget task; the route itself is covered for auth/status codes.
"""
import asyncio
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _setup_owner_and_member(create_user, login_user, whoami, test_client):
    """Two users in one org: owner + member. Returns (owner_token, member_token, org_id)."""
    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    org_id = whoami(owner_token)["organizations"][0]["id"]

    member_email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    invite_resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(owner_token, org_id),
    )
    assert invite_resp.status_code == 200, invite_resp.json()
    member = create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    return owner_token, member_token, org_id


def _setup_user(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _make_sqlite_ds(create_data_source, db_path, token, org_id, name="Trigger Test DB"):
    return create_data_source(
        name=name, type="sqlite",
        config={"database": db_path}, credentials={},
        user_token=token, org_id=org_id,
    )


def _create_trigger(test_client, token, org_id, **overrides):
    body = {
        "name": "Alert trigger",
        "source": "generic",
        "auth_mode": "token",
        "classify_enabled": False,
        "task_template": "Investigate the alert and summarize findings.",
        "mode": "chat",
        "data_source_ids": [],
    }
    body.update(overrides)
    resp = test_client.post("/api/triggers", json=body, headers=_headers(token, org_id))
    return resp


def _stub_agent_run(monkeypatch, calls):
    """Replace the agent run with a recorder; marks the event success path."""
    from app.services.completion_service import CompletionService

    async def fake_create_completion(self, db, report_id, completion_data,
                                     current_user, organization, background=False, **kw):
        calls.append({
            "report_id": str(report_id),
            "content": completion_data.prompt.content,
            "mode": completion_data.prompt.mode,
            "model_id": completion_data.prompt.model_id,
            "webhook_id": kw.get("webhook_id"),
            "user_id": str(current_user.id),
        })
        return None

    monkeypatch.setattr(CompletionService, "create_completion", fake_create_completion)


def _deliver(trigger_id, payload, delivery_id):
    """Run the delivery pipeline synchronously (own event loop + session)."""
    from app.services.webhook_service import webhook_service
    asyncio.run(webhook_service.process_delivery(
        trigger_id, payload, {"x-bow-delivery": delivery_id},
    ))


def _find_spawned_reports(get_reports, token, org_id, trigger_id):
    reports = get_reports(user_token=token, org_id=org_id)
    items = reports.get("reports", reports) if isinstance(reports, dict) else reports
    return [r for r in items if r.get("webhook_id") == trigger_id]


# ---------------------------------------------------------------------------
# CRUD + ownership
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_trigger_crud_and_run_spec(
    create_user, login_user, whoami, test_client,
    create_data_source, dynamic_sqlite_db,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    ds = _make_sqlite_ds(create_data_source, dynamic_sqlite_db, token, org_id)

    resp = _create_trigger(
        test_client, token, org_id,
        data_source_ids=[ds["id"]], mode="deep",
        classify_enabled=True, classifier_prompt="Only act on P1 alerts",
    )
    assert resp.status_code == 200, resp.json()
    trig = resp.json()
    # Standalone: no bound report, secret + URL revealed once
    assert trig["report_id"] is None
    assert trig["secret"] and trig["secret"].startswith("whsec_")
    assert trig["delivery_url"].endswith(f"/webhooks/{trig['token']}")
    # Run spec persisted
    assert trig["mode"] == "deep"
    assert trig["task_template"].startswith("Investigate")
    assert [d["id"] for d in trig["data_sources"]] == [ds["id"]]

    # List shows it (without the secret)
    listed = test_client.get("/api/triggers", headers=_headers(token, org_id)).json()
    assert [t["id"] for t in listed] == [trig["id"]]
    assert listed[0]["secret"] is None

    # Update run spec
    up = test_client.put(
        f"/api/triggers/{trig['id']}",
        json={"name": "Renamed", "mode": "chat", "task_template": "New task"},
        headers=_headers(token, org_id),
    )
    assert up.status_code == 200, up.json()
    assert up.json()["name"] == "Renamed"
    assert up.json()["mode"] == "chat"

    # Rotate reveals a fresh secret
    rot = test_client.post(f"/api/triggers/{trig['id']}/rotate", headers=_headers(token, org_id))
    assert rot.status_code == 200
    assert rot.json()["secret"] and rot.json()["secret"] != trig["secret"]

    # Delete
    dele = test_client.delete(f"/api/triggers/{trig['id']}", headers=_headers(token, org_id))
    assert dele.status_code == 204
    assert test_client.get("/api/triggers", headers=_headers(token, org_id)).json() == []


@pytest.mark.e2e
def test_triggers_are_private_to_their_owner(
    create_user, login_user, whoami, test_client,
):
    """Users see and manage ONLY their own triggers — same-org members get
    nothing in the list and 404 (not 403 — no existence leak) on direct access."""
    owner_token, member_token, org_id = _setup_owner_and_member(
        create_user, login_user, whoami, test_client)

    trig = _create_trigger(test_client, owner_token, org_id).json()

    # Member's list is empty; owner's has one
    assert test_client.get("/api/triggers", headers=_headers(member_token, org_id)).json() == []
    assert len(test_client.get("/api/triggers", headers=_headers(owner_token, org_id)).json()) == 1

    # Member cannot read runs / update / rotate / delete the owner's trigger
    h = _headers(member_token, org_id)
    assert test_client.get(f"/api/triggers/{trig['id']}/runs", headers=h).status_code == 404
    assert test_client.put(f"/api/triggers/{trig['id']}", json={"name": "hax"}, headers=h).status_code == 404
    assert test_client.post(f"/api/triggers/{trig['id']}/rotate", headers=h).status_code == 404
    assert test_client.delete(f"/api/triggers/{trig['id']}", headers=h).status_code == 404


@pytest.mark.e2e
def test_trigger_rejects_inaccessible_agents(
    create_user, login_user, whoami, test_client,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    resp = _create_trigger(test_client, token, org_id,
                           data_source_ids=[str(uuid.uuid4())])
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Delivery → spawn pipeline
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_trigger_delivery_spawns_session(
    monkeypatch, create_user, login_user, whoami, test_client,
    create_data_source, dynamic_sqlite_db, get_reports, get_completions,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    ds = _make_sqlite_ds(create_data_source, dynamic_sqlite_db, token, org_id)
    trig = _create_trigger(test_client, token, org_id,
                           data_source_ids=[ds["id"]]).json()

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _deliver(trig["id"], {
        "type": "alert", "title": "Checkout latency spike",
        "service": "checkout", "severity": "P1",
    }, "d-spawn-1")

    # A fresh session exists, stamped with the trigger id, agents attached
    spawned = _find_spawned_reports(get_reports, token, org_id, trig["id"])
    assert len(spawned) == 1, "expected exactly one spawned session"
    report = spawned[0]
    assert "Checkout latency spike" in report["title"]
    assert [d["id"] for d in report["data_sources"]] == [ds["id"]]

    # The visible event entry landed in the spawned session
    completions = get_completions(report_id=report["id"], user_token=token, org_id=org_id)
    events = [c for c in completions if c.get("role") == "external"]
    assert len(events) == 1

    # The agent run got the task template + untrusted-data envelope + run spec
    assert len(calls) == 1
    run = calls[0]
    assert run["report_id"] == report["id"]
    assert "<task>Investigate the alert and summarize findings.</task>" in run["content"]
    assert "<inbound_event" in run["content"] and "Checkout latency spike" in run["content"]
    assert run["mode"] == "chat"
    assert run["webhook_id"] == trig["id"]

    # Run history endpoint reflects it
    runs = test_client.get(f"/api/triggers/{trig['id']}/runs", headers=_headers(token, org_id)).json()
    assert runs["total"] == 1
    assert runs["runs"][0]["report_id"] == report["id"]
    assert "Checkout latency spike" in (runs["runs"][0]["event_summary"] or runs["runs"][0]["title"])

    # The owner got an in-app notification linking to the spawned session
    notifs = test_client.get("/api/notifications", headers=_headers(token, org_id)).json()
    trig_notifs = [n for n in notifs["items"] if n.get("source") == "trigger"]
    assert len(trig_notifs) == 1
    assert trig["name"] in trig_notifs[0]["title"]
    assert trig_notifs[0]["link"] == f"/reports/{report['id']}"


@pytest.mark.e2e
def test_trigger_delivery_dedup(
    monkeypatch, create_user, login_user, whoami, test_client,
    get_reports,
):
    """Same delivery id twice → one spawned session, not two."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id).json()

    calls = []
    _stub_agent_run(monkeypatch, calls)
    payload = {"type": "alert", "title": "Dup alert"}
    _deliver(trig["id"], payload, "d-dup-1")
    _deliver(trig["id"], payload, "d-dup-1")

    assert len(_find_spawned_reports(get_reports, token, org_id, trig["id"])) == 1
    assert len(calls) == 1

    # A different delivery id spawns a second, independent session
    _deliver(trig["id"], payload, "d-dup-2")
    assert len(_find_spawned_reports(get_reports, token, org_id, trig["id"])) == 2


@pytest.mark.e2e
def test_classifier_decline_leaves_no_orphan_report(
    monkeypatch, create_user, login_user, whoami, test_client, get_reports,
):
    """Classification happens BEFORE spawning: a declined event creates nothing."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id, classify_enabled=True).json()

    calls = []
    _stub_agent_run(monkeypatch, calls)

    # Stub the classifier plumbing: a model "exists", classify declines
    from app.services.llm_service import LLMService
    from app.ai.classifiers.webhook_classifier import WebhookClassifier, Decision

    async def fake_get_default_model(self, db, organization, user, is_small=False):
        return object()

    monkeypatch.setattr(LLMService, "get_default_model", fake_get_default_model)
    monkeypatch.setattr(WebhookClassifier, "__init__", lambda self, model, usage_session_maker=None: None)

    async def decline(self, **kw):
        return Decision(act=False, confidence=0.9, reason="noise", task=None)

    monkeypatch.setattr(WebhookClassifier, "classify", decline)
    _deliver(trig["id"], {"type": "heartbeat", "title": "ping"}, "d-decline-1")
    assert _find_spawned_reports(get_reports, token, org_id, trig["id"]) == []
    assert calls == []

    # Same trigger, classifier now acts → session spawns, classifier task is
    # NOT used because the template wins
    async def act(self, **kw):
        return Decision(act=True, confidence=0.9, reason="real alert", task="classifier-authored task")

    monkeypatch.setattr(WebhookClassifier, "classify", act)
    _deliver(trig["id"], {"type": "alert", "title": "real one"}, "d-decline-2")
    spawned = _find_spawned_reports(get_reports, token, org_id, trig["id"])
    assert len(spawned) == 1
    assert len(calls) == 1
    assert "<task>Investigate the alert and summarize findings.</task>" in calls[0]["content"]


@pytest.mark.e2e
def test_classifier_enabled_without_model_skips_delivery(
    monkeypatch, create_user, login_user, whoami, test_client, get_reports,
):
    """No LLM configured + classifier on → delivery skipped safely, no orphans."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id, classify_enabled=True).json()
    calls = []
    _stub_agent_run(monkeypatch, calls)
    _deliver(trig["id"], {"type": "alert", "title": "no model"}, "d-nomodel-1")
    assert _find_spawned_reports(get_reports, token, org_id, trig["id"]) == []
    assert calls == []


# ---------------------------------------------------------------------------
# Receiver route auth (status codes; heavy work is backgrounded)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_receiver_auth_for_triggers(
    monkeypatch, create_user, login_user, whoami, test_client,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id).json()
    calls = []
    _stub_agent_run(monkeypatch, calls)

    url = f"/webhooks/{trig['token']}"
    # Wrong bearer secret → 401
    r = test_client.post(url, json={"type": "alert"},
                         headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    # Unknown token → 404
    r = test_client.post("/webhooks/whk_nope", json={"type": "alert"})
    assert r.status_code == 404
    # Correct bearer secret → accepted
    r = test_client.post(url, json={"type": "alert", "title": "ok"},
                         headers={"Authorization": f"Bearer {trig['secret']}"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"

    # Deactivated trigger → 404
    test_client.put(f"/api/triggers/{trig['id']}", json={"is_active": False},
                    headers=_headers(token, org_id))
    r = test_client.post(url, json={"type": "alert"},
                         headers={"Authorization": f"Bearer {trig['secret']}"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fixed-report regression: report-bound webhooks behave exactly as before
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_report_bound_webhook_unchanged(
    monkeypatch, create_user, login_user, whoami, test_client,
    create_report, get_reports, get_completions,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    report = create_report(title="Bound report", user_token=token, org_id=org_id)

    resp = test_client.post(
        f"/api/reports/{report['id']}/webhooks",
        json={"name": "Bound hook", "source": "generic", "auth_mode": "token",
              "classify_enabled": False},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    wh = resp.json()
    assert wh["report_id"] == report["id"]

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _deliver(wh["id"], {"type": "alert", "title": "bound event"}, "d-bound-1")

    # Event landed in the BOUND report; nothing was spawned; no agent run
    # (classify disabled on a bound webhook = alert-only, the legacy behavior)
    completions = get_completions(report_id=report["id"], user_token=token, org_id=org_id)
    events = [c for c in completions if c.get("role") == "external"]
    assert len(events) == 1
    assert _find_spawned_reports(get_reports, token, org_id, wh["id"]) == []
    assert calls == []

    # Report-bound webhooks don't appear in the triggers list
    assert test_client.get("/api/triggers", headers=_headers(token, org_id)).json() == []
