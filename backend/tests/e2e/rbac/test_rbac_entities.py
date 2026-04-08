"""
Entity RBAC tests.

Permissions:

    view_entities         (member + admin)
    create_entities       (admin only)
    update_entities       (admin only)
    delete_entities       (admin only)
    suggest_entities      (member + admin)
    withdraw_entities     (member + admin)
    approve_entities      (admin only)
    reject_entities       (admin only)

The most important invariant: GET /entities (list) must not return entities
the caller cannot GET /entities/{id} on. The list endpoint applies a
data-source-access filter; we exercise that against principals with assorted
DS grants.
"""
import pytest


def _h(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.mark.e2e
def test_entity_list_detail_invariant_no_ds(
    test_client, rbac_principals, create_global_entity,
):
    """Every entity returned by GET /entities must be GET-able for the same
    caller via GET /entities/{id}. Cross-checks the list-time filter against
    the detail-time filter for every principal.

    Note: this variant intentionally creates entities WITHOUT data source
    associations because there is a separate, pre-existing bug in
    ``EntityService.create_entity`` where the post-commit ``db.refresh``
    expires the ``data_sources`` relationship, then the route serializer
    walks it via ``EntitySchema.model_validate(entity)`` and hits
    ``sqlalchemy.exc.MissingGreenlet`` because lazy loading runs in the
    sync serializer thread. The DS-bound variant is xfailed below.
    """
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]

    # Admin seeds entities WITHOUT data_source_ids — these go through the
    # working code path.
    create_global_entity(
        title="Entity-NoDS-1", slug="ent-no-ds-1",
        data_source_ids=[], status="published",
        user_token=admin["token"], org_id=org_id,
    )
    create_global_entity(
        title="Entity-NoDS-2", slug="ent-no-ds-2",
        data_source_ids=[], status="published",
        user_token=admin["token"], org_id=org_id,
    )

    failures = []
    for name in ("admin", "member", "ds_a_member", "ds_b_member"):
        p = rbac_principals["principals"][name]
        list_resp = test_client.get(
            "/api/entities", headers=_h(p["token"], org_id)
        )
        assert list_resp.status_code == 200, (
            f"{name} list entities: {list_resp.status_code} {list_resp.text[:200]}"
        )
        for e in list_resp.json():
            d = test_client.get(
                f"/api/entities/{e['id']}", headers=_h(p["token"], org_id)
            )
            if d.status_code != 200:
                failures.append(
                    f"{name} sees entity {e['id']} ({e.get('title')}) in list "
                    f"but GET returned {d.status_code}"
                )

    assert not failures, (
        "Entity list/detail invariant violations:\n" + "\n".join(failures)
    )


@pytest.mark.e2e
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing bug in EntityService.create_entity: when "
        "data_source_ids is provided the response serializer hits "
        "sqlalchemy.exc.MissingGreenlet because the post-refresh "
        "data_sources relationship is lazy-loaded in the sync serializer "
        "thread. Needs an eager selectinload after refresh; tracked here "
        "until fixed."
    ),
)
def test_entity_create_with_data_source_ids_does_not_500(
    test_client, rbac_principals, create_global_entity,
):
    """Documents the MissingGreenlet bug in create_entity when DS ids are passed."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    ds_a_id = rbac_principals["ds_a"]["id"]
    create_global_entity(
        title="EntityWithDS", slug="ent-with-ds",
        data_source_ids=[ds_a_id], status="published",
        user_token=admin["token"], org_id=org_id,
    )


@pytest.mark.e2e
def test_entity_create_update_delete_admin_only(
    test_client, rbac_principals, create_global_entity,
):
    """create/update/delete on /entities require admin role."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    member = rbac_principals["principals"]["member"]

    # Admin can create
    admin_ent = create_global_entity(
        title="AdminCreated", slug="admin-ent", status="published",
        user_token=admin["token"], org_id=org_id,
    )

    # Member POST /entities -> 403 (create_entities admin-only)
    member_create = test_client.post(
        "/api/entities",
        json={
            "type": "model",
            "title": "MemberMade",
            "slug": "member-ent",
            "code": "select 1",
            "data": {},
            "status": "draft",
            "tags": [],
            "data_source_ids": [],
        },
        headers=_h(member["token"], org_id),
    )
    assert member_create.status_code == 403, member_create.json()

    # Member PUT -> 403
    member_update = test_client.put(
        f"/api/entities/{admin_ent['id']}",
        json={"title": "Hacked"},
        headers=_h(member["token"], org_id),
    )
    assert member_update.status_code == 403, member_update.json()

    # Member DELETE -> 403
    member_delete = test_client.delete(
        f"/api/entities/{admin_ent['id']}", headers=_h(member["token"], org_id)
    )
    assert member_delete.status_code == 403, member_delete.json()


@pytest.mark.e2e
def test_outsider_cannot_access_entities(test_client, rbac_principals):
    """Outsider gets 403 from list/detail/mutate."""
    org_id = rbac_principals["org_id"]
    outsider = rbac_principals["principals"]["outsider"]
    headers = _h(outsider["token"], org_id)

    list_resp = test_client.get("/api/entities", headers=headers)
    assert list_resp.status_code in (403, 404), list_resp.status_code

    create_resp = test_client.post(
        "/api/entities",
        json={
            "type": "model",
            "title": "Evil",
            "slug": "evil-ent",
            "code": "select 1",
            "data": {},
            "status": "published",
            "tags": [],
            "data_source_ids": [],
        },
        headers=headers,
    )
    assert create_resp.status_code in (403, 404), create_resp.status_code
