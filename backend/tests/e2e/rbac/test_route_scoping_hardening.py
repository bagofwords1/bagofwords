"""Object-level scoping on routes whose ids the permission decorator can't see.

Each test pins one invariant: a caller authorized in their own report / org
cannot reach another user's (or another org's) object by supplying its id —
in the path, in a second path segment, or in the request body. Both the
denied path and the legitimate owner path are covered so a fix that simply
breaks the route can't pass.

Objects that only the agent loop produces (completions, steps, widgets,
visualizations, LLM models) are seeded directly in the DB; everything under
test goes through the real HTTP surface.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.completion import Completion
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.widget import Widget
from app.settings.config import settings as bow_settings


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def two_members(create_user, login_user, whoami, test_client):
    """An org admin and a plain member of the same org."""
    admin = create_user(email=f"owner_{uuid.uuid4().hex[:6]}@test.com")
    admin_token = login_user(admin["email"], admin["password"])
    admin_me = whoami(admin_token)
    org_id = admin_me["organizations"][0]["id"]

    member_email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    r = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_h(admin_token, org_id),
    )
    assert r.status_code == 200, r.text
    member = create_user(email=member_email)
    member_token = login_user(member["email"], member["password"])
    return {
        "org_id": org_id,
        "admin_token": admin_token, "admin_id": admin_me["id"],
        "member_token": member_token, "member_id": whoami(member_token)["id"],
    }


@pytest.fixture
def allow_multiple_orgs():
    flags = bow_settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations = saved


async def _seed_report_objects(report_id, org_id, user_id):
    """widget + query + step + visualization + system completion on a report."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        widget = Widget(title="w", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title="q", report_id=report_id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id)
        db.add(query)
        await db.flush()
        step = Step(title="s", slug=f"s-{suffix}", status="success",
                    widget_id=widget.id, query_id=query.id, code="secret_code()",
                    data={"rows": [{"a": 1}], "columns": [{"field": "a", "headerName": "a"}]},
                    data_model={"type": "table"})
        db.add(step)
        viz = Visualization(title="v", report_id=report_id, query_id=query.id, view={})
        db.add(viz)
        completion = Completion(prompt={"content": "x"}, completion={"content": "y"},
                                role="system", status="in_progress", report_id=report_id,
                                user_id=user_id)
        db.add(completion)
        await db.commit()
        return {"step_id": step.id, "viz_id": viz.id, "completion_id": completion.id,
                "widget_id": widget.id}


@pytest.fixture
def owner_report(two_members, create_report):
    """A private report owned by the admin, with seeded agent objects."""
    m = two_members
    report = create_report(title="Owner private", user_token=m["admin_token"], org_id=m["org_id"])
    objs = asyncio.run(_seed_report_objects(report["id"], m["org_id"], m["admin_id"]))
    return {**m, "report_id": report["id"], **objs}


# ── steps / visualizations ───────────────────────────────────────────────────

@pytest.mark.e2e
def test_step_read_requires_access_to_its_report(test_client, owner_report):
    o = owner_report
    url = f"/api/steps/{o['step_id']}"
    assert test_client.get(url, headers=_h(o["admin_token"], o["org_id"])).status_code == 200
    denied = test_client.get(url, headers=_h(o["member_token"], o["org_id"]))
    assert denied.status_code in (403, 404)
    assert "secret_code" not in denied.text


@pytest.mark.e2e
def test_visualization_read_and_patch_are_scoped_to_report_owner(test_client, owner_report):
    o = owner_report
    url = f"/api/visualizations/{o['viz_id']}"
    assert test_client.get(url, headers=_h(o["admin_token"], o["org_id"])).status_code == 200
    assert test_client.get(url, headers=_h(o["member_token"], o["org_id"])).status_code in (403, 404)

    patch = test_client.patch(url, json={"title": "hijacked"}, headers=_h(o["member_token"], o["org_id"]))
    assert patch.status_code in (403, 404)
    ok = test_client.patch(url, json={"title": "renamed"}, headers=_h(o["admin_token"], o["org_id"]))
    assert ok.status_code == 200 and ok.json()["title"] == "renamed"


# ── completions ──────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_only_the_run_owner_can_stop_or_read_plans(test_client, owner_report):
    o = owner_report
    for path in ("sigkill",):
        r = test_client.post(f"/api/completions/{o['completion_id']}/{path}",
                             headers=_h(o["member_token"], o["org_id"]))
        assert r.status_code == 404
    plans = test_client.get(f"/api/completions/{o['completion_id']}/plans",
                            headers=_h(o["member_token"], o["org_id"]))
    assert plans.status_code in (403, 404)

    ok = test_client.post(f"/api/completions/{o['completion_id']}/sigkill",
                          headers=_h(o["admin_token"], o["org_id"]))
    assert ok.status_code == 200


