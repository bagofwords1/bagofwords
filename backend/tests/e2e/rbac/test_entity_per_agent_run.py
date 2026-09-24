"""A saved query shared with several agents runs against the agent it is run
FROM, never against whichever agent its code happened to be written for.

The query's code is written once, inside one agent (it names that agent's
client, `ds_clients["<agent>:<connection>"]`). Sharing it with another agent
must make it run on THAT agent's connection of the same type, keep a separate
result per agent, and gate running on the agent run from alone.

Run:
    cd backend && uv run pytest tests/e2e/rbac/test_entity_per_agent_run.py -m e2e -v
"""
import os
import sqlite3
import tempfile
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _store_db(store):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sales (store TEXT, amount REAL)")
    conn.executemany("INSERT INTO sales VALUES (?, ?)", [(store, 10.0), (store, 32.0)])
    conn.commit()
    conn.close()
    return path


def _client_key(test_client, ds, admin, org_id):
    """The key agent-generated code uses for this agent's only connection."""
    resp = test_client.get(f"/api/data_sources/{ds['id']}", headers=_hdr(admin["token"], org_id))
    assert resp.status_code == 200, resp.text
    conns = resp.json().get("connections") or []
    assert len(conns) == 1, conns
    return f"{ds['name']}:{conns[0]['name']}"


def _code_for(key):
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients[{key!r}].execute_query(\"SELECT store, SUM(amount) AS total FROM sales GROUP BY store\")\n"
    )


@pytest.fixture
def shared(test_client, bootstrap_admin, sqlite_data_source, invite_user_to_org, grant_resource):
    """Two agents of the same connection type over different databases, and a
    published query written in the first one and shared with both."""
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    stores = [f"S{uuid.uuid4().hex[:4]}", f"S{uuid.uuid4().hex[:4]}"]
    paths = [_store_db(s) for s in stores]
    origin = sqlite_data_source(name=f"a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id, database=paths[0])
    other = sqlite_data_source(name=f"b_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id, database=paths[1])
    code = _code_for(_client_key(test_client, origin, admin, org_id))
    resp = test_client.post(
        "/api/entities/global",
        json={
            "type": "model", "title": f"Sales {uuid.uuid4().hex[:4]}", "slug": f"sales-{uuid.uuid4().hex[:8]}",
            "code": code, "data": {}, "status": "published",
            "data_source_ids": [origin["id"], other["id"]],
            "origin_data_source_id": origin["id"],
        },
        headers=_hdr(admin["token"], org_id),
    )
    assert resp.status_code == 200, resp.text
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    yield {
        "admin": admin, "org_id": org_id, "member": member, "grant": grant_resource,
        "origin": origin, "other": other, "stores": {origin["id"]: stores[0], other["id"]: stores[1]},
        "entity": resp.json(),
    }
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _stores(resp):
    return {r["store"] for r in resp.json()["data"]["rows"]}


def _run(test_client, w, ds_id, token=None, **body):
    return test_client.post(
        f"/api/entities/{w['entity']['id']}/run",
        json={"data_source_id": ds_id, **body},
        headers=_hdr(token or w["admin"]["token"], w["org_id"]),
    )


@pytest.mark.e2e
def test_query_runs_against_the_agent_it_is_run_from(test_client, shared):
    w = shared
    for ds in (w["origin"], w["other"]):
        resp = _run(test_client, w, ds["id"])
        assert resp.status_code == 200, resp.text
        assert _stores(resp) == {w["stores"][ds["id"]]}


@pytest.mark.e2e
def test_each_agent_keeps_its_own_result(test_client, shared):
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    assert _run(test_client, w, w["origin"]["id"]).status_code == 200
    assert _run(test_client, w, w["other"]["id"]).status_code == 200

    for ds in (w["origin"], w["other"]):
        got = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": ds["id"]}, headers=h)
        assert got.status_code == 200, got.text
        assert _stores(got) == {w["stores"][ds["id"]]}


@pytest.mark.e2e
def test_stored_code_names_no_agent(test_client, shared):
    w = shared
    code = w["entity"]["code"]
    for ds in (w["origin"], w["other"]):
        assert ds["name"] not in code


@pytest.mark.e2e
def test_member_reads_the_result_of_the_agent_they_can_reach(test_client, shared):
    """Access to one of a shared query's agents is enough to read it there —
    and only there."""
    w = shared
    h = _hdr(w["member"]["token"], w["org_id"])
    for ds in (w["origin"], w["other"]):
        assert _run(test_client, w, ds["id"]).status_code == 200
    w["grant"](resource_type="data_source", resource_id=w["other"]["id"], principal_type="user",
               principal_id=w["member"]["user_id"], permissions=["access"], user_token=w["admin"]["token"], org_id=w["org_id"])

    reachable = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["other"]["id"]}, headers=h)
    assert reachable.status_code == 200, reachable.text
    assert _stores(reachable) == {w["stores"][w["other"]["id"]]}

    default = test_client.get(f"/api/entities/{w['entity']['id']}", headers=h)
    assert default.status_code == 200, default.text
    assert _stores(default) == {w["stores"][w["other"]["id"]]}, "defaulted to an agent the member cannot reach"

    unreachable = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["origin"]["id"]}, headers=h)
    assert unreachable.status_code in (403, 404), unreachable.text


