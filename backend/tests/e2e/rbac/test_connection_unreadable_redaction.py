"""Which sources a crawl could not read is catalog internals: managers see it, members do not."""
import uuid

import pytest

from tests.e2e.test_connection_transient_failures import CHINOOK_DB, reindex_with_diagnostics

UNREADABLE_KEYS = ("unreadable_datasets", "unreadable_dataset_count")


def _listed(test_client, headers, connection_id):
    response = test_client.get("/api/connections", headers=headers)
    assert response.status_code == 200, response.text
    rows = [c for c in response.json() if c["id"] == connection_id]
    assert len(rows) == 1, response.text
    return rows[0]


@pytest.mark.e2e
@pytest.mark.parametrize("missing,denied", [("ghost_db", "locked_db"), ("gone_a", "denied_b")])
def test_connection_list_hides_unreadable_sources_from_members_but_not_managers(
    monkeypatch, test_client, bootstrap_admin, invite_user_to_org, create_connection, missing, denied,
):
    if not CHINOOK_DB.exists():
        pytest.skip(f"SQLite test database missing at {CHINOOK_DB}")
    admin = bootstrap_admin("admin")
    admin_headers = {"Authorization": f"Bearer {admin['token']}", "X-Organization-Id": admin["org_id"]}
    connection = create_connection(
        name=f"Src {uuid.uuid4().hex[:6]}", type="sqlite",
        config={"database": str(CHINOOK_DB)}, credentials={}, user_token=admin["token"], org_id=admin["org_id"],
    )
    shared = test_client.post(
        "/api/data_sources",
        json={"name": f"Agent {uuid.uuid4().hex[:6]}", "connection_id": connection["id"], "is_public": True},
        headers=admin_headers,
    )
    assert shared.status_code == 200, shared.text
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    member_headers = {"Authorization": f"Bearer {member['token']}", "X-Organization-Id": admin["org_id"]}

    job = reindex_with_diagnostics(monkeypatch, test_client, admin_headers, connection["id"], [
        {"name": missing, "reason": "not found"},
        {"name": denied, "reason": "AccessDeniedException"},
    ])
    assert job["status"] == "completed", job

    managed = _listed(test_client, admin_headers, connection["id"])
    assert {d["name"] for d in managed["indexing"]["stats"]["unreadable_datasets"]} == {missing, denied}
    assert managed["indexing"]["stats"]["unreadable_dataset_count"] == 2

    seen_by_member = _listed(test_client, member_headers, connection["id"])
    member_stats = (seen_by_member.get("indexing") or {}).get("stats") or {}
    assert not any(k in member_stats for k in UNREADABLE_KEYS)
    assert missing not in str(seen_by_member) and denied not in str(seen_by_member)
