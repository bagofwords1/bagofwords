"""Per-member Qlik identity under "Require user authentication" (member_mapped).

Contract under test:

* turning on per-user auth for Qlik on-prem means binding identity fields of
  the connection form (User ID / User Directory) to member attributes; at
  least one binding is required, and only fields the connector flags as
  bindable can be bound (no Postgres role switching);
* each signed-in member acts as the Qlik account their ADMIN-SET attribute
  names, through both credential resolvers every query path uses — never one
  they can edit themselves (display name, email, a stored per-user row);
* a member without the attribute is BLOCKED (403) — never handed the
  certificate's own identity, admins included (no owner/admin fallback);
* no signed-in user (indexing) keeps the saved identity;
* the preview / per-member test are full-admin only; binding changes are
  audited.

Only the remote probe is mocked (there is no Qlik server); clients are
constructed for real and never connect.
"""

import asyncio
import uuid

import pytest

from app.services.connection_service import ConnectionService


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def probe(monkeypatch):
    """Connection tests probe a real server; record what they were given."""
    calls = []

    async def _ok(self, **kwargs):
        calls.append(kwargs)
        return {"success": True, "message": "ok", "reachable": True}

    monkeypatch.setattr(ConnectionService, "test_connection_params", _ok)
    return calls


@pytest.fixture
def org(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    def invite(custom_attributes=None):
        email = f"mem_{uuid.uuid4().hex[:8]}@test.com"
        body = {"organization_id": org_id, "email": email, "role": "member"}
        if custom_attributes is not None:
            body["custom_attributes"] = custom_attributes
        r = test_client.post(f"/api/organizations/{org_id}/members", json=body, headers=_h(admin_token, org_id))
        assert r.status_code == 200, r.json()
        create_user(email=email, password="test123")
        token = login_user(email, "test123")
        return {"email": email, "membership_id": r.json()["id"], "token": token, "user_id": whoami(token)["id"]}

    return {"id": org_id, "admin": admin_token, "admin_user_id": whoami(admin_token)["id"], "invite": invite}


QLIK_CREDS = {"client_cert": "cert", "client_key": "key", "user_directory": "CORP"}


def _create_qlik(test_client, org, bindings=None, auth_policy="user_required"):
    body = {
        "name": f"qlik-{uuid.uuid4().hex[:6]}",
        "type": "qlik_sense_onprem",
        "config": {"server_url": "https://qlik.example.com"},
        "credentials": QLIK_CREDS,
        "auth_policy": auth_policy,
    }
    if bindings is not None:
        body["member_bindings"] = bindings
    return test_client.post("/api/connections", json=body, headers=_h(org["admin"], org["id"]))


def _run(coro_fn):
    from app.dependencies import async_session_maker

    async def run():
        async with async_session_maker() as db:
            return await coro_fn(db)

    return asyncio.run(run())


async def _load(db, connection_id, user_id):
    from sqlalchemy import select

    from app.models.connection import Connection
    from app.models.user import User

    connection = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
    user = None
    if user_id:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    return connection, user


def _client_for(connection_id, user_id):
    """The connection's client as ``user_id`` (None = indexing context),
    through the resolver the agent's query paths use."""
    async def go(db):
        connection, user = await _load(db, connection_id, user_id)
        return await ConnectionService().construct_client(db, connection, user)

    return _run(go)


def _ds_service_creds(connection_id, user_id):
    """The data-source service's per-connection resolver (agent tool path)."""
    from app.services.data_source_service import DataSourceService

    async def go(db):
        connection, user = await _load(db, connection_id, user_id)
        return await DataSourceService().resolve_credentials_for_connection(db, connection, None, user)

    return _run(go)


def _status_for(connection_id, user_id):
    from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService

    async def go(db):
        connection, user = await _load(db, connection_id, user_id)
        return await UserDataSourceCredentialsService().build_user_status_for_connection(db, connection, user)

    return _run(go)


def _blocked(connection_id, user_id):
    from app.errors import AppError

    with pytest.raises(AppError) as exc:
        _client_for(connection_id, user_id)
    assert exc.value.status_code == 403
    return exc.value


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_per_user_auth_on_qlik_needs_a_field_mapped_to_a_member_attribute(test_client, org, probe):
    r = _create_qlik(test_client, org)
    assert r.status_code == 400, r.json()
    assert r.json()["error_code"] == "member_identity.invalid"

    r = _create_qlik(test_client, org, {"user_id": "qlik_user_id"})
    assert r.status_code == 200, r.json()
    assert r.json()["allowed_user_auth_modes"] == ["member_mapped"]
    detail = test_client.get(f"/api/connections/{r.json()['id']}", headers=_h(org["admin"], org["id"])).json()
    assert detail["member_bindings"] == {"user_id": "qlik_user_id"}
    # The fixed identity fields are shown (not secret); the key never is.
    assert detail["credentials_meta"]["user_directory"] == "CORP"
    assert "client_key" not in (detail["credentials_meta"] or {})

    # Clearing the last binding while per-user auth stays on is refused.
    r = test_client.put(
        f"/api/connections/{r.json()['id']}", json={"member_bindings": {}}, headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 400, r.json()


@pytest.mark.e2e
@pytest.mark.parametrize("bindings", [
    {"client_key": "qlik_user_id"},      # a secret, not an identity field
    {"server_url": "qlik_user_id"},      # config, not credentials
    {"user_id": "has space"},            # malformed attribute key
])
def test_only_flagged_identity_fields_can_be_bound(test_client, org, probe, bindings):
    r = _create_qlik(test_client, org, bindings)
    assert r.status_code == 400, r.json()
    assert r.json()["error_code"] == "member_identity.invalid"


@pytest.mark.e2e
def test_postgres_offers_no_member_bindings(test_client, org, probe):
    """Review finding 2: no SET ROLE-style identity on a shared login."""
    body = {
        "name": f"pg-{uuid.uuid4().hex[:6]}",
        "type": "postgresql",
        "config": {"host": "127.0.0.1", "port": 1, "database": "acme"},
        "credentials": {"user": "bow_gateway", "password": "pw"},
        "auth_policy": "system_only",
        "member_bindings": {"user": "pg_role"},
    }
    r = test_client.post("/api/connections", json=body, headers=_h(org["admin"], org["id"]))
    assert r.status_code == 400, r.json()
    assert r.json()["error_code"] == "member_identity.invalid"
    fields = test_client.get(
        "/api/data_sources/postgresql/fields", headers=_h(org["admin"], org["id"]),
    ).json()
    assert not any(p.get("ui:memberBindable") for p in fields["credentials"]["properties"].values())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_each_member_acts_as_their_admin_mapped_qlik_account(test_client, org, probe):
    dana = org["invite"]({"qlik_user_id": "dana"})
    noa = org["invite"]({"qlik_user_id": "noa", "qlik_dir": "SALES"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()

    for member, expected in ((dana, "dana"), (noa, "noa")):
        client = _client_for(conn["id"], member["user_id"])
        assert (client.user_directory, client.user_id) == ("CORP", expected)
        assert client._acting_user() == f"UserDirectory=CORP; UserId={expected}"
        creds = _ds_service_creds(conn["id"], member["user_id"])
        assert (creds["user_directory"], creds["user_id"], creds["client_cert"]) == ("CORP", expected, "cert")

    # Bind the directory too: both fields now come from the member.
    r = test_client.put(
        f"/api/connections/{conn['id']}",
        json={"member_bindings": {"user_id": "qlik_user_id", "user_directory": "qlik_dir"}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    client = _client_for(conn["id"], noa["user_id"])
    assert (client.user_directory, client.user_id) == ("SALES", "noa")
    _blocked(conn["id"], dana["user_id"])  # dana has no qlik_dir

    # Indexing (no user) keeps the saved, shared identity.
    client = _client_for(conn["id"], None)
    assert (client.user_directory, client.user_id) == ("CORP", None)


@pytest.mark.e2e
def test_a_member_without_the_attribute_is_blocked_not_given_the_certificate(test_client, org, probe):
    avi = org["invite"]({"other": "x"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()

    err = _blocked(conn["id"], avi["user_id"])
    assert err.error_code == "member_identity.unresolved"
    assert err.params == {"attributes": "qlik_user_id"}
    # The org admin (also the connection's creator) gets no fallback either.
    assert _blocked(conn["id"], org["admin_user_id"]).error_code == "member_identity.unresolved"

    # ...and nothing for them to "connect": an admin has to act.
    status = _status_for(conn["id"], avi["user_id"])
    assert (status.auth_mode, status.has_user_credentials, status.effective_auth) == ("member_mapped", False, "none")

    # Once an admin sets it, the member has access without doing anything.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{avi['membership_id']}",
        json={"custom_attributes": {"qlik_user_id": "avi"}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    status = _status_for(conn["id"], avi["user_id"])
    assert (status.has_user_credentials, status.effective_auth) == (True, "user")
    assert _client_for(conn["id"], avi["user_id"]).user_id == "avi"


@pytest.mark.e2e
def test_members_cannot_change_their_identity_through_their_profile(test_client, org, probe):
    """Review finding 3: display name and email are member-editable, so they
    are never identity sources."""
    dana = org["invite"]({"qlik_user_id": "dana"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()

    r = test_client.patch(
        "/api/users/me", json={"name": "ceo", "email": f"ceo_{uuid.uuid4().hex[:6]}@test.com"},
        headers=_h(dana["token"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _client_for(conn["id"], dana["user_id"]).user_id == "dana"

    # Nor through their own attributes.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{dana['membership_id']}",
        json={"custom_attributes": {"qlik_user_id": "ceo"}},
        headers=_h(dana["token"], org["id"]),
    )
    assert r.status_code == 403
    assert _client_for(conn["id"], dana["user_id"]).user_id == "dana"


@pytest.mark.e2e
def test_stored_per_user_identities_are_ignored(test_client, org, probe):
    """The removed "identity" mode let members save the Qlik account to act
    as. Such a row — or any stored per-user row — must not override the
    admin's mapping."""
    from app.models.user_connection_credentials import UserConnectionCredentials

    dana = org["invite"]({"qlik_user_id": "dana"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()

    async def plant(db):
        row = UserConnectionCredentials(
            connection_id=conn["id"], user_id=dana["user_id"], organization_id=org["id"],
            auth_mode="identity", is_active=True, is_primary=True,
        )
        row.encrypt_credentials({"user_directory": "CORP", "user_id": "ceo"})
        db.add(row)
        await db.commit()

    _run(plant)
    assert _client_for(conn["id"], dana["user_id"]).user_id == "dana"
    assert _ds_service_creds(conn["id"], dana["user_id"])["user_id"] == "dana"

    # And the API refuses to store one.
    r = test_client.post(
        f"/api/connections/{conn['id']}/my-credentials",
        json={"auth_mode": "identity", "credentials": {"user_directory": "CORP", "user_id": "ceo"}},
        headers=_h(dana["token"], org["id"]),
    )
    assert r.status_code in (400, 403, 404, 422), r.json()


@pytest.mark.e2e
def test_a_mapped_connection_without_bindings_fails_closed(test_client, org, probe):
    """What a legacy per-user Qlik connection looks like after the migration:
    member_mapped with nothing bound yet."""
    from sqlalchemy import update

    from app.models.connection import Connection

    dana = org["invite"]({"qlik_user_id": "dana"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()

    async def strip(db):
        await db.execute(update(Connection).where(Connection.id == conn["id"]).values(member_bindings=None))
        await db.commit()

    _run(strip)
    assert _blocked(conn["id"], dana["user_id"]).error_code == "member_identity.not_configured"


@pytest.mark.e2e
def test_turning_per_user_auth_off_runs_everyone_as_the_saved_identity(test_client, org, probe):
    dana = org["invite"]({"qlik_user_id": "dana"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()
    r = test_client.put(
        f"/api/connections/{conn['id']}", json={"auth_policy": "system_only"}, headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _client_for(conn["id"], dana["user_id"]).user_id is None
    # The mapping is kept for when it is turned back on.
    r = test_client.put(
        f"/api/connections/{conn['id']}", json={"auth_policy": "user_required"}, headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _client_for(conn["id"], dana["user_id"]).user_id == "dana"


# ---------------------------------------------------------------------------
# Admin tools
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_admin_previews_and_tests_who_acts_as_whom(test_client, org, probe):
    dana = org["invite"]({"qlik_user_id": "dana"})
    avi = org["invite"]({})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()
    ah = _h(org["admin"], org["id"])

    r = test_client.post(f"/api/connections/{conn['id']}/member-identity/preview", json={}, headers=ah)
    assert r.status_code == 200, r.json()
    rows = {m["email"]: m for m in r.json()["members"]}
    assert rows[dana["email"]]["status"] == "ok"
    assert rows[dana["email"]]["identity"] == {"user_directory": "CORP", "user_id": "dana"}
    assert rows[avi["email"]]["status"] == "blocked"
    assert rows[avi["email"]]["missing"] == ["qlik_user_id"]
    assert "qlik_user_id" in r.json()["attribute_keys"]

    # Unsaved bindings preview without being saved.
    r = test_client.post(
        f"/api/connections/{conn['id']}/member-identity/preview",
        json={"member_bindings": {"user_id": "nope"}}, headers=ah,
    )
    assert {m["status"] for m in r.json()["members"]} == {"blocked"}

    probe.clear()
    r = test_client.post(f"/api/connections/{conn['id']}/member-identity/test", json={"user_id": dana["user_id"]}, headers=ah)
    assert r.status_code == 200 and r.json()["success"] is True, r.json()
    assert probe[-1]["credentials"]["user_id"] == "dana"
    assert probe[-1]["credentials"]["client_cert"] == "cert"

    r = test_client.post(f"/api/connections/{conn['id']}/member-identity/test", json={"user_id": avi["user_id"]}, headers=ah)
    assert r.json()["success"] is False
    assert len(probe) == 1  # blocked before anything was sent


@pytest.mark.e2e
def test_preview_and_member_test_are_full_admin_only(test_client, org, probe):
    """They reveal member attributes, which only full admins may read."""
    r = test_client.post(
        f"/api/organizations/{org['id']}/roles",
        json={"name": f"conn-admin-{uuid.uuid4().hex[:6]}", "permissions": ["manage_connections"]},
        headers=_h(org["admin"], org["id"]),
    )
    if r.status_code == 402:
        pytest.skip("custom roles need an enterprise license in this environment")
    assert r.status_code == 200, r.json()
    manager = org["invite"]({})
    r2 = test_client.post(
        f"/api/organizations/{org['id']}/role-assignments",
        json={"role_id": r.json()["id"], "principal_type": "user", "principal_id": manager["user_id"]},
        headers=_h(org["admin"], org["id"]),
    )
    assert r2.status_code == 200, r2.json()
    dana = org["invite"]({"qlik_user_id": "dana"})
    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()
    mh = _h(manager["token"], org["id"])

    assert test_client.get(f"/api/connections/{conn['id']}", headers=mh).status_code == 200
    r = test_client.post(f"/api/connections/{conn['id']}/member-identity/preview", json={}, headers=mh)
    assert r.status_code == 403
    r = test_client.post(f"/api/connections/{conn['id']}/member-identity/test", json={"user_id": dana["user_id"]}, headers=mh)
    assert r.status_code == 403
    for member in (dana,):
        r = test_client.post(f"/api/connections/{conn['id']}/member-identity/preview", json={}, headers=_h(member["token"], org["id"]))
        assert r.status_code == 403


@pytest.mark.e2e
def test_binding_changes_are_audited(test_client, org, probe):
    from sqlalchemy import select

    from app.ee.audit.models import AuditLog

    conn = _create_qlik(test_client, org, {"user_id": "qlik_user_id"}).json()
    ah = _h(org["admin"], org["id"])
    r = test_client.put(
        f"/api/connections/{conn['id']}",
        json={"member_bindings": {"user_id": "employee_id"}}, headers=ah,
    )
    assert r.status_code == 200, r.json()
    # A save that changes nothing is not logged.
    assert test_client.put(
        f"/api/connections/{conn['id']}", json={"member_bindings": {"user_id": "employee_id"}}, headers=ah,
    ).status_code == 200

    async def rows(db):
        return (await db.execute(
            select(AuditLog).where(
                AuditLog.resource_id == conn["id"],
                AuditLog.action == "connection.member_identity_changed",
            )
        )).scalars().all()

    logged = _run(rows)
    assert len(logged) == 1
    assert logged[0].details["before"] == {"enabled": True, "bindings": {"user_id": "qlik_user_id"}}
    assert logged[0].details["after"] == {"enabled": True, "bindings": {"user_id": "employee_id"}}