@pytest.mark.e2e
def test_agent_manager_refreshes_only_their_agent(test_client, shared):
    """Refreshing a shared result needs create_entities on the agent it is
    refreshed on — not on every agent the query is shared with."""
    w = shared
    token = w["member"]["token"]
    w["grant"](resource_type="data_source", resource_id=w["other"]["id"], principal_type="user",
               principal_id=w["member"]["user_id"], permissions=["access", "create_entities"],
               user_token=w["admin"]["token"], org_id=w["org_id"])

    mine = _run(test_client, w, w["other"]["id"], token=token)
    assert mine.status_code == 200, mine.text
    assert _stores(mine) == {w["stores"][w["other"]["id"]]}

    theirs = _run(test_client, w, w["origin"]["id"], token=token)
    assert theirs.status_code in (403, 404), theirs.text


@pytest.mark.e2e
def test_member_sees_shared_query_under_the_agent_they_can_reach(test_client, shared):
    w = shared
    h = _hdr(w["member"]["token"], w["org_id"])
    w["grant"](resource_type="data_source", resource_id=w["other"]["id"], principal_type="user",
               principal_id=w["member"]["user_id"], permissions=["access"], user_token=w["admin"]["token"], org_id=w["org_id"])

    listed = test_client.get("/api/entities", params={"data_source_ids": w["other"]["id"]}, headers=h)
    assert listed.status_code == 200, listed.text
    assert w["entity"]["id"] in {e["id"] for e in listed.json()}

    detail = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["other"]["id"]}, headers=h)
    assert detail.status_code == 200, detail.text


@pytest.mark.e2e
def test_editing_still_needs_every_attached_agent(test_client, shared):
    w = shared
    w["grant"](resource_type="data_source", resource_id=w["other"]["id"], principal_type="user",
               principal_id=w["member"]["user_id"], permissions=["access", "create_entities"],
               user_token=w["admin"]["token"], org_id=w["org_id"])
    resp = test_client.put(
        f"/api/entities/{w['entity']['id']}", json={"title": "renamed by a one-agent manager"},
        headers=_hdr(w["member"]["token"], w["org_id"]),
    )
    assert resp.status_code in (403, 404), resp.text


@pytest.mark.e2e
def test_renaming_an_agent_does_not_break_its_queries(test_client, shared):
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    renamed = test_client.put(f"/api/data_sources/{w['origin']['id']}", json={"name": f"renamed_{uuid.uuid4().hex[:6]}"}, headers=h)
    assert renamed.status_code == 200, renamed.text
    resp = _run(test_client, w, w["origin"]["id"])
    assert resp.status_code == 200, resp.text
    assert _stores(resp) == {w["stores"][w["origin"]["id"]]}


