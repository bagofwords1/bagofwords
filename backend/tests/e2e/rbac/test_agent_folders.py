"""Exercise folder inheritance through the same HTTP routes as the explorer."""

import pytest
from tests.e2e.test_smart_instructions_planner_prompt import env, llm  # noqa: F401 (shared planner fixtures)


@pytest.mark.e2e
def test_shared_branch(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    viewer = invite_user_to_org(org_id=org, admin_token=admin["token"])
    lead = invite_user_to_org(org_id=org, admin_token=admin["token"])

    def request(actor, method, path, body=None, status=200):
        response = test_client.request(
            method,
            "/api/" + path,
            json=body,
            headers={"Authorization": "Bearer " + actor["token"], "X-Organization-Id": str(org)},
        )
        assert response.status_code == status, response.text
        return response.json()

    root = request(admin, "POST", "folders", {"name": "Marketing"})["id"]
    path = {"parent_id": root, "path_segments": ["Acquisition", "Campaigns"]}
    leaf = request(admin, "POST", "folders/ensure", path)["id"]
    assert request(admin, "POST", "folders/ensure", path)["id"] == leaf
    ds = sqlite_data_source(name="campaigns", user_token=admin["token"], org_id=org)
    request(admin, "PUT", f"folders/items/agent/{ds['id']}/placement", {"parent_id": leaf})
    for actor, role in [(viewer, "Viewer"), (lead, "Manager")]:
        request(
            admin,
            "PUT",
            f"folders/{root}/share",
            {"principal_type": "user", "principal_id": actor["user_id"], "role": role},
        )
        rows = request(actor, "GET", "folders")["folders"]
        assert next(f for f in rows if f["id"] == leaf)["can_manage"] == (role == "Manager")
        assert ds["id"] in {d["id"] for d in request(actor, "GET", "data_sources/active")}
    request(viewer, "POST", "folders", {"name": "Denied", "parent_id": leaf}, 403)
    request(lead, "POST", "folders", {"name": "New", "parent_id": leaf})
    request(
        admin,
        "PUT",
        f"folders/{leaf}/share",
        {"principal_type": "user", "principal_id": admin["user_id"], "role": "Manager"},
    )
    request(admin, "PUT", f"folders/{leaf}/inheritance", {"limited_access": True})
    for actor in [viewer, lead]:
        assert leaf not in {f["id"] for f in request(actor, "GET", "folders")["folders"]}
        assert ds["id"] not in {d["id"] for d in request(actor, "GET", "data_sources/active")}


@pytest.mark.e2e
def test_folder_report_resolves_current_sources(test_client, bootstrap_admin, sqlite_data_source):
    import asyncio

    from app.ai.tools.implementations.agent_focus_common import resolve_run_agents
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    headers = {"Authorization": "Bearer " + admin["token"], "X-Organization-Id": org}
    root = test_client.post("/api/folders", headers=headers, json={"name": "Team"}).json()["id"]
    agent = sqlite_data_source(name="inside", user_token=admin["token"], org_id=org)
    sqlite_data_source(name="outside", user_token=admin["token"], org_id=org)
    moved = test_client.put(
        f"/api/folders/items/agent/{agent['id']}/placement", headers=headers, json={"parent_id": root}
    )
    assert moved.is_success, moved.text
    response = test_client.post("/api/reports", headers=headers, json={"agent_folder_id": root})
    assert response.is_success, response.text
    assert response.json()["agent_folder_id"] == root

    async def roster():
        async with async_session_maker() as db:
            report = await db.get(Report, response.json()["id"])
            await db.refresh(report, ["data_sources"])
            sources = await resolve_run_agents(
                db, await db.get(Organization, org), await db.get(User, admin["user_id"]), report
            )
            return [a.id for a in sources]

    assert asyncio.run(roster()) == [agent["id"]]


def test_folder_role_roster_at_scale(test_client, env, llm, invite_user_to_org, sqlite_data_source):
    """Inspect actual planner prompts before and after a branch stops inheriting."""
    import re
    import asyncio
    from sqlalchemy import insert, select, update
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.folder import Folder

    admin = env["headers"]
    org = env["org_id"]
    member = invite_user_to_org(org_id=org, admin_token=env["token"])
    query_user = {**admin, "Authorization": "Bearer " + member["token"]}

    def api(method, path, headers=admin, **payload):
        response = test_client.request(method, "/api/" + path, headers=headers, json=payload)
        assert response.is_success, response.text
        return response.json()

    home = api("POST", "folders", name="Roster QA")["id"]
    branches = [api("POST", "folders", name=n, parent_id=home)["id"] for n in ("East", "West")]
    template = sqlite_data_source(name="External warehouse", user_token=env["token"], org_id=org)
    names = [f"PortfolioAgent{i:03}" for i in range(299)]

    async def seed():
        async with async_session_maker() as db:
            owner = await db.scalar(select(Folder.creator_id).where(Folder.id == home))
            connection = await db.scalar(
                select(domain_connection.c.connection_id).where(domain_connection.c.data_source_id == template["id"])
            )
            folders = [
                Folder(name=f"District {i}", parent_id=branches[i % 2], organization_id=org, creator_id=owner)
                for i in range(60)
            ]
            db.add_all(folders)
            await db.flush()
            agents = [
                DataSource(
                    name=name,
                    folder_id=folders[i % len(folders)].id,
                    organization_id=org,
                    owner_user_id=owner,
                    is_public=False,
                    inherits_folder_access=i != 0,
                )
                for i, name in enumerate(names)
            ]
            db.add_all(agents)
            await db.flush()
            await db.execute(
                insert(domain_connection), [{"data_source_id": a.id, "connection_id": connection} for a in agents]
            )
            await db.commit()
            return owner

    owner = asyncio.run(seed())
    rule = api(
        "POST",
        f"folders/{home}/knowledge/instructions",
        title="Attribution routing",
        text="MARKETING_ROUTE_CAC route acquisition cost to PortfolioAgent289.",
    )
    api("POST", f"folders/{branches[0]}/knowledge/instructions", title="Local routing", text="PRIVATE_CHILD_ROUTE_EAST")
    role = api(
        "POST",
        f"organizations/{org}/roles",
        name="Portfolio readers",
        resource_grants=[{"resource_type": "folder", "resource_id": home, "permissions": ["view"]}],
    )
    api(
        "POST",
        f"organizations/{org}/role-assignments",
        role_id=role["id"],
        principal_type="user",
        principal_id=member["user_id"],
    )
    rid = api("POST", "reports", query_user, agent_folder_id=home)["id"]

    def turn(expected, focused=0):
        from app.ai.tools.implementations.agent_focus_common import resolve_run_agents
        from app.models.report import Report
        from app.models.organization import Organization
        from app.models.user import User

        async def runtime_names():
            async with async_session_maker() as db:
                report = await db.get(Report, rid)
                await db.refresh(report, ["data_sources"])
                agents = await resolve_run_agents(
                    db, await db.get(Organization, org), await db.get(User, member["user_id"]), report
                )
                return {agent.name for agent in agents}

        assert asyncio.run(runtime_names()) == expected
        llm["planner"].clear()
        api("POST", f"reports/{rid}/completions", query_user, prompt={"content": "What agents are available?"})
        assert llm["planner"], "Completion never reached planner"
        prompt = llm["planner"][0]
        roster = re.search(r"<available_agents\b.*?</available_agents>", prompt, re.S)
        assert roster, "No planner roster"
        assert "PRIVATE_CHILD_ROUTE_EAST" not in prompt
        assert ("MARKETING_ROUTE_CAC" in prompt) == (len(expected) > 2)
        assert f'count="{len(expected)}"' in roster.group()
        assert f'focused="{focused}"' in roster.group()
        advertised = set(re.findall(r"PortfolioAgent\d{3}", roster.group()))
        assert advertised and advertised <= expected
        assert "External warehouse" not in roster.group()
        if len(expected) > 2:
            assert 'folder_path="Roster QA / ' in roster.group()

    async def measure_discovery(limit):
        from time import monotonic
        from sqlalchemy import event
        from app.ai.tools.implementations.agent_focus_common import resolve_run_agents
        from app.services.folder_knowledge import readable_folder_rules
        from app.models.organization import Organization
        from app.models.report import Report
        from app.models.user import User

        async with async_session_maker() as db:
            if limit == 30:
                await db.execute(update(DataSource).where(
                    DataSource.organization_id == org, DataSource.name.in_(names[30:])
                ).values(is_active=False))
            report = await db.get(Report, rid)
            await db.refresh(report, ["data_sources"])
            actor = await db.get(User, member["user_id"])
            organization = await db.get(Organization, org)
            connection = (await db.connection()).sync_connection
            samples = []
            for iteration in range(2):
                statements = []
                def count_sql(*args):
                    statements.append(1)
                event.listen(connection, "before_cursor_execute", count_sql)
                started = monotonic()
                try:
                    agents = await resolve_run_agents(db, organization, actor, report)
                    rules = await readable_folder_rules(db, organization, actor, home)
                finally:
                    event.remove(connection, "before_cursor_execute", count_sql)
                assert len(agents) == min(limit - 1, 298)
                assert rule["id"] in {entry.id for entry in rules}
                samples.append(len(statements))
                print(f"folder discovery: limit={limit} pass={iteration+1} sql={len(statements)} "
                      f"ms={(monotonic()-started)*1000:.1f}")
            await db.rollback()
            return samples

    small = asyncio.run(measure_discovery(30))
    large = asyncio.run(measure_discovery(300))
    assert all(b <= a + 5 for a, b in zip(small, large)), (small, large)

    async def search_neighbors():
        from app.ai.tools.implementations.search_agents import SearchAgentsTool
        from app.models.organization import Organization
        from app.models.report import Report
        from app.models.user import User
        async with async_session_maker() as db:
            report = await db.get(Report, rid)
            ctx = {"db": db, "report": report, "mode": "chat",
                   "organization": await db.get(Organization, org),
                   "user": await db.get(User, member["user_id"])}
            for subject, expected in [(names[1], set(names[1::60])), (names[0], set())]:
                aid = await db.scalar(select(DataSource.id).where(DataSource.name == subject,
                                                                  DataSource.organization_id == org))
                events = [e async for e in SearchAgentsTool().run_stream({"same_folder_as": aid}, ctx)]
                found = events[-1].payload["output"]
                assert found["success"] and {a["name"] for a in found["agents"]} == expected
                assert all(a["folder_path"] == "Roster QA / West / District 1" for a in found["agents"])
    asyncio.run(search_neighbors())

    turn(set(names[1:]))

    # Read the routing revision from the planner input and choose the stable ID
    # solely from the real search observation in the next planner request.
    def choose(step, text):
        target = re.search(r"MARKETING_ROUTE_CAC route acquisition cost to (PortfolioAgent[0-9]+)", text)
        if step == 0 and target:
            return "search_agents", {"query": [target[1]]}
        if step == 1:
            match = re.search(r"- PortfolioAgent[0-9]+ \(id=([0-9a-f-]{36})", text)
            if match:
                return "set_report_agents", {"agent_ids": [match[1]]}
        return None

    for target in (names[289], names[287]):
        if target == names[287]:
            api(
                "PUT",
                "instructions/" + rule["id"],
                text="MARKETING_ROUTE_CAC route acquisition cost to " + target + ".",
            )
        sample = api("POST", "reports", query_user, agent_folder_id=home)["id"]
        import os
        if os.environ.get("FOLDER_ROUTING_LIVE_KEY"):
            from tests.e2e.rbac.folder_routing_provider import select_focus_tool
            llm.update(base=None, provider_choice=select_focus_tool)
        else:
            llm.update(base=None, choose=choose)
        api(
            "POST",
            f"reports/{sample}/completions",
            query_user,
            prompt={"content": "Which agent handles acquisition cost?"},
        )
        observed = api("GET", "reports/" + sample, query_user)
        target_id = next(a["id"] for a in api("GET", "data_sources/active", query_user) if a["name"] == target)
        assert observed["focused_data_source_ids"] == [target_id], llm["planner"][-1][-1800:]
    llm.pop("choose", None)
    llm.pop("provider_choice", None)
    # No-match folder searches never suggest an arbitrary accessible agent.
    def exact_name_after_miss(step, text):
        if step == 0:
            return "search_agents", {"query": ["unmatched-synthetic-topic"]}
        if step == 1:
            assert "No matching agent in this folder" in text
            return "set_report_agents", {"agent_ids": [names[287]]}
        return None

    llm.update(base=None, choose=exact_name_after_miss)
    by_name = api("POST", "reports", query_user, agent_folder_id=home)["id"]
    api("POST", f"reports/{by_name}/completions", query_user, prompt={"content": "Find the campaign agent."})
    assert api("GET", "reports/" + by_name, query_user)["focused_data_source_ids"] == [target_id]
    llm.pop("choose")
    # Small branches use the same folder focus contract even below the normal Auto threshold.
    district = next(f["id"] for f in api("GET", "folders")["folders"] if f["name"] == "District 58")
    leaf = api("POST", "folders", name="Pair", parent_id=district)["id"]

    async def file_pair():
        async with async_session_maker() as db:
            await db.execute(
                update(DataSource)
                .where(DataSource.organization_id == org, DataSource.name.in_(names[58:60]))
                .values(folder_id=leaf)
            )
            await db.commit()

    asyncio.run(file_pair())
    full_report = rid
    rid = api("POST", "reports", query_user, agent_folder_id=leaf)["id"]
    turn(set(names[58:60]))
    second = next(a["id"] for a in api("GET", "data_sources/active", query_user) if a["name"] == names[59])
    api("PUT", f"folders/items/agent/{second}/placement", parent_id=district)
    turn({names[58]}, focused=1)
    rid = full_report
    for branch in branches:
        api("PUT", f"folders/{branch}/share", principal_type="user", principal_id=owner, role="Manager")
        api("PUT", f"folders/{branch}/inheritance", limited_access=True)
        if branch == branches[0]:
            llm["planner"].clear()
            blocked = test_client.post(
                f"/api/reports/{rid}/completions", headers=query_user, json={"prompt": {"content": "Continue"}}
            )
            assert blocked.status_code == 404 and not llm["planner"]
            rid = api("POST", "reports", query_user, agent_folder_id=home)["id"]
            turn(set(names[1::2]) - {names[59]})
    llm["planner"].clear()
    rid = api("POST", "reports", query_user, agent_folder_id=home)["id"]
    result = test_client.post(
        f"/api/reports/{rid}/completions",
        headers=query_user,
        json={"prompt": {"content": "Show available agents again"}},
    )
    assert not llm["planner"]
    assert result.status_code == 409, result.text


def test_folder_role_levels_and_agent_exceptions(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    create_llm_provider_and_models,
    llm,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "local-test-double")
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    create_llm_provider_and_models(user_token=admin["token"], org_id=org)
    member = invite_user_to_org(org_id=org, admin_token=admin["token"])

    def request(actor, method, path, **data):
        return test_client.request(
            method,
            "/api/" + path,
            json=data,
            headers={"Authorization": "Bearer " + actor["token"], "X-Organization-Id": org},
        )

    folder = request(admin, "POST", "folders", name="Marketing").json()["id"]
    agent = sqlite_data_source(name="Campaigns", user_token=admin["token"], org_id=org)["id"]
    assert request(admin, "PUT", f"folders/items/agent/{agent}/placement", parent_id=folder).is_success
    roles = f"organizations/{org}/roles"
    grant = {"resource_type": "folder", "resource_id": folder, "permissions": ["view"]}
    assert (
        request(
            admin, "POST", roles, name="Invalid", resource_grants=[{**grant, "permissions": ["unknown"]}]
        ).status_code
        == 422
    )
    assert (
        request(admin, "POST", roles, name="Missing", resource_grants=[{**grant, "resource_id": "missing"}]).status_code
        == 403
    )
    group = request(admin, "POST", f"organizations/{org}/groups", name="Campaign staff").json()["id"]
    assert request(admin, "POST", f"organizations/{org}/groups/{group}/members", user_id=member["user_id"]).is_success
    role = request(admin, "POST", roles, name="Folder staff", resource_grants=[grant]).json()["id"]
    assert request(
        admin, "POST", f"organizations/{org}/role-assignments", role_id=role, principal_type="group", principal_id=group
    ).is_success
    child = request(admin, "POST", "folders", name="Private work", parent_id=folder).json()["id"]
    suite_id = None
    for capability in ["manage_files", "manage_evals", "manage_instructions"]:
        custom = {**grant, "permissions": [capability]}
        updated = request(admin, "PUT", roles + "/" + role, resource_grants=[custom])
        assert updated.is_success, updated.text
        rows = {f["id"]: f for f in request(member, "GET", "folders").json()["folders"]}
        assert rows[folder]["capabilities"] == [capability]
        for kind in ("files", "instructions", "evals"):
            response = request(member, "GET", f"folders/{folder}/knowledge/{kind}")
            assert response.status_code == (200 if capability == "manage_" + kind else 403), response.text
        if capability == "manage_evals":
            assert request(member, "GET", f"files/{file_id}/content").status_code == 404
            assert test_client.get(embed_url).status_code in (403, 404)
            base = f"folders/{folder}/knowledge/evals"
            response = request(member, "POST", base, name="CAC routing")
            assert response.is_success, response.text
            suite_id = response.json()["id"]
            response = request(
                member,
                "POST",
                f"{base}/{suite_id}/cases",
                name="Acquisition",
                prompt_json={"content": "What is our acquisition cost?"},
                expectations_json={"rules": []},
            )
            assert response.is_success, response.text
            assert request(member, "POST", f"{base}/{suite_id}/runs").status_code == 403
            assert request(member, "PUT", f"folders/{child}/knowledge/evals/{suite_id}").status_code == 403
        if capability == "manage_files":
            headers = {"Authorization": "Bearer " + member["token"], "X-Organization-Id": org}
            file = test_client.post(
                f"/api/folders/{folder}/knowledge/files",
                headers=headers,
                files={"file": ("glossary.txt", b"Paid channel glossary", "text/plain")},
            )
            assert file.is_success, file.text
            file_id = file.json()["id"]
            assert request(member, "GET", f"files/{file_id}/content").is_success
            embed_url = request(member, "GET", f"files/{file_id}/embed_token").json()["url"]
            assert test_client.get(embed_url).is_success
            assert request(member, "GET", f"folders/{folder}/knowledge/files").json()[0]["name"] == "glossary.txt"
        if capability == "manage_instructions":
            created = request(
                member,
                "POST",
                f"folders/{folder}/knowledge/instructions",
                title="Channel policy",
                text="Use the attribution agent.",
            )
            assert created.is_success, created.text
            assert (
                request(member, "GET", f"folders/{folder}/knowledge/instructions").json()[0]["id"]
                == created.json()["id"]
            )
            assert request(
                member, "DELETE", f"folders/{folder}/knowledge/instructions/{created.json()['id']}"
            ).is_success
        if capability == "manage_instructions":
            training = request(member, "POST", "reports", mode="training", agent_folder_id=folder)
            assert training.is_success, training.text
            rid = training.json()["id"]
            assert request(member, "GET", f"reports/{rid}").is_success
            llm["planner"].clear()
            made = request(member, "POST", f"reports/{rid}/completions", prompt={"content": "Review routing rules"})
            assert made.is_success and llm["planner"], made.text
            assert "Campaigns" not in llm["planner"][0].partition("<available_agents")[2].partition("</available_agents>")[0]
        assert not rows[folder]["can_query"] and child not in rows
        assert request(member, "POST", "reports", agent_folder_id=folder).status_code == 403
        assert agent not in {a["id"] for a in request(member, "GET", "data_sources/active").json()}
    assert request(
        admin, "PUT", roles + "/" + role, resource_grants=[{**grant, "permissions": ["view", "manage_evals"]}]
    ).is_success
    base = f"folders/{folder}/knowledge/evals/{suite_id}"
    run = request(member, "POST", base + "/runs")
    assert run.is_success, run.text
    run_id = run.json()["id"]
    results = request(member, "GET", f"tests/runs/{run_id}/results")
    assert results.is_success, results.text
    result = results.json()[0]
    assert request(member, "GET", f"tests/results/{result['id']}").is_success
    streamed = request(member, "POST", f"tests/runs/{run_id}/stream")
    assert streamed.is_success, streamed.text
    checked = request(member, "GET", f"tests/results/{result['id']}").json()
    assert checked["status"] == "pass", checked
    assert llm["planner"], "Folder routing did not reach the planner"

    pinned_suite = request(admin, "POST", "tests/suites", name="Campaign targets", data_source_id=agent).json()["id"]
    pinned_case = request(admin, "POST", f"tests/suites/{pinned_suite}/cases", name="Pinned campaign",
                          prompt_json={"content": "Check campaign spend"}, expectations_json={"rules": []},
                          data_source_ids_json=[agent])
    assert pinned_case.is_success, pinned_case.text
    pinned_base = f"folders/{folder}/knowledge/evals/{pinned_suite}"
    assert request(admin, "PUT", pinned_base).is_success
    assert request(member, "POST", pinned_base + "/runs").status_code == 403
    peer = sqlite_data_source(name="Second eligible campaign agent", user_token=admin["token"], org_id=org)["id"]
    assert request(admin, "PUT", f"folders/items/agent/{peer}/placement", parent_id=folder).is_success
    assert request(admin, "POST", f"folders/{folder}/knowledge/instructions",
                   title="Pinned eval routing", text="PINNED_EVAL_FOLDER_CONTEXT").is_success
    llm["planner"].clear()
    pinned_run = request(admin, "POST", pinned_base + "/runs")
    assert pinned_run.is_success, pinned_run.text
    pinned_id = pinned_run.json()["id"]
    assert request(admin, "POST", f"tests/runs/{pinned_id}/stream").is_success
    pinned_result = request(admin, "GET", f"tests/runs/{pinned_id}/results").json()[0]
    assert pinned_result["status"] == "pass", pinned_result
    prompt = llm["planner"][0]
    assert "PINNED_EVAL_FOLDER_CONTEXT" in prompt
    roster = prompt.partition("<available_agents")[2].partition("</available_agents>")[0]
    assert "Campaigns" in roster and "Second eligible campaign agent" not in roster

    assert request(admin, "PUT", roles + "/" + role, resource_grants=[grant]).is_success
    assert request(member, "GET", f"tests/runs/{run_id}/results").status_code in (403, 404)
    for level in ("viewer", "manager"):
        info = request(member, "GET", f"data_sources/{agent}").json()
        assert (info["inherited_folder_role"], info["inherited_folder_name"]) == (level, "Marketing")
        move = request(member, "PUT", f"folders/items/agent/{agent}/placement", parent_id=None, preview=True)
        assert move.status_code == (200 if level == "manager" else 403), move.text
        if level == "viewer":
            grant["permissions"] = ["manage"]
            assert request(admin, "PUT", roles + "/" + role, resource_grants=[grant]).is_success
    assert request(admin, "PUT", f"folders/agents/{agent}/inheritance", limited_access=True).is_success
    assert agent not in {a["id"] for a in request(member, "GET", "data_sources/active").json()}


@pytest.mark.e2e
@pytest.mark.per_request_loop
def test_folder_revoke_racing_instruction_create(test_client, bootstrap_admin, invite_user_to_org):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    editor = invite_user_to_org(org_id=org, admin_token=admin["token"])

    def request(actor, method, path, data):
        return test_client.request(method, "/api/" + path, json=data, headers={
            "X-Organization-Id": org, "Authorization": "Bearer " + actor["token"],
        })

    fid = request(admin, "POST", "folders", {"name": "Acquisition"}).json()["id"]
    path = f"folders/{fid}/knowledge/instructions"
    share = f"folders/{fid}/share"
    target = {"principal_type": "user", "principal_id": editor["user_id"]}
    assert request(admin, "PUT", share, {**target, "role": "Manager"}).is_success
    gate = Barrier(2)

    def write():
        gate.wait(timeout=15)
        return request(editor, "POST", path, {"title": "Campaign owner", "text": "Use attribution."})

    def revoke():
        gate.wait(timeout=15)
        return request(admin, "PUT", share, {**target, "role": None})

    with ThreadPoolExecutor(max_workers=2) as pool:
        edit, removal = pool.submit(write), pool.submit(revoke)
        changed = edit.result(timeout=45)
        removed = removal.result(timeout=45)
    assert removed.is_success, removed.text
    assert changed.is_success or changed.status_code == 403, changed.text
    rows = request(admin, "GET", path, None).json()
    assert len(rows) == int(changed.is_success)
    assert request(editor, "GET", path, None).status_code == 403
    assert request(editor, "POST", path, {"title": "Late rule", "text": "Late write"}).status_code == 403


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_folder_authoring_tool_destinations(test_client, bootstrap_admin, invite_user_to_org):
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool
    from app.ai.tools.implementations.create_eval import CreateEvalTool
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.folder import folder_instructions, folder_eval_suites

    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    member = invite_user_to_org(org_id=org, admin_token=admin["token"])
    headers = {"Authorization": "Bearer " + admin["token"], "X-Organization-Id": org}
    def post(path, body):
        res = test_client.post("/api/" + path, json=body, headers=headers)
        assert res.is_success, res.text
        return res.json()
    folder = post("folders", {"name": "Marketing"})["id"]
    other = post("folders", {"name": "Finance"})["id"]
    role = post(f"organizations/{org}/roles", {"name": "Curator", "resource_grants": [{
        "resource_type": "folder", "resource_id": folder, "permissions": ["manage_instructions", "manage_evals"]}]})
    post(f"organizations/{org}/role-assignments", {"role_id": role["id"], "principal_type": "user",
                                                "principal_id": member["user_id"]})
    async with async_session_maker() as db:
        ctx = {"mode": "knowledge", "db": db, "organization": await db.get(Organization, org),
               "user": await db.get(User, member["user_id"]), "report": SimpleNamespace(agent_folder_id=folder, data_sources=[])}
        for tool, fields in [(CreateInstructionTool(), {"text": "Route campaign ROI questions to the attribution agent.",
                                                        "confidence": 0.99}),
                             (CreateEvalTool(), {"name": "Route ROI", "prompt": {"content": "What is campaign ROI?"},
                                "expectations": {"rules": [{"type": "tool.calls", "tool": "create_data", "min_calls": 1}]}})]:
            # A folder NAME (what weaker models pass) resolves to the report's folder;
            # an explicit id of another real folder is refused, not redirected.
            for destination in ({}, {"destination": "global"}, {"folder_id": other}, {"destination": "folder"},
                                {"destination": "folder", "folder_id": "Marketing"}):
                events = [e async for e in tool.run_stream({**fields, **destination}, ctx)]
                if destination.get("destination") != "folder":
                    assert events[-1].type == "tool.error", events[-1]
                    continue
                result = events[-1].payload.get("output", {})
                assert result.get("success"), events[-1]
                assert result["folder_id"] == folder and result["data_source_ids"] == []
                if "suite_id" in result:
                    assert result["status"] == "draft"
                    assert await db.scalar(select(folder_eval_suites.c.folder_id).where(
                        folder_eval_suites.c.suite_id == result["suite_id"])) == folder
                else:
                    assert await db.scalar(select(folder_instructions.c.folder_id).where(
                        folder_instructions.c.instruction_id == result["instruction_id"])) == folder
        from app.ai.tools.mcp.instructions import CreateInstructionMCPTool
        from app.ai.tools.mcp.evals import CreateEvalMCPTool
        from app.routes.mcp import _holds_permission_anywhere
        for adapter, capability, fields in [
            (CreateInstructionMCPTool(), "manage_instructions", {"text": "Use attribution for acquisition costs."}),
            (CreateEvalMCPTool(), "manage_evals", {"name": "MCP routing", "prompt": {"content": "Acquisition cost?"},
                "expectations": {"rules": [{"type": "tool.calls", "tool": "create_data", "min_calls": 1}]}}),
        ]:
            assert await _holds_permission_anywhere(db, ctx["user"], ctx["organization"], capability)
            for fid in (other, folder):
                result = await adapter.execute({**fields, "folder_id": fid}, db, ctx["user"], ctx["organization"])
                assert result["success"] == (fid == folder), result
                if result["success"]:
                    assert result["folder_id"] == folder
