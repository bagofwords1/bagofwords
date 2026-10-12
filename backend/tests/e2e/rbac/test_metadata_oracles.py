"""Small metadata leaks: what a caller learns about objects they can't access.

- Rollback of a build outside the caller's org reads exactly like a missing
  build (404, no agent name), whatever the caller's role.
- An eval run comparison never uses a baseline run the caller can't read.
- A project's default-agent list shows only agents the caller can see, and a
  save from that filtered list keeps the agents the caller was not shown.
- Instruction enhancement only feeds the model agents the caller can see.
- Removing or replacing an avatar stops serving the old file.
"""
import io
import os
import uuid
from datetime import datetime, timedelta

import pytest
from PIL import Image


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────
# Build rollback across orgs
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def foreign_build(test_client, bootstrap_admin, sqlite_data_source):
    """An approved build in org A that pins an instruction on a named agent."""
    owner = bootstrap_admin("owner")
    agent_name = f"SecretAgent_{uuid.uuid4().hex[:6]}"
    ds = sqlite_data_source(name=agent_name, user_token=owner["token"], org_id=owner["org_id"])
    r = test_client.post(
        "/api/instructions",
        json={"text": "rule on secret agent", "status": "published",
              "category": "general", "data_source_ids": [ds["id"]]},
        headers=_hdr(owner["token"], owner["org_id"]),
    )
    assert r.status_code == 200, r.text
    main = test_client.get("/api/builds/main", headers=_hdr(owner["token"], owner["org_id"]))
    assert main.status_code == 200, main.text
    return {"owner": owner, "build_id": main.json()["id"], "agent_name": agent_name}


@pytest.mark.e2e
@pytest.mark.parametrize("role", ["admin", "agent_manager"])
def test_rollback_of_foreign_build_reads_as_missing(
    test_client, foreign_build, bootstrap_admin, invite_user_to_org,
    sqlite_data_source, grant_resource, role,
):
    other = bootstrap_admin("other")
    caller = other
    if role == "agent_manager":
        ds = sqlite_data_source(name=f"own_{uuid.uuid4().hex[:6]}",
                                user_token=other["token"], org_id=other["org_id"])
        caller = invite_user_to_org(org_id=other["org_id"], admin_token=other["token"])
        g = grant_resource(resource_type="data_source", resource_id=ds["id"],
                           principal_type="user", principal_id=caller["user_id"],
                           permissions=["manage_instructions"],
                           user_token=other["token"], org_id=other["org_id"])
        assert g.status_code == 200, g.text

    hdr = _hdr(caller["token"], other["org_id"])
    foreign = test_client.post(f"/api/builds/{foreign_build['build_id']}/rollback", headers=hdr)
    missing = test_client.post(f"/api/builds/{uuid.uuid4()}/rollback", headers=hdr)

    assert foreign.status_code == 404, foreign.text
    assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json())
    assert foreign_build["agent_name"] not in foreign.text


@pytest.mark.e2e
def test_rollback_within_own_org_still_works(test_client, bootstrap_admin):
    admin = bootstrap_admin("rb")
    hdr = _hdr(admin["token"], admin["org_id"])
    for text in ("first", "second"):
        r = test_client.post("/api/instructions",
                             json={"text": text, "status": "published", "category": "general"},
                             headers=hdr)
        assert r.status_code == 200, r.text
    builds = test_client.get("/api/builds", params={"status": "all"}, headers=hdr).json()["items"]
    main_id = test_client.get("/api/builds/main", headers=hdr).json()["id"]
    target = next(b for b in builds if b["id"] != main_id and b["status"] == "approved")

    r = test_client.post(f"/api/builds/{target['id']}/rollback", headers=hdr)
    assert r.status_code == 200, r.text
    assert test_client.get("/api/builds/main", headers=hdr).json()["id"] == r.json()["id"]


# ────────────────────────────────────────────────────────────────────
# Eval run comparison baseline
# ────────────────────────────────────────────────────────────────────


