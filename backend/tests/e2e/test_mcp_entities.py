"""E2E tests: saved queries (catalog entities) over the MCP API.

The organization's saved queries are its agreed metric/model definitions. An
external MCP client that is never shown them writes its own definition of the
same term, so the contract here is: a published saved query on an agent the
caller can reach is discoverable (get_context, list_entities) and its code is
readable (describe_entity) — and a draft one, or one on an agent the caller
cannot reach, is neither.
"""

import json
import uuid

import pytest


def _mcp_caller(test_client, api_key):
    def call(tool_name, arguments):
        response = test_client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return json.loads(result["content"][0]["text"])

    return call


def _sqlite_agent(create_data_source, *, user_token, org_id, name):
    """A plain (non-public) agent, so access is grant-based."""
    return create_data_source(
        name=name,
        type="sqlite",
        config={"database": "db/app.db"},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )


@pytest.fixture
def org_admin(create_user, login_user, whoami, create_api_key, enable_mcp):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)
    api_key = create_api_key(user_token=token, org_id=org_id)["key"]
    return {"user": user, "token": token, "org_id": org_id, "api_key": api_key}


@pytest.mark.e2e
def test_mcp_lists_published_saved_queries_with_their_definition(
    test_client, org_admin, create_data_source, create_global_entity,
):
    """A published saved query is listed, and describe_entity returns the code
    it is defined by — the whole point of the surface."""
    suffix = uuid.uuid4().hex[:8]
    ds = _sqlite_agent(
        create_data_source, user_token=org_admin["token"], org_id=org_admin["org_id"],
        name=f"Warehouse {suffix}",
    )
    definition = "select count(*) as active_users from users where deleted_at is null"
    entity = create_global_entity(
        title=f"Active users {suffix}",
        slug=f"active-users-{suffix}",
        status="published",
        type="metric",
        code=definition,
        description="An active user has not been deleted.",
        data_source_ids=[ds["id"]],
        user_token=org_admin["token"],
        org_id=org_admin["org_id"],
    )

    call = _mcp_caller(test_client, org_admin["api_key"])

    listing = call("list_entities", {})
    titles = {e["title"] for e in listing["entities"]}
    assert entity["title"] in titles
    assert listing["total"] == len(listing["entities"])

    described = call("describe_entity", {"name_or_id": entity["title"]})
    assert described["success"] is True
    assert described["entity_id"] == entity["id"]
    # The definition itself, not a paraphrase of it.
    assert described["code"] == definition
    assert described["type"] == "metric"
    assert ds["name"] in described["agents"]


@pytest.mark.e2e
@pytest.mark.parametrize("handle", ["id", "slug", "title", "partial_title"])
def test_mcp_describe_entity_resolves_any_handle(
    test_client, org_admin, create_data_source, create_global_entity, handle,
):
    """A client holding any of the names a saved query goes by reaches it."""
    suffix = uuid.uuid4().hex[:8]
    ds = _sqlite_agent(
        create_data_source, user_token=org_admin["token"], org_id=org_admin["org_id"],
        name=f"Warehouse {suffix}",
    )
    entity = create_global_entity(
        title=f"Net revenue {suffix}",
        slug=f"net-revenue-{suffix}",
        status="published",
        type="metric",
        code="select 1 as net_revenue",
        data_source_ids=[ds["id"]],
        user_token=org_admin["token"],
        org_id=org_admin["org_id"],
    )
    name_or_id = {
        "id": entity["id"],
        "slug": entity["slug"],
        "title": entity["title"],
        "partial_title": f"Net revenue {suffix}"[:14],
    }[handle]

    described = _mcp_caller(test_client, org_admin["api_key"])(
        "describe_entity", {"name_or_id": name_or_id}
    )
    assert described["success"] is True
    assert described["entity_id"] == entity["id"]