@pytest.mark.e2e
def test_deleting_the_origin_agent_hands_the_query_to_the_next_agent(test_client, shared):
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    assert _run(test_client, w, w["other"]["id"]).status_code == 200
    gone = test_client.delete(f"/api/data_sources/{w['origin']['id']}", headers=h)
    assert gone.status_code in (200, 204), gone.text

    got = test_client.get(f"/api/entities/{w['entity']['id']}", headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["origin_data_source_id"] == w["other"]["id"]
    assert _stores(got) == {w["stores"][w["other"]["id"]]}

    rerun = test_client.post(f"/api/entities/{w['entity']['id']}/run", json={}, headers=h)
    assert rerun.status_code == 200, rerun.text
    assert _stores(rerun) == {w["stores"][w["other"]["id"]]}


DYNAMIC_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.concat([c.execute_query(\"SELECT store, SUM(amount) AS total FROM sales GROUP BY store\")\n"
    "                      for k, c in sorted(ds_clients.items()) if ':' in k and not k.endswith('::fast')])\n"
)


@pytest.mark.e2e
def test_query_without_an_agent_does_not_run_against_the_whole_org(test_client, shared, sqlite_data_source):
    """Code run with no agent chosen must not fall back to reading every
    agent in the organization. Uses code that reaches `ds_clients` without
    naming an agent, which is exactly what such a fallback silently feeds."""
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    bystander_path = _store_db("BYSTANDER")
    try:
        sqlite_data_source(name=f"c_{uuid.uuid4().hex[:6]}", user_token=w["admin"]["token"], org_id=w["org_id"], database=bystander_path)
        resp = test_client.post("/api/entities/preview", json={"code": DYNAMIC_CODE, "data_source_ids": []}, headers=h)
        if resp.status_code == 200 and (resp.json().get("data") or {}).get("rows"):
            assert "BYSTANDER" not in _stores(resp), "code run with no agent read an unrelated agent"
        else:
            assert resp.status_code >= 400 or not (resp.json().get("data") or {}).get("rows"), resp.text
    finally:
        os.unlink(bystander_path)


def _async(coro_fn):
    import asyncio
    return asyncio.run(coro_fn())


@pytest.mark.e2e
def test_describe_entity_hands_the_report_code_for_its_own_agent(test_client, shared):
    """A saved query materialized in a report of one agent carries THAT
    agent's client key, so the report re-runs it on its own agent later."""
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.tools.implementations.describe_entity import DescribeEntityTool
    from app.services.entity_code import client_keys

    w = shared
    for ds in (w["origin"], w["other"]):
        assert _run(test_client, w, ds["id"]).status_code == 200

    async def run():
        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["admin"]["user_id"])
            settings = await org.get_settings(db)
            events = [e async for e in DescribeEntityTool().run_stream(
                {"name_or_id": w["entity"]["id"], "should_create": True, "agent": w["other"]["name"]},
                {"db": db, "organization": org, "user": user, "settings": settings},
            )]
            return events[-1].payload["output"]

    out = _async(run)
    assert out["success"], out["errors"]
    keys, _ = client_keys(out["code"])
    assert keys and all(k.startswith(w["other"]["name"] + ":") for k in keys), keys
    assert {r["store"] for r in out["data"]["rows"]} == {w["stores"][w["other"]["id"]]}


@pytest.mark.e2e
def test_load_entity_serves_the_result_of_the_agent_in_play(test_client, shared):
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.code_execution.loadables import LoadablesResolver

    w = shared
    for ds in (w["origin"], w["other"]):
        assert _run(test_client, w, ds["id"]).status_code == 200

    async def resolve(agent_id):
        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["admin"]["user_id"])
            r = LoadablesResolver(db, org, None, user, run_agent_ids=[agent_id])
            out = await r.resolve([], [w["entity"]["id"]])
            assert not out["errors"], out["errors"]
            return set(out["entities"][w["entity"]["id"]]["store"])

    for ds in (w["origin"], w["other"]):
        assert _async(lambda: resolve(ds["id"])) == {w["stores"][ds["id"]]}


@pytest.mark.e2e
def test_the_model_never_sees_the_agent_token_or_another_agents_key(test_client, shared):
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.context.builders.entity_context_builder import EntityContextBuilder

    w = shared

    async def build(agent_id):
        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["admin"]["user_id"])
            section = await EntityContextBuilder(db, org, None, user=user).build(
                keywords=[], data_source_ids=[agent_id],
            )
            item = next(i for i in section.items if i.id == w["entity"]["id"])
            return item, section.render()

    for ds, other in ((w["origin"], w["other"]), (w["other"], w["origin"])):
        item, rendered = _async(lambda: build(ds["id"]))
        assert "$agent" not in (item.code or "")
        assert ds["name"] in (item.code or "")
        assert other["name"] not in (item.code or "")
        assert item.ds_names == [ds["name"]]
        assert other["name"] not in rendered