# ── objects addressed through a report the caller owns ───────────────────────

@pytest.mark.e2e
def test_scheduled_prompt_must_belong_to_the_report_in_the_path(
    test_client, owner_report, create_report, create_scheduled_prompt,
):
    o = owner_report
    sp = create_scheduled_prompt(o["report_id"], user_token=o["admin_token"], org_id=o["org_id"])
    mine = create_report(title="Member own", user_token=o["member_token"], org_id=o["org_id"])
    base = f"/api/reports/{mine['id']}/scheduled-prompts/{sp['id']}"
    h = _h(o["member_token"], o["org_id"])

    assert test_client.put(base, json={"cron_schedule": "0 1 * * *"}, headers=h).status_code == 404
    assert test_client.get(f"{base}/runs", headers=h).status_code == 404
    assert test_client.post(f"{base}/trigger", headers=h).status_code == 404
    assert test_client.delete(base, headers=h).status_code == 404
    # Still there for its owner.
    listed = test_client.get(f"/api/reports/{o['report_id']}/scheduled-prompts",
                             headers=_h(o["admin_token"], o["org_id"])).json()
    assert sp["id"] in {s["id"] for s in listed}


@pytest.mark.e2e
def test_listing_other_users_scheduled_prompts_requires_admin(test_client, two_members):
    m = two_members
    for f in ("shared", "all", "anything"):
        r = test_client.get("/api/scheduled-prompts", params={"filter": f},
                            headers=_h(m["member_token"], m["org_id"]))
        assert r.status_code == 403, f
    assert test_client.get("/api/scheduled-prompts", params={"filter": "my"},
                           headers=_h(m["member_token"], m["org_id"])).status_code == 200


@pytest.mark.e2e
def test_webhook_must_belong_to_the_report_in_the_path(test_client, owner_report, create_report):
    o = owner_report
    wh = test_client.post(f"/api/reports/{o['report_id']}/webhooks", json={"name": "hook"},
                          headers=_h(o["admin_token"], o["org_id"]))
    assert wh.status_code == 200, wh.text
    wh_id = wh.json()["id"]
    mine = create_report(title="Member own", user_token=o["member_token"], org_id=o["org_id"])
    base = f"/api/reports/{mine['id']}/webhooks/{wh_id}"
    h = _h(o["member_token"], o["org_id"])

    rotated = test_client.post(f"{base}/rotate", headers=h)
    assert rotated.status_code == 404
    assert test_client.put(base, json={"name": "x"}, headers=h).status_code == 404
    assert test_client.delete(base, headers=h).status_code == 404
    ok = test_client.post(f"/api/reports/{o['report_id']}/webhooks/{wh_id}/rotate",
                          headers=_h(o["admin_token"], o["org_id"]))
    assert ok.status_code == 200


# ── ids supplied in the request body ─────────────────────────────────────────

@pytest.mark.e2e
def test_create_query_requires_owning_the_target_report(test_client, owner_report, create_report):
    o = owner_report
    h = _h(o["member_token"], o["org_id"])
    denied = test_client.post("/api/queries", json={"title": "x", "report_id": o["report_id"]}, headers=h)
    assert denied.status_code == 404
    # A widget from another report can't be smuggled in under the caller's report.
    mine = create_report(title="Member own", user_token=o["member_token"], org_id=o["org_id"])
    smuggled = test_client.post("/api/queries", json={
        "title": "x", "report_id": mine["id"], "widget_id": o["widget_id"]}, headers=h)
    assert smuggled.status_code == 404
    ok = test_client.post("/api/queries", json={"title": "x", "report_id": mine["id"]}, headers=h)
    assert ok.status_code == 200, ok.text


@pytest.mark.e2e
def test_create_artifact_requires_owning_the_target_report(test_client, owner_report, create_report):
    o = owner_report
    body = {"mode": "page", "title": "x", "content": {"code": "<div/>"}}
    denied = test_client.post("/api/artifacts", json={**body, "report_id": o["report_id"]},
                              headers=_h(o["member_token"], o["org_id"]))
    assert denied.status_code == 404
    mine = create_report(title="Member own", user_token=o["member_token"], org_id=o["org_id"])
    ok = test_client.post("/api/artifacts", json={**body, "report_id": mine["id"]},
                          headers=_h(o["member_token"], o["org_id"]))
    assert ok.status_code == 200, ok.text


