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


def _stub_agent_run(monkeypatch, calls, status="success", error=None):
    """Replace the agent run with a recorder.

    It still writes the system completion the real service would, because the
    caller decides success or failure by reading that row back — a stub that
    returns bare None looks exactly like an agent that died before answering.
    """
    from app.models.completion import Completion
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
        db.add(Completion(
            prompt={"content": completion_data.prompt.content},
            completion={"content": "done", **({"error": error} if error else {})},
            model="stub", report_id=str(report_id), turn_index=0,
            role="system", status=status, user_id=str(current_user.id),
        ))
        await db.commit()
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


@pytest.mark.e2e
def test_trigger_auto_model_is_valid(
    create_user, login_user, whoami, test_client,
):
    """Auto model (no explicit pick) is a first-class option: model_id omitted
    or explicitly null must succeed and round-trip as null so the run engages
    the Auto router. Regression guard for the client sending the 'auto' UI
    sentinel as a literal model id — the server rightly 404s an unknown id, so
    the sentinel must never leave the client (see PromptBoxV2.getModel)."""
    token, org_id = _setup_user(create_user, login_user, whoami)

    # Omitted → Auto
    trig = _create_trigger(test_client, token, org_id)
    assert trig.status_code == 200, trig.json()
    tid = trig.json()["id"]
    assert trig.json()["model_id"] is None

    # Explicit null → Auto (still valid)
    trig2 = _create_trigger(test_client, token, org_id, name="Auto 2", model_id=None)
    assert trig2.status_code == 200, trig2.json()
    assert trig2.json()["model_id"] is None

    # The UI sentinel is NOT a model id — the server must reject it. This is
    # exactly the failure the client fix prevents by mapping 'auto' → null.
    bad = _create_trigger(test_client, token, org_id, name="Bad", model_id="auto")
    assert bad.status_code == 404, bad.json()

    # Update back to Auto (null) after the fact also round-trips.
    up = test_client.put(
        f"/api/triggers/{tid}",
        json={"model_id": None},
        headers=_headers(token, org_id),
    )
    assert up.status_code == 200, up.json()
    assert up.json()["model_id"] is None


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

    # Deactivated trigger → accepted-but-not-run. A 4xx here would make senders
    # (GitHub et al.) auto-disable the endpoint, so resuming later would
    # silently deliver nothing; the delivery is recorded on the trigger instead
    # so the owner can still see that events are arriving.
    calls.clear()
    test_client.put(f"/api/triggers/{trig['id']}", json={"is_active": False},
                    headers=_headers(token, org_id))
    r = test_client.post(url, json={"type": "alert", "title": "while paused"},
                         headers={"Authorization": f"Bearer {trig['secret']}"})
    assert r.status_code == 200
    assert r.json()["status"] == "captured"
    assert calls == []  # nothing ran

    # ...and it is visible to the owner as the trigger's last delivery.
    detail = test_client.get(f"/api/triggers/{trig['id']}", headers=_headers(token, org_id))
    assert detail.status_code == 200
    last_event = detail.json()["last_event"]
    assert last_event and last_event["acted"] is False
    assert last_event["raw"]["title"] == "while paused"
    # Credential-bearing headers are never echoed back.
    assert not any("authorization" in k.lower() for k in (last_event["headers"] or {}))

    # An unauthenticated delivery is still rejected outright — capture happens
    # only after verification.
    r = test_client.post(url, json={"type": "alert"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


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


# ---------------------------------------------------------------------------
# Setup ergonomics: the delivery URL is the credential by default, providers
# can validate the endpoint, and rotating it actually revokes access.
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_secret_url_is_the_default_and_needs_no_headers(
    monkeypatch, create_user, login_user, whoami, test_client,
):
    """Most senders (Intercom, Zapier, cron/curl) can only be given a URL — no
    custom headers — so an unspecified auth_mode must accept a bare POST."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    body = {"name": "Intercom", "task_template": "Summarize the contact.",
            "mode": "chat", "data_source_ids": []}
    trig = test_client.post("/api/triggers", json=body, headers=_headers(token, org_id)).json()
    assert trig["auth_mode"] == "url_token"

    _stub_agent_run(monkeypatch, [])
    r = test_client.post(f"/webhooks/{trig['token']}", json={"type": "contact.created"})
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "accepted"


@pytest.mark.e2e
def test_endpoint_probe_is_always_ok(create_user, login_user, whoami, test_client):
    """Providers HEAD/GET the URL before accepting it. Answering differently for
    a real token than a bogus one would leak which tokens exist, so both are 200
    and neither runs anything."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id).json()

    for method in ("head", "get"):
        for path in (f"/webhooks/{trig['token']}", "/webhooks/whk_does_not_exist"):
            r = getattr(test_client, method)(path)
            assert r.status_code == 200, (method, path, r.status_code)


@pytest.mark.e2e
def test_rotating_a_secret_url_revokes_the_old_one(
    create_user, login_user, whoami, test_client,
):
    """In url_token mode the URL *is* the credential, so rotation has to mint a
    new path token — rotating only the signing secret would leave a leaked URL
    working."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id, auth_mode="url_token").json()
    old_token = trig["token"]

    rotated = test_client.post(f"/api/triggers/{trig['id']}/rotate",
                               headers=_headers(token, org_id)).json()
    assert rotated["token"] != old_token
    assert test_client.post(f"/webhooks/{old_token}", json={"type": "x"}).status_code == 404
    assert test_client.post(f"/webhooks/{rotated['token']}", json={"type": "x"}).status_code == 200

    # HMAC mode keeps its URL stable (the sender is configured against it) and
    # only gets a fresh signing key.
    hm = _create_trigger(test_client, token, org_id, auth_mode="hmac").json()
    hm_rotated = test_client.post(f"/api/triggers/{hm['id']}/rotate",
                                  headers=_headers(token, org_id)).json()
    assert hm_rotated["token"] == hm["token"]
    assert hm_rotated["secret"] != hm["secret"]


# ---------------------------------------------------------------------------
# Project binding: a trigger filed into a project spawns its sessions there
# and shows up in that project's automations.
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_trigger_spawns_sessions_into_its_project(
    monkeypatch, create_user, login_user, whoami, test_client,
    create_project, get_reports,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    project = create_project(name="Revenue", user_token=token, org_id=org_id)

    trig = _create_trigger(test_client, token, org_id, project_id=project["id"]).json()
    assert trig["project_id"] == project["id"]
    assert trig["project_name"] == "Revenue"

    calls = []
    _stub_agent_run(monkeypatch, calls)
    _deliver(trig["id"], {"type": "alert", "title": "in project"}, "d-proj-1")

    spawned = _find_spawned_reports(get_reports, token, org_id, trig["id"])
    assert len(spawned) == 1
    assert spawned[0]["project_id"] == project["id"]

    # ...and the trigger itself is listed among the project's automations.
    autos = test_client.get(f"/api/projects/{project['id']}/automations",
                            headers=_headers(token, org_id)).json()
    trigger_items = [a for a in autos if a["kind"] == "trigger"]
    assert [a["id"] for a in trigger_items] == [trig["id"]]
    assert trigger_items[0]["label"] == "Alert trigger"
    assert trigger_items[0]["report_id"] is None


@pytest.mark.e2e
def test_trigger_project_can_be_changed_and_cleared(
    create_user, login_user, whoami, test_client, create_project,
):
    token, org_id = _setup_user(create_user, login_user, whoami)
    p1 = create_project(name="One", user_token=token, org_id=org_id)
    p2 = create_project(name="Two", user_token=token, org_id=org_id)
    trig = _create_trigger(test_client, token, org_id, project_id=p1["id"]).json()

    moved = test_client.put(f"/api/triggers/{trig['id']}", json={"project_id": p2["id"]},
                            headers=_headers(token, org_id)).json()
    assert moved["project_id"] == p2["id"]

    cleared = test_client.put(f"/api/triggers/{trig['id']}", json={"project_id": ""},
                              headers=_headers(token, org_id)).json()
    assert cleared["project_id"] is None

    # Omitting the field leaves the binding alone.
    untouched = test_client.put(f"/api/triggers/{trig['id']}", json={"name": "Renamed"},
                                headers=_headers(token, org_id)).json()
    assert untouched["project_id"] is None


@pytest.mark.e2e
def test_trigger_rejects_a_project_the_user_cannot_see(
    create_user, login_user, whoami, test_client, create_project,
):
    """Filing a trigger into someone else's private project must 404 — the same
    no-existence-leak rule the project routes use."""
    owner_token, member_token, org_id = _setup_owner_and_member(
        create_user, login_user, whoami, test_client)
    private = create_project(name="Owner only", user_token=owner_token, org_id=org_id)

    resp = _create_trigger(test_client, member_token, org_id, project_id=private["id"])
    assert resp.status_code == 404


@pytest.mark.e2e
def test_active_trigger_with_no_task_records_without_running(
    monkeypatch, create_user, login_user, whoami, test_client,
):
    """A trigger is active from creation so its URL works during setup. With no
    task and no classifier there is nothing to instruct the agent with, so the
    delivery is recorded (that is how the UI confirms the URL) but no empty run
    is spawned. Adding a task makes the same delivery run for real."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = test_client.post(
        "/api/triggers",
        json={"name": "", "auth_mode": "url_token", "is_active": True},
        headers=_headers(token, org_id),
    ).json()
    assert trig["is_active"] is True

    calls = []
    _stub_agent_run(monkeypatch, calls)
    r = test_client.post(f"/webhooks/{trig['token']}", json={"type": "alert", "title": "too early"})
    assert r.status_code == 200
    assert r.json()["status"] == "captured"
    assert calls == []

    # The delivery is still visible to the owner, which is the point.
    detail = test_client.get(f"/api/triggers/{trig['id']}", headers=_headers(token, org_id)).json()
    assert detail["last_event"]["raw"]["title"] == "too early"

    # Once it has a task, deliveries run.
    test_client.put(f"/api/triggers/{trig['id']}", json={"task_template": "Summarize the alert."},
                    headers=_headers(token, org_id))
    r2 = test_client.post(f"/webhooks/{trig['token']}", json={"type": "alert", "title": "now"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "accepted"


@pytest.mark.e2e
def test_trigger_list_reports_the_last_run_verdict(
    monkeypatch, create_user, login_user, whoami, test_client,
):
    """A trigger whose sessions are failing must show it in the list, and
    recover once a later delivery runs clean."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id).json()

    def _listed():
        items = test_client.get("/api/triggers", headers=_headers(token, org_id)).json()
        return next(t for t in items if t["id"] == trig["id"])

    assert _listed()["last_run_status"] is None, "never fired → nothing to report"

    _stub_agent_run(monkeypatch, [], status="error", error={"code": "auth"})
    _deliver(trig["id"], {"type": "alert", "title": "Bad run"}, "d-verdict-1")
    assert _listed()["last_run_status"] == "error"

    _stub_agent_run(monkeypatch, [], status="success")
    _deliver(trig["id"], {"type": "alert", "title": "Good run"}, "d-verdict-2")
    assert _listed()["last_run_status"] == "success"


@pytest.mark.e2e
def test_trigger_runs_carry_each_run_verdict(
    monkeypatch, create_user, login_user, whoami, test_client,
):
    """The runs column distinguishes a session that answered from one that died."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    trig = _create_trigger(test_client, token, org_id).json()

    _stub_agent_run(monkeypatch, [], status="error", error={"code": "auth"})
    _deliver(trig["id"], {"type": "alert", "title": "First"}, "d-runs-1")
    _stub_agent_run(monkeypatch, [], status="success")
    _deliver(trig["id"], {"type": "alert", "title": "Second"}, "d-runs-2")

    runs = test_client.get(f"/api/triggers/{trig['id']}/runs",
                           headers=_headers(token, org_id)).json()["runs"]
    assert [r["status"] for r in runs] == ["success", "error"], "newest first"