@pytest.mark.e2e
def test_sharing_runs_the_query_on_the_new_agent_at_once(test_client, shared):
    """An agent a query is shared with opens with its own rows — no refresh."""
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    got = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["other"]["id"]}, headers=h)
    assert got.status_code == 200, got.text
    assert _stores(got) == {w["stores"][w["other"]["id"]]}


@pytest.mark.e2e
def test_sharing_with_an_agent_the_code_fails_on_is_refused(test_client, shared, sqlite_data_source):
    """Same connection type, but the tables the query reads are not there:
    the share is refused with a typed error and nothing changes."""
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    fd, empty = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    sqlite3.connect(empty).close()
    try:
        bare = sqlite_data_source(name=f"e_{uuid.uuid4().hex[:6]}", user_token=w["admin"]["token"], org_id=w["org_id"], database=empty)
        ids = [w["origin"]["id"], w["other"]["id"], bare["id"]]
        resp = test_client.put(f"/api/entities/{w['entity']['id']}", json={"data_source_ids": ids}, headers=h)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error_code"] == "entity.share_run_failed"

        after = test_client.get(f"/api/entities/{w['entity']['id']}", headers=h).json()
        assert {d["id"] for d in after["data_sources"]} == {w["origin"]["id"], w["other"]["id"]}
    finally:
        os.unlink(empty)


@pytest.mark.e2e
def test_each_agent_is_shown_its_own_code_and_saving_it_changes_nothing(test_client, shared):
    from app.services.entity_code import client_keys

    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    for ds in (w["origin"], w["other"]):
        got = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": ds["id"]}, headers=h).json()
        keys, _ = client_keys(got["code_for_agent"])
        assert keys and all(k.startswith(ds["name"] + ":") for k in keys), keys

    # Saving the code as another agent shows it is not an edit: that agent's
    # result survives.
    other_view = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["other"]["id"]}, headers=h).json()
    saved = test_client.put(f"/api/entities/{w['entity']['id']}", json={"code": other_view["code_for_agent"]}, headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["code"] == w["entity"]["code"]
    again = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["other"]["id"]}, headers=h)
    assert _stores(again) == {w["stores"][w["other"]["id"]]}


@pytest.mark.e2e
def test_the_model_is_shown_every_agent_of_the_conversation_the_query_is_on(test_client, shared):
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.context.builders.entity_context_builder import EntityContextBuilder

    w = shared

    async def build():
        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["admin"]["user_id"])
            section = await EntityContextBuilder(db, org, None, user=user).build(
                keywords=[], data_source_ids=[w["other"]["id"], w["origin"]["id"]],
            )
            return next(i for i in section.items if i.id == w["entity"]["id"])

    item = _async(build)
    # Both agents, the one its code/rows come from (the origin, in play) first.
    assert item.ds_names == [w["origin"]["name"], w["other"]["name"]]
    assert w["origin"]["name"] in (item.code or "")


def _describe(w, agent=None, run_agent_ids=None):
    from types import SimpleNamespace
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.tools.implementations.describe_entity import DescribeEntityTool

    async def run():
        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            user = await db.get(User, w["admin"]["user_id"])
            settings = await org.get_settings(db)
            report = SimpleNamespace(data_sources=[SimpleNamespace(id=i) for i in (run_agent_ids or [])], id=None)
            tool_input = {"name_or_id": w["entity"]["id"]}
            if agent:
                tool_input["agent"] = agent
            events = [e async for e in DescribeEntityTool().run_stream(
                tool_input, {"db": db, "organization": org, "user": user, "settings": settings, "report": report},
            )]
            return events[-1].payload["observation"]

    return _async(run)