@pytest.mark.e2e
@pytest.mark.parametrize("path", [
    "../../../../../../etc/passwd",
    "pptx_previews/../../db/x.db",
    "/etc/hostname",
])
def test_slide_preview_never_reads_outside_the_previews_dir(test_client, two_members, create_report, path):
    m = two_members
    h = _h(m["member_token"], m["org_id"])
    report = create_report(title="Slides", user_token=m["member_token"], org_id=m["org_id"])
    created = test_client.post("/api/artifacts", json={
        "report_id": report["id"], "mode": "slides", "title": "s",
        "content": {"preview_images": [path]}}, headers=h)
    assert created.status_code == 200, created.text
    art_id = created.json()["id"]
    # Client-supplied preview paths are dropped on create...
    assert "preview_images" not in (created.json().get("content") or {})
    # ...and on update.
    patched = test_client.patch(f"/api/artifacts/{art_id}",
                                json={"content": {"preview_images": [path]}}, headers=h)
    assert patched.status_code == 200
    assert "preview_images" not in (patched.json().get("content") or {})
    r = test_client.get(f"/api/artifacts/{art_id}/preview/0", headers=h)
    assert r.status_code == 404
    assert "root:" not in r.text


# ── cross-organization ───────────────────────────────────────────────────────

@pytest.fixture
def foreign_org_admin(allow_multiple_orgs, two_members, create_organization):
    """The plain member of org A creates (and so administers) org B."""
    m = two_members
    org_b = create_organization(name=f"Org B {uuid.uuid4().hex[:6]}", user_token=m["member_token"])
    return {**m, "org_b": org_b}


@pytest.mark.e2e
def test_path_org_must_match_the_authorized_org(test_client, foreign_org_admin):
    f = foreign_org_admin
    victim, own = f["org_id"], f["org_b"]
    h = _h(f["member_token"], own)  # authorized as admin of org B

    roles = test_client.get(f"/api/organizations/{victim}/roles", headers=h)
    assert roles.status_code == 403
    grab = test_client.post(f"/api/organizations/{victim}/role-assignments", json={
        "role_id": "anything", "principal_type": "user", "principal_id": f["member_id"]}, headers=h)
    assert grab.status_code == 403
    invite = test_client.post(f"/api/organizations/{victim}/members", json={
        "organization_id": victim, "email": "x@example.com", "role": "admin"}, headers=h)
    assert invite.status_code == 403
    members = test_client.get(f"/api/organizations/{victim}/members", headers=h)
    assert members.status_code == 403
    # The same routes keep working for the org the caller is authorized in.
    assert test_client.get(f"/api/organizations/{own}/roles", headers=h).status_code == 200
    assert test_client.get(f"/api/organizations/{own}/members", headers=h).status_code == 200


async def _seed_model(org_id):
    async with async_session_maker() as db:
        provider = LLMProvider(organization_id=org_id, name="p", provider_type="anthropic",
                               is_preset=False, is_enabled=True, use_preset_credentials=False)
        db.add(provider)
        await db.flush()
        model = LLMModel(organization_id=org_id, provider_id=provider.id, name="m",
                         model_id=f"claude-test-{uuid.uuid4().hex[:6]}", is_preset=False,
                         is_enabled=True, is_default=True, is_small_default=True)
        db.add(model)
        await db.commit()
        return model.id


@pytest.mark.e2e
def test_set_default_model_is_scoped_to_the_callers_org(test_client, foreign_org_admin):
    f = foreign_org_admin
    victim_model = asyncio.run(_seed_model(f["org_id"]))
    r = test_client.post(f"/api/llm/models/{victim_model}/set_default",
                         headers=_h(f["member_token"], f["org_b"]))
    assert r.status_code == 404


@pytest.mark.e2e
def test_license_changes_need_an_instance_admin_on_multi_org(test_client, foreign_org_admin):
    f = foreign_org_admin
    h = _h(f["member_token"], f["org_b"])
    assert test_client.put("/api/license/key", json={"key": "x"}, headers=h).status_code == 403
    assert test_client.delete("/api/license/key", headers=h).status_code == 403


# ── settings writer ──────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("key,value", [
    ("signup_policy", {"auto_invite_role": "admin", "allowed_domains": ["evil.test"]}),
    ("entra_profile_sync", {"enabled": True}),
    ("google_profile_sync", {"enabled": True}),
    ("smtp", {"host": "evil.test"}),
])
def test_generic_settings_writer_cannot_set_gated_keys(test_client, two_members, key, value):
    m = two_members
    h = _h(m["admin_token"], m["org_id"])
    r = test_client.put("/api/organization/settings", json={"config": {key: value}}, headers=h)
    assert r.status_code == 403
    cfg = test_client.get("/api/organization/settings", headers=h).json()["config"]
    assert cfg.get(key) != value


@pytest.mark.e2e
def test_generic_settings_writer_still_saves_ordinary_keys(test_client, two_members):
    m = two_members
    h = _h(m["admin_token"], m["org_id"])
    name = f"Analyst {uuid.uuid4().hex[:4]}"
    r = test_client.put("/api/organization/settings",
                        json={"config": {"general": {"ai_analyst_name": name}}}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["general"]["ai_analyst_name"] == name
