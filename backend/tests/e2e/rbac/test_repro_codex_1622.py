"""Reproduction only (Codex review comment 2 on #1622): a non-admin owner reloading a
mixed agent — one per-user connection that takes no login (SQLite) and one per-user
Postgres connection they have not connected yet. Expected (pre-#1622 behaviour and the
reviewer's ask): 200 with the SQLite tables. Claimed: the Reload fails outright."""
import uuid
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

SHARED_LOGIN = "shared_service_account"
CHINOOK_PATH = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def logins(monkeypatch):
    seen = []

    def engine_for(uri, **_):
        seen.append(make_url(uri).username)
        return sqlalchemy.create_engine("sqlite://", poolclass=StaticPool)

    monkeypatch.setattr("app.data_sources.clients.postgresql_client.get_engine", engine_for)
    return seen


@pytest.mark.e2e
@pytest.mark.xfail(strict=True, reason="Codex comment 2 on #1622: a mixed agent's Reload 403s for a non-admin owner; "
                                       "passed (200, 11 SQLite tables) on 10b0aff, the commit before #1622")
def test_repro_mixed_agent_reload_by_non_admin_owner(
    logins, test_client, bootstrap_admin, invite_user_to_org, create_data_source, create_connection,
):
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    owner = invite_user_to_org(org_id=org_id, admin_token=admin["token"], role="admin")
    owner_h = _headers(owner["token"], org_id)

    ds = create_data_source(name=f"Mixed {uuid.uuid4().hex[:8]}", type="sqlite",
                            config={"database": str(CHINOOK_PATH)}, credentials={},
                            auth_policy="user_required", user_token=owner["token"], org_id=org_id)
    pg = create_connection(name=f"pg {uuid.uuid4().hex[:6]}", type="postgresql",
                           config={"host": "localhost", "port": 5432, "database": "warehouse"},
                           credentials={"user": SHARED_LOGIN, "password": "not-a-real-password"},
                           auth_policy="user_required", user_token=owner["token"], org_id=org_id)
    attached = test_client.post(f"/api/data_sources/{ds['id']}/connections/{pg['id']}?sync_tables=false", headers=owner_h)
    assert attached.status_code == 200, attached.text
    conns = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=owner_h).json()
    assert {c["type"] for c in conns} == {"sqlite", "postgresql"}, conns
    assert all(c["auth_policy"] == "user_required" for c in conns), conns

    before = test_client.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=owner_h)
    assert before.status_code == 200, f"admin owner reload failed: {before.text}"
    print("\nADMIN OWNER RELOAD:", before.status_code, "tables:", len(before.json()))

    demoted = test_client.put(f"/api/organizations/{org_id}/members/{owner['membership_id']}",
                              json={"role": "member"}, headers=_headers(admin["token"], org_id))
    assert demoted.status_code == 200 and demoted.json()["role"] == "member", demoted.json()
    logins.clear()

    reloaded = test_client.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=owner_h)
    print("NON-ADMIN OWNER RELOAD:", reloaded.status_code, reloaded.text[:300])
    if reloaded.status_code == 200:
        print("TABLES RETURNED:", len(reloaded.json()), sorted({t.get("name") for t in reloaded.json()})[:5])
    print("LOGINS THAT REACHED THE PG DRIVER DURING THAT RELOAD:", logins)

    assert reloaded.status_code == 200, reloaded.text
    assert len(reloaded.json()) > 0, "Reload lost the SQLite tables"
