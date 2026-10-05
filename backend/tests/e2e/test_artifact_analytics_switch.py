"""Analytics has its own org switch: on by default, independent of resources."""

import asyncio
import pytest
from app.dependencies import async_session_maker
from app.models.report import Report
from tests.fixtures.artifact import seed_artifact

# Explicit re-export registers the shared fixture with pytest.
from tests.e2e.rbac.conftest import invite_user_to_org as invite_user_to_org


@pytest.fixture
def default_artifact(test_client, create_user, login_user, whoami, create_report):
    """An owner's artifact in an org whose settings were never touched."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    report = create_report(user_token=token, org_id=org)

    async def seed():
        async with async_session_maker() as db:
            row = await db.get(Report, report["id"])
            artifact = await seed_artifact(db, report_id=row.id, user_id=row.user_id, organization_id=org)
            await db.commit()
            return artifact.artifact_id

    artifact = asyncio.run(seed())
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    return test_client, f"/api/artifacts/{artifact}/runtime", headers, report["id"]


def set_flags(client, headers, **flags):
    config = {f"enable_artifact_{name}": {"value": value} for name, value in flags.items()}
    response = client.put("/api/organization/settings", headers=headers, json={"config": config})
    assert response.status_code == 200, response.text


def count_view(client, base, headers, surface="embedded"):
    token = client.get(base + f"/view-token?surface={surface}", headers=headers)
    if token.status_code != 200:
        return token.status_code
    return client.post(base + "/views", headers=headers, json=token.json()).status_code


@pytest.mark.e2e
def test_defaults_count_views_without_enabling_resources(default_artifact):
    client, base, headers, _ = default_artifact
    config = client.get("/api/organization/settings", headers=headers).json()["config"]
    assert config["enable_artifact_analytics"]["value"] is True
    assert config["enable_artifact_resources"]["value"] is False

    assert client.get(base + "/features", headers=headers).json() == {"resources": False, "analytics": True}
    assert client.get(base + "/resources", headers=headers).status_code == 404
    assert count_view(client, base, headers, "embedded") == 200
    assert count_view(client, base, headers, "standalone") == 200
    analytics = client.get(base + "/analytics", headers=headers)
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()["views"] == 2
    assert analytics.json()["authenticatedViewers"] == 1


@pytest.mark.e2e
@pytest.mark.parametrize("resources", [False, True])
def test_disabling_analytics_stops_counting_and_hides_it_regardless_of_resources(default_artifact, resources):
    client, base, headers, _ = default_artifact
    assert count_view(client, base, headers) == 200
    set_flags(client, headers, analytics=False, resources=resources)

    assert client.get(base + "/features", headers=headers).json() == {"resources": resources, "analytics": False}
    assert count_view(client, base, headers) == 404
    assert client.get(base + "/analytics", headers=headers).status_code == 404
    assert client.get(base + "/resources", headers=headers).status_code == (200 if resources else 404)

    # Turning it back on keeps what was counted before.
    set_flags(client, headers, analytics=True)
    assert client.get(base + "/analytics", headers=headers).json()["views"] == 1


@pytest.mark.e2e
def test_only_the_owner_is_offered_analytics_but_every_viewer_is_counted(default_artifact, invite_user_to_org):
    client, base, headers, report_id = default_artifact
    member = invite_user_to_org(org_id=headers["X-Organization-Id"], admin_token=headers["Authorization"].split()[1])
    member_headers = {**headers, "Authorization": "Bearer " + member["token"]}
    response = client.put(
        f"/api/reports/{report_id}/visibility/artifact", headers=headers, json={"visibility": "public"}
    )
    assert response.status_code == 200, response.text

    assert client.get(base + "/features", headers=member_headers).json()["analytics"] is False
    assert client.get(base + "/analytics", headers=member_headers).status_code == 403
    assert count_view(client, base, member_headers) == 200
    assert count_view(client, base, headers) == 200
    assert client.get(base + "/analytics", headers=headers).json()["authenticatedViewers"] == 2


@pytest.mark.e2e
def test_analytics_switch_is_admin_only(default_artifact, invite_user_to_org):
    client, base, headers, _ = default_artifact
    member = invite_user_to_org(org_id=headers["X-Organization-Id"], admin_token=headers["Authorization"].split()[1])
    member_headers = {**headers, "Authorization": "Bearer " + member["token"]}
    response = client.put(
        "/api/organization/settings",
        headers=member_headers,
        json={"config": {"enable_artifact_analytics": {"value": False}}},
    )
    assert response.status_code == 403
    assert client.get(base + "/features", headers=headers).json()["analytics"] is True
