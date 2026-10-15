"""PUT /instructions/{id} reports — and versions — the references it just saved.

The route loads the instruction (references included) before the service
rewrites the reference rows with a bulk DELETE/INSERT. The response and the
version snapshot were both built from that already-loaded collection, so they
carried the references from BEFORE the update. The Knowledge explorer re-seeds
its draft from the PUT response, so a table the user had just removed showed up
ticked again, and the next autosave persisted it back.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _ref(table):
    return {"object_type": "datasource_table", "object_id": table["id"],
            "display_text": table["name"], "relation_type": "scope"}


def _ref_ids(refs):
    return sorted(str(r["object_id"]) for r in refs or [])


@pytest.fixture
def ref_world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source,
              grant_resource, refresh_schema):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds = sqlite_data_source(name="refs_ds", user_token=admin["token"], org_id=org_id)
    refresh_schema(data_source_id=ds["id"], user_token=admin["token"], org_id=org_id)
    r = test_client.get(f"/api/data_sources/{ds['id']}/full_schema",
                        params={"page": 1, "page_size": 500},
                        headers=_hdr(admin["token"], org_id))
    assert r.status_code == 200, r.text[:400]
    tables = r.json()["tables"]
    assert len(tables) >= 5, "need several tables to vary the reference set"

    # Per-agent instruction manager: not an org admin.
    author = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant = grant_resource(
        resource_type="data_source", resource_id=ds["id"],
        principal_type="user", principal_id=author["user_id"],
        permissions=["manage_instructions"],
        user_token=admin["token"], org_id=org_id,
    )
    assert grant.status_code == 200, grant.json()
    return {"org_id": org_id, "ds_id": ds["id"], "tables": tables,
            "principals": {"admin": admin, "author": author}}


@pytest.mark.e2e
@pytest.mark.parametrize("role", ["admin", "author"])
def test_put_returns_and_versions_the_references_it_saved(test_client, ref_world, role):
    org_id, ds_id, t = ref_world["org_id"], ref_world["ds_id"], ref_world["tables"]
    headers = _hdr(ref_world["principals"][role]["token"], org_id)

    created = test_client.post(
        "/api/instructions",
        json={"text": "Scope alerts by severity", "status": "published",
              "category": "general", "data_source_ids": [ds_id],
              "references": [_ref(t[0]), _ref(t[1]), _ref(t[2])]},
        headers=headers,
    )
    assert created.status_code == 200, created.text[:400]
    iid = created.json()["id"]

    # Remove one, remove another, add new ones, swap all, clear.
    for selection in ([t[1], t[2]], [t[2]], [t[2], t[3], t[4]], [t[0]], []):
        want = _ref_ids([_ref(x) for x in selection])
        put = test_client.put(
            f"/api/instructions/{iid}",
            json={"references": [_ref(x) for x in selection]},
            headers=headers,
        )
        assert put.status_code == 200, put.text[:400]
        body = put.json()
        assert _ref_ids(body["references"]) == want, "PUT response is not what was saved"

        got = test_client.get(f"/api/instructions/{iid}", headers=headers)
        assert got.status_code == 200, got.text[:400]
        assert _ref_ids(got.json()["references"]) == want

        vid = body.get("current_version_id")
        assert vid, "a references change must cut a version"
        ver = test_client.get(f"/api/instructions/{iid}/versions/{vid}", headers=headers)
        assert ver.status_code == 200, ver.text[:400]
        assert _ref_ids(ver.json()["references_json"]) == want, "version snapshot lags the save"
