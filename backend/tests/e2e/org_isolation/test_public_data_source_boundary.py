"""A "public" data source is open to the members of its own organization only.

Contract:
* the access check never grants a data source to a principal who is not bound
  to the organization that owns it, public or not, admin elsewhere or not;
* an entity can only be attached to data sources of the caller's organization;
* a connection's OAuth sign-in is available only to the owning organization.
"""
import asyncio
import uuid

import pytest

from app.core.permission_resolver import user_can_access_data_source
from app.dependencies import async_session_maker
from app.models.data_source import DataSource

from .agent_surface_fixtures import allow_multiple_orgs, h, sqlite_agent, two_orgs  # noqa: F401


async def _can_access(user_id, org_id, ds_id):
    async with async_session_maker() as db:
        ds = await db.get(DataSource, ds_id)
        return await user_can_access_data_source(db, str(user_id), str(org_id), ds)


@pytest.mark.parametrize("is_public", [True, False])
def test_data_source_access_stops_at_the_owning_organization(two_orgs, sqlite_agent, is_public):
    o = two_orgs
    ds = sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=is_public)

    # The owning organization's own admin keeps access.
    assert asyncio.run(_can_access(o["other_user_id"], o["other_org"], ds["id"])) is True
    # An unrelated principal has none, whichever organization the check is asked for.
    assert asyncio.run(_can_access(o["home_user_id"], o["other_org"], ds["id"])) is False
    assert asyncio.run(_can_access(o["home_user_id"], o["home_org"], ds["id"])) is False


def test_public_data_source_is_open_to_plain_members_of_its_organization(two_orgs, sqlite_agent):
    o = two_orgs
    # `other` admin is a plain (non-admin) member of `home`.
    public = sqlite_agent(token=o["home_token"], org_id=o["home_org"], is_public=True)
    private = sqlite_agent(token=o["home_token"], org_id=o["home_org"], is_public=False)

    assert asyncio.run(_can_access(o["other_user_id"], o["home_org"], public["id"])) is True
    assert asyncio.run(_can_access(o["other_user_id"], o["home_org"], private["id"])) is False


def _entity_payload(ds_ids):
    return {
        "type": "model", "title": f"e {uuid.uuid4().hex[:6]}", "slug": f"e-{uuid.uuid4().hex[:8]}",
        "description": None, "tags": [], "code": "select 1 as value", "data": {},
        "status": "draft", "data_source_ids": ds_ids,
    }


def test_entity_can_only_be_created_on_own_organization_data_sources(test_client, two_orgs, sqlite_agent):
    o = two_orgs
    own = sqlite_agent(token=o["home_token"], org_id=o["home_org"])
    foreign = sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=True)
    headers = h(o["home_token"], o["home_org"])

    denied = test_client.post("/api/entities/global", json=_entity_payload([foreign["id"]]), headers=headers)
    assert denied.status_code in (403, 404), denied.text
    mixed = test_client.post("/api/entities/global", json=_entity_payload([own["id"], foreign["id"]]), headers=headers)
    assert mixed.status_code in (403, 404), mixed.text
    assert foreign["name"] not in denied.text and foreign["name"] not in mixed.text

    ok = test_client.post("/api/entities/global", json=_entity_payload([own["id"]]), headers=headers)
    assert ok.status_code == 200, ok.text
    assert [d["id"] for d in ok.json()["data_sources"]] == [own["id"]]


def test_entity_update_cannot_attach_another_organizations_data_source(test_client, two_orgs, sqlite_agent):
    o = two_orgs
    own = sqlite_agent(token=o["home_token"], org_id=o["home_org"])
    second = sqlite_agent(token=o["home_token"], org_id=o["home_org"])
    foreign = sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=True)
    headers = h(o["home_token"], o["home_org"])
    entity = test_client.post("/api/entities/global", json=_entity_payload([own["id"]]), headers=headers).json()

    denied = test_client.put(f"/api/entities/{entity['id']}",
                             json={"data_source_ids": [foreign["id"]]}, headers=headers)
    assert denied.status_code in (403, 404), denied.text
    after = test_client.get(f"/api/entities/{entity['id']}", headers=headers)
    assert after.status_code == 200
    assert [d["id"] for d in after.json()["data_sources"]] == [own["id"]]

    ok = test_client.put(f"/api/entities/{entity['id']}",
                         json={"data_source_ids": [second["id"]]}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert [d["id"] for d in ok.json()["data_sources"]] == [second["id"]]


def test_connection_oauth_sign_in_requires_membership_of_the_owning_organization(
    test_client, two_orgs, create_connection, create_domain_from_connection,
):
    o = two_orgs
    conn = create_connection(
        name=f"pbi_{uuid.uuid4().hex[:6]}", type="powerbi", config={},
        credentials={"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"},
        auth_policy="user_required", allowed_user_auth_modes=["oauth"],
        user_token=o["other_token"], org_id=o["other_org"],
    )
    # A public agent on the connection: open to `other`'s members, nobody else.
    create_domain_from_connection(
        name=f"pub_{uuid.uuid4().hex[:6]}", connection_id=conn["id"],
        user_token=o["other_token"], org_id=o["other_org"], is_public=True,
    )
    url = f"/api/connections/{conn['id']}/oauth/authorize"

    outsider = test_client.get(url, headers=h(o["home_token"], o["home_org"]))
    assert outsider.status_code in (403, 404), outsider.text
    assert "authorization_url" not in outsider.text
    # Same answer without an organization header: the route derives it from the connection.
    bare = test_client.get(url, headers={"Authorization": f"Bearer {o['home_token']}"})
    assert bare.status_code in (403, 404), bare.text

    member = test_client.get(url, headers=h(o["other_token"], o["other_org"]))
    assert member.status_code == 200, member.text
    assert member.json()["authorization_url"]