def _seed_run(report_id, suite_id, case_statuses, created_at):
    """Insert a finished TestRun with one result per case.

    Direct DB write (tests/AGENTS.md rule 5 exception): results only come from
    executing a live eval run, which crosses the LLM boundary.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models.completion import Completion
    from app.models.eval import TestRun, TestResult

    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with Session(engine) as s:
            run = TestRun(title=f"run {uuid.uuid4().hex[:6]}", suite_ids=str(suite_id),
                          status="success", created_at=created_at)
            s.add(run)
            s.flush()
            for case_id, status in case_statuses.items():
                head = Completion(prompt={"content": "p"}, completion={"content": ""},
                                  role="user", message_type="user_message", report_id=report_id)
                s.add(head)
                s.flush()
                s.add(TestResult(run_id=run.id, case_id=str(case_id),
                                 head_completion_id=head.id, status=status))
            s.commit()
            return str(run.id)
    finally:
        engine.dispose()


@pytest.fixture
def eval_world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
               grant_resource, create_test_suite, create_test_case, create_report):
    admin = bootstrap_admin("ev")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"ea_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"eb_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=ds_a["id"],
                       principal_type="user", principal_id=manager["user_id"],
                       permissions=["manage_evals"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.text

    suite = create_test_suite(name="cmp", user_token=admin["token"], org_id=org_id)
    case_a = create_test_case(suite_id=suite["id"], name="case-a", data_source_ids_json=[ds_a["id"]],
                              user_token=admin["token"], org_id=org_id)
    case_b = create_test_case(suite_id=suite["id"], name="case-b", data_source_ids_json=[ds_b["id"]],
                              user_token=admin["token"], org_id=org_id)
    report = create_report(title="eval", user_token=admin["token"], org_id=org_id)
    t0 = datetime.utcnow() - timedelta(hours=2)
    # Readable to the manager (only case A).
    older_visible = _seed_run(report["id"], suite["id"], {case_a["id"]: "fail"}, t0)
    # Shares case A but also ran case B, which the manager can't see.
    hidden = _seed_run(report["id"], suite["id"],
                       {case_a["id"]: "pass", case_b["id"]: "fail"}, t0 + timedelta(minutes=30))
    current = _seed_run(report["id"], suite["id"], {case_a["id"]: "pass"}, t0 + timedelta(hours=1))
    return {"org_id": org_id, "admin": admin, "manager": manager,
            "older_visible": older_visible, "hidden": hidden, "current": current}


@pytest.mark.e2e
def test_compare_default_baseline_skips_runs_caller_cannot_read(test_client, eval_world):
    w = eval_world
    r = test_client.get(f"/api/tests/runs/{w['current']}/compare",
                        headers=_hdr(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["against_run"]["id"] == w["older_visible"]
    assert "case-b" not in r.text


@pytest.mark.e2e
def test_compare_default_baseline_unchanged_for_org_eval_admin(test_client, eval_world):
    w = eval_world
    r = test_client.get(f"/api/tests/runs/{w['current']}/compare",
                        headers=_hdr(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    assert r.json()["against_run"]["id"] == w["hidden"]


@pytest.mark.e2e
def test_get_eval_run_tool_compare_uses_only_readable_baseline(test_client, eval_world):
    """The training-mode tool goes through the same comparison."""
    import asyncio
    from app.ai.tools.implementations.get_eval_run import GetEvalRunTool
    from app.models.organization import Organization
    from app.models.user import User
    from app.settings.database import create_async_session_factory

    w = eval_world

    async def _run():
        async with create_async_session_factory()() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["manager"]["user_id"])
            events = [e async for e in GetEvalRunTool().run_stream(
                {"run_id": w["current"], "compare_to_previous": True},
                {"db": db, "organization": org, "user": user},
            )]
        return events[-1]

    end = asyncio.run(_run())
    assert end.type == "tool.end", end
    compare = end.payload["output"]["compare"]
    assert compare["against_run"]["id"] == w["older_visible"]


# ────────────────────────────────────────────────────────────────────
# Project default agents
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_project_lists_only_default_agents_caller_can_see(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
    grant_resource, create_project, update_project,
):
    admin = bootstrap_admin("pj")
    org_id = admin["org_id"]
    seen = sqlite_data_source(name=f"seen_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    hidden = sqlite_data_source(name=f"hidden_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=seen["id"],
                       principal_type="user", principal_id=member["user_id"],
                       permissions=["view"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.text

    project = create_project(name="shared", user_token=admin["token"], org_id=org_id)
    update_project(project["id"], user_token=admin["token"], org_id=org_id, access="org")
    r = test_client.put(f"/api/projects/{project['id']}/data_sources",
                        json={"data_source_ids": [seen["id"], hidden["id"]]},
                        headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text

    as_member = test_client.get(f"/api/projects/{project['id']}", headers=_hdr(member["token"], org_id))
    assert as_member.status_code == 200, as_member.text
    assert {d["id"] for d in as_member.json()["data_sources"]} == {seen["id"]}
    assert hidden["name"] not in as_member.text

    as_admin = test_client.get(f"/api/projects/{project['id']}", headers=_hdr(admin["token"], org_id))
    assert {d["id"] for d in as_admin.json()["data_sources"]} == {seen["id"], hidden["id"]}


@pytest.mark.e2e
def test_project_default_save_keeps_agents_caller_was_not_shown(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
    grant_resource, create_project, upsert_project_member,
):
    admin = bootstrap_admin("pk")
    org_id = admin["org_id"]
    a = sqlite_data_source(name=f"a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    b = sqlite_data_source(name=f"b_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    c = sqlite_data_source(name=f"c_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    mgr = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grants = {}
    for ds in (a, b, c):
        g = grant_resource(resource_type="data_source", resource_id=ds["id"],
                           principal_type="user", principal_id=mgr["user_id"],
                           permissions=["view"], user_token=admin["token"], org_id=org_id)
        assert g.status_code == 200, g.text
        grants[ds["id"]] = g.json()["id"]

    project = create_project(name="defaults", user_token=admin["token"], org_id=org_id)
    upsert_project_member(project["id"], mgr["user_id"], permissions=["manage"],
                          user_token=admin["token"], org_id=org_id)
    r = test_client.put(f"/api/projects/{project['id']}/data_sources",
                        json={"data_source_ids": [a["id"], b["id"]]},
                        headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text
    # The manager later loses access to B.
    d = test_client.delete(f"/api/organizations/{org_id}/resource-grants/{grants[b['id']]}",
                           headers=_hdr(admin["token"], org_id))
    assert d.status_code == 204, d.text

    shown = test_client.get(f"/api/projects/{project['id']}", headers=_hdr(mgr["token"], org_id)).json()
    shown_ids = [x["id"] for x in shown["data_sources"]]
    assert shown_ids == [a["id"]]
    # Their UI saves what it was shown plus a new pick.
    r = test_client.put(f"/api/projects/{project['id']}/data_sources",
                        json={"data_source_ids": shown_ids + [c["id"]]},
                        headers=_hdr(mgr["token"], org_id))
    assert r.status_code == 200, r.text
    assert {x["id"] for x in r.json()["data_sources"]} == {a["id"], c["id"]}

    as_admin = test_client.get(f"/api/projects/{project['id']}", headers=_hdr(admin["token"], org_id))
    assert {x["id"] for x in as_admin.json()["data_sources"]} == {a["id"], b["id"], c["id"]}


# ────────────────────────────────────────────────────────────────────
# Instruction enhance
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def captured_enhance(monkeypatch):
    """Stub the LLM boundary and capture the agent context it is handed."""
    import app.services.instruction_service as svc_mod
    from app.services.llm_service import LLMService

    seen = {}

    class _FakeSuggest:
        def __init__(self, *a, **k):
            pass

        async def enhance_instruction(self, text, instructions_ctx, ds_ctx):
            seen["ds_context"] = ds_ctx
            return {"enhanced_instruction": f"better: {text}"}

    async def _model(*a, **k):
        return object()

    monkeypatch.setattr(svc_mod, "SuggestInstructions", _FakeSuggest)
    monkeypatch.setattr(LLMService, "get_default_model", _model)
    return seen


@pytest.mark.e2e
def test_enhance_uses_only_agents_caller_can_see(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
    grant_resource, captured_enhance,
):
    admin = bootstrap_admin("en")
    org_id = admin["org_id"]
    mine = sqlite_data_source(name=f"Mine_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    theirs = sqlite_data_source(name=f"Theirs_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    mgr = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=mine["id"],
                       principal_type="user", principal_id=mgr["user_id"],
                       permissions=["manage_instructions"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.text

    r = test_client.post("/api/instructions/enhance",
                         json={"text": "use fiscal year", "data_source_ids": [mine["id"], theirs["id"]]},
                         headers=_hdr(mgr["token"], org_id))
    assert r.status_code == 200, r.text
    assert theirs["name"] not in r.text
    assert mine["name"] in captured_enhance["ds_context"]
    assert theirs["name"] not in captured_enhance["ds_context"]

    # Admin still gets both.
    r = test_client.post("/api/instructions/enhance",
                         json={"text": "use fiscal year", "data_source_ids": [mine["id"], theirs["id"]]},
                         headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text
    assert mine["name"] in captured_enhance["ds_context"]
    assert theirs["name"] in captured_enhance["ds_context"]


# ────────────────────────────────────────────────────────────────────
# Avatar lifecycle
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_avatar_old_url_stops_serving_after_replace_and_remove(test_client, bootstrap_admin):
    u = bootstrap_admin("av")
    hdr = {"Authorization": f"Bearer {u['token']}"}

    def upload(color):
        r = test_client.post("/api/users/me/avatar",
                             files={"avatar": ("a.png", _png(color), "image/png")}, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()["image_url"]

    first = upload("red")
    assert test_client.get(first).status_code == 200
    # Re-uploading the same image keeps the same key and keeps serving it.
    assert upload("red") == first
    assert test_client.get(first).status_code == 200

    second = upload("blue")
    assert second != first
    assert test_client.get(second).status_code == 200
    assert test_client.get(first).status_code == 404

    r = test_client.delete("/api/users/me/avatar", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["image_url"] is None
    assert test_client.get(second).status_code == 404


@pytest.mark.e2e
def test_avatar_change_never_touches_another_users_file(test_client, bootstrap_admin):
    a = bootstrap_admin("ava")
    b = bootstrap_admin("avb")
    r = test_client.post("/api/users/me/avatar",
                         files={"avatar": ("a.png", _png("green"), "image/png")},
                         headers={"Authorization": f"Bearer {a['token']}"})
    a_url = r.json()["image_url"]
    for color in ("green", "yellow"):
        test_client.post("/api/users/me/avatar",
                         files={"avatar": ("b.png", _png(color), "image/png")},
                         headers={"Authorization": f"Bearer {b['token']}"})
    test_client.delete("/api/users/me/avatar", headers={"Authorization": f"Bearer {b['token']}"})
    assert test_client.get(a_url).status_code == 200
