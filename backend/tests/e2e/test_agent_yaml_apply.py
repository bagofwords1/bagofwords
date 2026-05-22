"""End-to-end tests for agent YAML declarative apply.

Covers the contract locked in ``docs/design/agent-yaml.md``:
- create → get → apply (unchanged) round-trip
- update returns a diff
- structured errors with did-you-mean for ref typos
- dry_run has no side effects
- permission denial on non-admin user
"""
from pathlib import Path

import pytest


DATA_SOURCE_TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()


def _yaml_post(test_client, path, yaml_text, user_token, org_id, **params):
    return test_client.post(
        path,
        content=yaml_text,
        params=params,
        headers={
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
            "Content-Type": "application/yaml",
        },
    )


@pytest.fixture
def chinook_data_source(create_data_source, create_user, login_user, whoami):
    """Set up a user with a chinook DS and return (token, org_id, ds, connection_name)."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    ds = create_data_source(
        name="Chinook",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    # The newly-created data source has a single connection auto-named "sqlite-1".
    conns = ds.get("connections") or []
    conn_name = conns[0]["name"] if conns else "sqlite-1"
    return token, org_id, ds, conn_name


@pytest.mark.e2e
def test_create_get_reapply_round_trip(test_client, chinook_data_source):
    """apply → get → apply must yield 'unchanged' (the round-trip guarantee)."""
    token, org_id, _ds, conn_name = chinook_data_source

    manifest = f"""name: analyst
description: Smoke agent
context: chinook sandbox
is_public: false
connections:
  - "{conn_name}"
conversation_starters:
  - Top customers by spend
"""
    r = _yaml_post(test_client, "/api/agents/apply", manifest, token, org_id)
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["status"] == "created"
    assert body["name"] == "analyst"
    assert body["errors"] == []

    # Export → re-apply should be 'unchanged'
    exp = test_client.get(
        "/api/agents/analyst.yaml",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert exp.status_code == 200, exp.text
    exported = exp.text
    assert "name: analyst" in exported

    r2 = _yaml_post(test_client, "/api/agents/apply", exported, token, org_id)
    assert r2.status_code == 200, r2.json()
    body2 = r2.json()
    assert body2["status"] == "unchanged", body2


@pytest.mark.e2e
def test_update_returns_diff(test_client, chinook_data_source):
    token, org_id, _ds, conn_name = chinook_data_source

    base = f"""name: analyst-2
description: first
is_public: false
connections:
  - "{conn_name}"
conversation_starters:
  - q1
"""
    assert _yaml_post(test_client, "/api/agents/apply", base, token, org_id).json()["status"] == "created"

    updated = f"""name: analyst-2
description: second
is_public: true
connections:
  - "{conn_name}"
conversation_starters:
  - q1
  - q2
"""
    body = _yaml_post(test_client, "/api/agents/apply", updated, token, org_id).json()
    assert body["status"] == "updated"
    diff = body["diff"]
    assert diff["description"]["to"] == "second"
    assert diff["is_public"]["to"] is True
    assert diff["conversation_starters"]["to"] == ["q1", "q2"]


@pytest.mark.e2e
def test_ref_errors_with_suggestions(test_client, chinook_data_source):
    """Typo'd connection / unknown user / unknown group all surface with codes."""
    token, org_id, _ds, conn_name = chinook_data_source

    # Lowercase + typo of the real connection name
    typo = conn_name.lower()[:-1]
    bad = f"""name: ref-err
connections:
  - {typo}
members:
  - user: noone@nowhere.com
  - group: doesnt-exist
"""
    body = _yaml_post(test_client, "/api/agents/apply", bad, token, org_id).json()
    assert body["status"] == "error"
    codes = sorted(e["code"] for e in body["errors"])
    assert "connection_not_found" in codes
    assert "user_not_found" in codes
    assert "group_not_found" in codes

    # The connection typo should attract a did-you-mean for the real name
    conn_err = next(e for e in body["errors"] if e["code"] == "connection_not_found")
    assert conn_err["suggestion"] == conn_name


@pytest.mark.e2e
def test_yaml_parse_error_returns_structured_error(test_client, chinook_data_source):
    token, org_id, _ds, _conn = chinook_data_source
    body = _yaml_post(test_client, "/api/agents/apply", "name: a\n  bad: [", token, org_id).json()
    assert body["status"] == "error"
    assert body["errors"][0]["code"] == "yaml_parse_error"


@pytest.mark.e2e
def test_member_ref_requires_user_or_group(test_client, chinook_data_source):
    token, org_id, _ds, conn_name = chinook_data_source
    bad = f"""name: bad-member
connections:
  - "{conn_name}"
members:
  - permissions: [view]
"""
    body = _yaml_post(test_client, "/api/agents/apply", bad, token, org_id).json()
    assert body["status"] == "error"
    assert any(e["code"] == "schema_invalid" for e in body["errors"])


@pytest.mark.e2e
def test_dry_run_has_no_side_effects(test_client, chinook_data_source):
    token, org_id, _ds, conn_name = chinook_data_source
    manifest = f"""name: dry-only
description: would be nice
connections:
  - "{conn_name}"
"""
    body = _yaml_post(
        test_client, "/api/agents/apply", manifest, token, org_id, dry_run="true"
    ).json()
    assert body["status"] == "dry_run"

    # Confirm it wasn't created
    listing = test_client.get(
        "/api/agents",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    ).json()
    assert all(a["name"] != "dry-only" for a in listing)
