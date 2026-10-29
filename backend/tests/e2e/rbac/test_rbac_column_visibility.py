"""Changing which columns an agent sees is a table-management action: an org
member without manage rights on the agent is refused, and nothing changes."""
from pathlib import Path

import pytest

CHINOOK = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.mark.e2e
def test_member_cannot_change_column_visibility(
    test_client, create_user, login_user, whoami, create_data_source,
    refresh_schema, invite_user_to_org,
):
    if not CHINOOK.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK}")
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    ds = create_data_source(
        name="Column visibility rbac", type="sqlite",
        config={"database": str(CHINOOK)}, credentials={},
        user_token=admin_token, org_id=org_id,
    )
    refresh_schema(data_source_id=ds["id"], user_token=admin_token, org_id=org_id)

    def _target():
        r = test_client.get(f"/api/data_sources/{ds['id']}/full_schema",
                            params={"page": 1, "page_size": 500},
                            headers=_hdr(admin_token, org_id))
        assert r.status_code == 200, r.text
        return r.json()["tables"][0]

    target = _target()
    member = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    body = {"excluded_columns": {target["id"]: [target["columns"][0]["name"]]}}

    denied = test_client.put(f"/api/data_sources/{ds['id']}/update_tables_status",
                             json=body, headers=_hdr(member["token"], org_id))
    assert denied.status_code == 403
    assert not _target()["excluded_columns"]

    allowed = test_client.put(f"/api/data_sources/{ds['id']}/update_tables_status",
                              json=body, headers=_hdr(admin_token, org_id))
    assert allowed.status_code == 200, allowed.text
    assert _target()["excluded_columns"] == [target["columns"][0]["name"]]