@pytest.mark.e2e
def test_describe_entity_says_which_other_agents_still_need_a_run(test_client, shared):
    w = shared
    obs = _describe(w, run_agent_ids=[w["origin"]["id"], w["other"]["id"]])
    assert obs["agent"] == w["origin"]["name"]
    assert obs["also_shared_with"] == [w["other"]["name"]]
    assert w["other"]["name"] in obs["next_step_hint"]


@pytest.mark.e2e
def test_describe_entity_on_a_named_agent_still_names_the_others(test_client, shared):
    """The model naming one agent is not the user choosing it: the others of
    the conversation are still pointed out."""
    w = shared
    both = [w["origin"]["id"], w["other"]["id"]]
    named = _describe(w, agent=w["other"]["name"], run_agent_ids=both)
    assert named["agent"] == w["other"]["name"]
    assert named["also_shared_with"] == [w["origin"]["name"]]


@pytest.mark.e2e
def test_describe_entity_on_a_single_agent_adds_no_hint(test_client, shared):
    w = shared

    single = _describe(w, run_agent_ids=[w["other"]["id"]])
    assert single["agent"] == w["other"]["name"]
    assert "also_shared_with" not in single


@pytest.mark.e2e
def test_new_code_on_a_new_origin_does_not_keep_the_old_origins_rows(test_client, shared):
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    assert _run(test_client, w, w["origin"]["id"]).status_code == 200

    other_key = _client_key(test_client, w["other"], w["admin"], w["org_id"])
    new_code = _code_for(other_key).replace("SUM(amount)", "SUM(amount) * 2")
    resp = test_client.put(
        f"/api/entities/{w['entity']['id']}",
        json={"code": new_code, "data_source_ids": [w["other"]["id"]]},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["origin_data_source_id"] == w["other"]["id"]
    assert w["stores"][w["origin"]["id"]] not in {r.get("store") for r in (body.get("data") or {}).get("rows", [])}


@pytest.mark.e2e
def test_changing_the_query_through_run_still_needs_every_agent(test_client, shared):
    """A refresh is gated on the agent it runs on; changing the query itself
    (title, status, ...) through the same endpoint is an edit."""
    w = shared
    w["grant"](resource_type="data_source", resource_id=w["other"]["id"], principal_type="user",
               principal_id=w["member"]["user_id"], permissions=["access", "create_entities"],
               user_token=w["admin"]["token"], org_id=w["org_id"])
    token = w["member"]["token"]
    assert _run(test_client, w, w["other"]["id"], token=token).status_code == 200
    edit = _run(test_client, w, w["other"]["id"], token=token, title="renamed via run")
    assert edit.status_code in (403, 404), edit.text


@pytest.mark.e2e
def test_saving_from_another_agents_view_keeps_the_origin(test_client, shared):
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    other_view = test_client.get(f"/api/entities/{w['entity']['id']}", params={"data_source_id": w["other"]["id"]}, headers=h).json()
    saved = test_client.put(
        f"/api/entities/{w['entity']['id']}",
        json={"title": "renamed", "code": other_view["code_for_agent"]},
        headers=h,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["origin_data_source_id"] == w["origin"]["id"]


@pytest.mark.e2e
def test_a_query_left_without_agents_still_runs_on_the_agent_it_named(test_client, shared):
    w = shared
    h = _hdr(w["admin"]["token"], w["org_id"])
    resp = test_client.put(f"/api/entities/{w['entity']['id']}", json={"data_source_ids": []}, headers=h)
    assert resp.status_code == 200, resp.text
    assert "$agent" not in resp.json()["code"]
    run = test_client.post(f"/api/entities/{w['entity']['id']}/run", json={}, headers=h)
    assert run.status_code == 200, run.text
    assert _stores(run) == {w["stores"][w["origin"]["id"]]}