@pytest.mark.e2e
def test_mcp_does_not_serve_unpublished_saved_queries(
    test_client, org_admin, create_data_source, create_global_entity,
):
    """Drafts are not the organization's agreed definition yet."""
    suffix = uuid.uuid4().hex[:8]
    ds = _sqlite_agent(
        create_data_source, user_token=org_admin["token"], org_id=org_admin["org_id"],
        name=f"Warehouse {suffix}",
    )
    draft = create_global_entity(
        title=f"Draft churn {suffix}",
        slug=f"draft-churn-{suffix}",
        status="draft",
        code="select 1 as churn",
        data_source_ids=[ds["id"]],
        user_token=org_admin["token"],
        org_id=org_admin["org_id"],
    )

    call = _mcp_caller(test_client, org_admin["api_key"])
    assert draft["title"] not in {e["title"] for e in call("list_entities", {})["entities"]}

    described = call("describe_entity", {"name_or_id": draft["id"]})
    assert described["success"] is False
    assert described["code"] is None


@pytest.mark.e2e
def test_mcp_saved_queries_follow_agent_access(
    test_client, org_admin, create_data_source, create_global_entity,
    create_user, login_user, create_api_key,
):
    """A member with no grant on the agent neither sees nor reads its saved
    query — the same gate every other entity surface applies."""
    suffix = uuid.uuid4().hex[:8]
    ds = _sqlite_agent(
        create_data_source, user_token=org_admin["token"], org_id=org_admin["org_id"],
        name=f"Private warehouse {suffix}",
    )
    entity = create_global_entity(
        title=f"Secret metric {suffix}",
        slug=f"secret-metric-{suffix}",
        status="published",
        code="select 1 as secret",
        data_source_ids=[ds["id"]],
        user_token=org_admin["token"],
        org_id=org_admin["org_id"],
    )

    member_email = f"member_{suffix}@test.com"
    invite = test_client.post(
        f"/api/organizations/{org_admin['org_id']}/members",
        json={"organization_id": org_admin["org_id"], "email": member_email, "role": "member"},
        headers={
            "Authorization": f"Bearer {org_admin['token']}",
            "X-Organization-Id": str(org_admin["org_id"]),
        },
    )
    assert invite.status_code == 200, invite.text
    member = create_user(email=member_email)
    member_token = login_user(member["email"], member["password"])
    member_key = create_api_key(user_token=member_token, org_id=org_admin["org_id"])["key"]

    member_call = _mcp_caller(test_client, member_key)
    assert entity["title"] not in {
        e["title"] for e in member_call("list_entities", {})["entities"]
    }
    described = member_call("describe_entity", {"name_or_id": entity["id"]})
    assert described["success"] is False
    assert described["code"] is None

    # The owner still reaches it, so the assertion above is about access and
    # not about the saved query being unreachable for everyone.
    owner_described = _mcp_caller(test_client, org_admin["api_key"])(
        "describe_entity", {"name_or_id": entity["id"]}
    )
    assert owner_described["success"] is True


@pytest.mark.e2e
def test_mcp_get_context_advertises_saved_queries_on_the_report(
    test_client, org_admin, create_data_source, create_global_entity,
):
    """get_context is what a client calls to learn what a session has; a saved
    query on the session's agents belongs in that answer."""
    suffix = uuid.uuid4().hex[:8]
    ds = _sqlite_agent(
        create_data_source, user_token=org_admin["token"], org_id=org_admin["org_id"],
        name=f"Warehouse {suffix}",
    )
    entity = create_global_entity(
        title=f"Monthly revenue {suffix}",
        slug=f"monthly-revenue-{suffix}",
        status="published",
        type="metric",
        code="select 1 as revenue",
        data_source_ids=[ds["id"]],
        user_token=org_admin["token"],
        org_id=org_admin["org_id"],
    )

    call = _mcp_caller(test_client, org_admin["api_key"])
    report_id = call("create_report", {"title": f"Session {suffix}"})["report_id"]

    context = call("get_context", {"report_id": report_id})
    listed = {e["title"]: e for e in context["entities"]}
    assert entity["title"] in listed
    assert listed[entity["title"]]["id"] == entity["id"]
    assert context["entities_hint"]

    scoped = call("list_entities", {"report_id": report_id})
    assert entity["title"] in {e["title"] for e in scoped["entities"]}


@pytest.mark.e2e
def test_mcp_tools_list_exposes_the_saved_query_tools(test_client, org_admin):
    """A client discovers these tools the same way it discovers the others."""
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": org_admin["api_key"]},
    )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert {"list_entities", "describe_entity"} <= names
