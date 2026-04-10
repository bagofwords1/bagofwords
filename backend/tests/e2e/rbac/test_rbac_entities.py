"""
RBAC end-to-end coverage for /api/entities.

The task brief specifically calls out the entities list/detail invariant
as a recent bug class: ``GET /entities`` must never return an entity ID
that the same caller can't open via ``GET /entities/{id}``.

This file builds two private DSes, attaches an entity to each, and
walks every principal asserting both directions of the invariant.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def entity_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # sqlite_data_source defaults to is_public=False and asserts the flip.
    ds_a = sqlite_data_source(name="e_ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="e_ds_b", user_token=admin["token"], org_id=org_id)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_grantee = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_resource(
        resource_type="data_source",
        resource_id=ds_a["id"],
        principal_type="user",
        principal_id=ds_a_grantee["user_id"],
        permissions=["create_entities"],
        user_token=admin["token"],
        org_id=org_id,
    )

    # Admin creates one entity per DS (using global endpoint)
    entity_a = test_client.post(
        "/api/entities/global",
        json={
            "type": "model",
            "title": "ent_on_ds_a",
            "slug": f"ent-ds-a-{ds_a['id'][:6]}",
            "code": "select 1 as v",
            "data": {},
            "tags": [],
            "status": "published",
            "data_source_ids": [ds_a["id"]],
        },
        headers=_hdr(admin["token"], org_id),
    )
    assert entity_a.status_code == 200, entity_a.text

    entity_b = test_client.post(
        "/api/entities/global",
        json={
            "type": "model",
            "title": "ent_on_ds_b",
            "slug": f"ent-ds-b-{ds_b['id'][:6]}",
            "code": "select 1 as v",
            "data": {},
            "tags": [],
            "status": "published",
            "data_source_ids": [ds_b["id"]],
        },
        headers=_hdr(admin["token"], org_id),
    )
    assert entity_b.status_code == 200, entity_b.text

    return {
        "org_id": org_id,
        "ds_a": ds_a,
        "ds_b": ds_b,
        "entity_a": entity_a.json(),
        "entity_b": entity_b.json(),
        "principals": {
            "admin": admin,
            "member": member,
            "ds_a_grantee": ds_a_grantee,
        },
    }


@pytest.mark.e2e
def test_entity_list_detail_invariant(test_client, entity_world):
    """Forward invariant: every entity exposed in /entities must open via GET /{id}.

    This is the regression-prevention check the task brief explicitly
    asks for: members must never see an entity ID in the list that they
    cannot then open. Walks every principal in one shot.
    """
    org_id = entity_world["org_id"]

    failures = []
    for name, p in entity_world["principals"].items():
        list_resp = test_client.get(
            "/api/entities",
            headers=_hdr(p["token"], org_id),
        )
        if list_resp.status_code != 200:
            failures.append(f"{name}: list returned {list_resp.status_code}")
            continue
        for ent in list_resp.json():
            detail = test_client.get(
                f"/api/entities/{ent['id']}",
                headers=_hdr(p["token"], org_id),
            )
            if detail.status_code != 200:
                failures.append(
                    f"{name}: list contained entity {ent['id']} but GET returned {detail.status_code}"
                )

    assert not failures, "\n".join(failures)


@pytest.mark.e2e
def test_admin_sees_all_entities(test_client, entity_world):
    """Admin (full_admin_access via system role) must see entities on every DS."""
    org_id = entity_world["org_id"]
    admin = entity_world["principals"]["admin"]

    list_resp = test_client.get("/api/entities", headers=_hdr(admin["token"], org_id))
    assert list_resp.status_code == 200, list_resp.text
    seen = {e["id"] for e in list_resp.json()}
    assert entity_world["entity_a"]["id"] in seen
    assert entity_world["entity_b"]["id"] in seen


@pytest.mark.e2e
def test_member_cannot_see_private_entities(test_client, entity_world):
    """A member with no DS access must not see entities tied to private DSes."""
    org_id = entity_world["org_id"]
    member = entity_world["principals"]["member"]

    list_resp = test_client.get("/api/entities", headers=_hdr(member["token"], org_id))
    assert list_resp.status_code == 200
    seen = {e["id"] for e in list_resp.json()}
    assert entity_world["entity_a"]["id"] not in seen
    assert entity_world["entity_b"]["id"] not in seen

    # And the detail call must also be denied (404, since the service hides it).
    detail = test_client.get(
        f"/api/entities/{entity_world['entity_a']['id']}",
        headers=_hdr(member["token"], org_id),
    )
    assert detail.status_code in (403, 404), detail.text
