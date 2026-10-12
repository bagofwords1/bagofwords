"""Admin-set member attributes and per-connection identity mapping.

Contract under test:

* an admin sets/edits/imports a member's attributes; a member cannot set any
  (their own included) — that is what makes them safe to derive an identity
  from;
* a connection's identity mapping resolves, per signed-in member, the identity
  the client acts as (Postgres ``session_role``, Qlik ``user_id``), through
  both credential resolvers every query path uses;
* a member whose mapping resolves empty is BLOCKED (403) — never handed the
  connection's own identity;
* no signed-in user (indexing) keeps the connection's own identity.

The data-source driver is the only thing mocked (connection validation probes
a real server); clients are constructed for real but never connect.
"""

import asyncio
import io
import uuid

import pytest

from app.services.connection_service import ConnectionService


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def no_driver_probe(monkeypatch):
    """Connection create/update probes the real server; there is none here."""
    async def _ok(self, **kwargs):
        return {"success": True, "message": "ok", "reachable": True}

    monkeypatch.setattr(ConnectionService, "test_connection_params", _ok)


@pytest.fixture
def org(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    def invite(custom_attributes=None, register=True):
        email = f"mem_{uuid.uuid4().hex[:8]}@test.com"
        body = {"organization_id": org_id, "email": email, "role": "member"}
        if custom_attributes is not None:
            body["custom_attributes"] = custom_attributes
        r = test_client.post(f"/api/organizations/{org_id}/members", json=body, headers=_h(admin_token, org_id))
        assert r.status_code == 200, r.json()
        member = {"email": email, "membership_id": r.json()["id"], "invite": r.json()}
        if register:
            create_user(email=email, password="test123")
            member["token"] = login_user(email, "test123")
            member["user_id"] = whoami(member["token"])["id"]
        return member

    return {"id": org_id, "admin": admin_token, "admin_user_id": whoami(admin_token)["id"], "invite": invite}


def _members(test_client, org):
    r = test_client.get(f"/api/organizations/{org['id']}/members", headers=_h(org["admin"], org["id"]))
    assert r.status_code == 200, r.json()
    return {m["id"]: m for m in r.json()}


def _create_pg_connection(test_client, org, identity_mapping=None):
    body = {
        "name": f"pg-{uuid.uuid4().hex[:6]}",
        "type": "postgresql",
        "config": {"host": "127.0.0.1", "port": 1, "database": "acme"},
        "credentials": {"user": "bow_gateway", "password": "pw"},
        "auth_policy": "system_only",
    }
    if identity_mapping is not None:
        body["identity_mapping"] = identity_mapping
    r = test_client.post("/api/connections", json=body, headers=_h(org["admin"], org["id"]))
    assert r.status_code == 200, r.json()
    return r.json()


def _set_mapping(test_client, org, connection_id, mapping):
    return test_client.put(
        f"/api/connections/{connection_id}",
        json={"identity_mapping": mapping},
        headers=_h(org["admin"], org["id"]),
    )


def _client_for(connection_id, user_id):
    """Build the connection's client as ``user_id`` (None = system context)
    through the same resolver the agent's query paths use."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.user import User

    async def run():
        async with async_session_maker() as db:
            connection = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
            user = None
            if user_id:
                user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            return await ConnectionService().construct_client(db, connection, user)

    return asyncio.run(run())


def _ds_level_creds(connection_id, user_id):
    """The data-source service's per-connection resolver (system_only path)."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.user import User
    from app.services.data_source_service import DataSourceService

    async def run():
        async with async_session_maker() as db:
            connection = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            return await DataSourceService().resolve_credentials_for_connection(db, connection, None, user)

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Member attributes
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_admin_sets_member_attributes_on_invite_and_edit(test_client, org):
    member = org["invite"]({"qlik_user_id": " dana ", "pg_role": "sales_team"})
    listed = _members(test_client, org)[member["membership_id"]]
    assert listed["custom_attributes"] == {"qlik_user_id": "dana", "pg_role": "sales_team"}

    # An edit replaces the set; an empty value drops that attribute.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{member['membership_id']}",
        json={"custom_attributes": {"pg_role": "finance_team", "qlik_user_id": ""}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _members(test_client, org)[member["membership_id"]]["custom_attributes"] == {"pg_role": "finance_team"}

    # Editing only the note leaves attributes alone.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{member['membership_id']}",
        json={"note": "Finance analyst"},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert _members(test_client, org)[member["membership_id"]]["custom_attributes"] == {"pg_role": "finance_team"}


@pytest.mark.e2e
@pytest.mark.parametrize("bad", [{"1starts_with_digit": "x"}, {"has space": "x"}, {"k": "v" * 300}, {"k": ["a"]}])
def test_invalid_member_attributes_are_rejected(test_client, org, bad):
    email = f"bad_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(
        f"/api/organizations/{org['id']}/members",
        json={"organization_id": org["id"], "email": email, "custom_attributes": bad},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code in (400, 422), r.json()


@pytest.mark.e2e
def test_members_cannot_set_attributes_not_even_their_own(test_client, org):
    member = org["invite"]({"pg_role": "sales_team"})
    for target in (member["membership_id"],):
        r = test_client.put(
            f"/api/organizations/{org['id']}/members/{target}",
            json={"custom_attributes": {"pg_role": "finance_team"}},
            headers=_h(member["token"], org["id"]),
        )
        assert r.status_code == 403, r.json()
    assert _members(test_client, org)[member["membership_id"]]["custom_attributes"] == {"pg_role": "sales_team"}


@pytest.mark.e2e
def test_only_member_managers_can_read_attributes(test_client, org):
    member = org["invite"]({"pg_role": "sales_team"})
    other = org["invite"]({"pg_role": "finance_team"})
    r = test_client.get(f"/api/organizations/{org['id']}/members", headers=_h(member["token"], org["id"]))
    assert r.status_code == 200, r.json()
    assert all(not m.get("custom_attributes") for m in r.json())
    assert _members(test_client, org)[other["membership_id"]]["custom_attributes"] == {"pg_role": "finance_team"}


@pytest.mark.e2e
def test_import_merges_attribute_columns_without_touching_notes(test_client, org):
    existing = org["invite"]({"pg_role": "sales_team", "region": "EMEA"})
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{existing['membership_id']}",
        json={"note": "keep me"},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200
    new_email = f"imp_{uuid.uuid4().hex[:8]}@test.com"
    csv_text = (
        "Email,ATTR.pg_role,attr.qlik_user_id\n"
        f"{existing['email']},finance_team,\n"
        f"{new_email},sales_team,noa\n"
    )

    def _import(dry_run):
        return test_client.post(
            f"/api/organizations/{org['id']}/members/import?dry_run={'true' if dry_run else 'false'}",
            files={"file": ("members.csv", io.BytesIO(csv_text.encode()), "text/csv")},
            headers=_h(org["admin"], org["id"]),
        )

    preview = _import(True)
    assert preview.status_code == 200, preview.json()
    assert preview.json()["summary"]["updated"] == 1 and preview.json()["summary"]["created"] == 1
    # A dry run writes nothing.
    assert _members(test_client, org)[existing["membership_id"]]["custom_attributes"]["pg_role"] == "sales_team"

    committed = _import(False)
    assert committed.status_code == 200, committed.json()
    members = _members(test_client, org)
    updated = members[existing["membership_id"]]
    # Merged: pg_role replaced, region kept, the empty qlik_user_id cell changes nothing.
    assert updated["custom_attributes"] == {"pg_role": "finance_team", "region": "EMEA"}
    assert updated["note"] == "keep me"
    created = next(m for m in members.values() if (m.get("email") or "") == new_email)
    assert created["custom_attributes"] == {"pg_role": "sales_team", "qlik_user_id": "noa"}

    # Re-importing the same file is a no-op.
    again = _import(False)
    assert again.json()["summary"]["unchanged"] == 2


# ---------------------------------------------------------------------------
# Identity mapping
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.parametrize(
    "mapping",
    [
        {"not_a_slot": "user.email"},
        {"session_role": "user.password"},
        {"session_role": "membership.attr:bad key"},
        {"session_role": "static:{membership.secret}"},
        {"session_role": "static:"},
    ],
)
def test_invalid_identity_mappings_are_rejected(test_client, org, no_driver_probe, mapping):
    conn = _create_pg_connection(test_client, org)
    r = _set_mapping(test_client, org, conn["id"], mapping)
    assert r.status_code == 400, r.json()
    assert r.json().get("error_code") == "identity_mapping.invalid"


@pytest.mark.e2e
def test_identity_mapping_requires_every_required_slot(test_client, org, no_driver_probe):
    """Qlik needs directory AND user id — half an identity names a different account."""
    r = test_client.post(
        "/api/connections",
        json={
            "name": f"qlik-{uuid.uuid4().hex[:6]}",
            "type": "qlik_sense_onprem",
            "config": {"server_url": "https://qlik.example.com"},
            "credentials": {"client_cert": "x", "client_key": "y"},
            "identity_mapping": {"user_id": "membership.attr:qlik_user_id"},
        },
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 400, r.json()
    assert r.json().get("error_code") == "identity_mapping.invalid"


@pytest.mark.e2e
def test_mapping_is_saved_shown_to_admins_and_cleared(test_client, org, no_driver_probe):
    conn = _create_pg_connection(test_client, org)
    detail = test_client.get(f"/api/connections/{conn['id']}", headers=_h(org["admin"], org["id"])).json()
    assert detail["identity_mapping"] is None
    assert [s["param"] for s in detail["identity_slots"]] == ["session_role"]

    assert _set_mapping(test_client, org, conn["id"], {"session_role": "membership.attr:pg_role"}).status_code == 200
    detail = test_client.get(f"/api/connections/{conn['id']}", headers=_h(org["admin"], org["id"])).json()
    assert detail["identity_mapping"] == {"session_role": "membership.attr:pg_role"}
    # One materialized copy can't serve per-member identities.
    assert detail["custom_queries_supported"] is False

    assert _set_mapping(test_client, org, conn["id"], {}).status_code == 200
    detail = test_client.get(f"/api/connections/{conn['id']}", headers=_h(org["admin"], org["id"])).json()
    assert detail["identity_mapping"] is None


@pytest.mark.e2e
def test_each_member_acts_as_their_own_mapped_identity(test_client, org, no_driver_probe):
    sales = org["invite"]({"pg_role": "sales_team"})
    finance = org["invite"]({"pg_role": "finance_team"})
    conn = _create_pg_connection(test_client, org, {"session_role": "membership.attr:pg_role"})

    assert _client_for(conn["id"], sales["user_id"]).session_role == "sales_team"
    assert _client_for(conn["id"], finance["user_id"]).session_role == "finance_team"
    # The data-source resolver (system_only query path) applies it too.
    assert _ds_level_creds(conn["id"], sales["user_id"])["session_role"] == "sales_team"
    # No signed-in user (indexing) → the connection's own identity.
    assert _client_for(conn["id"], None).session_role is None

    # Re-pointing an attribute moves the member on the next query.
    r = test_client.put(
        f"/api/organizations/{org['id']}/members/{sales['membership_id']}",
        json={"custom_attributes": {"pg_role": "finance_team"}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200
    assert _client_for(conn["id"], sales["user_id"]).session_role == "finance_team"


@pytest.mark.e2e
def test_member_without_a_value_is_blocked_not_given_the_shared_identity(test_client, org, no_driver_probe):
    from app.errors import AppError

    unmapped = org["invite"]({})
    conn = _create_pg_connection(test_client, org, {"session_role": "membership.attr:pg_role"})
    with pytest.raises(AppError) as exc:
        _client_for(conn["id"], unmapped["user_id"])
    assert exc.value.status_code == 403
    assert exc.value.error_code == "identity_mapping.unresolved"
    with pytest.raises(AppError):
        _ds_level_creds(conn["id"], unmapped["user_id"])

    # A static template with an empty interpolated atom is missing too — it
    # must not collapse into a different (possibly real) role name.
    assert _set_mapping(
        test_client, org, conn["id"], {"session_role": "static:team_{membership.attr:pg_role}"}
    ).status_code == 200
    with pytest.raises(AppError):
        _client_for(conn["id"], unmapped["user_id"])


@pytest.mark.e2e
def test_preview_shows_who_acts_as_whom_and_who_is_blocked(test_client, org, no_driver_probe):
    sales = org["invite"]({"pg_role": "sales_team"})
    pending = org["invite"]({"pg_role": "finance_team"}, register=False)
    conn = _create_pg_connection(test_client, org)

    r = test_client.post(
        f"/api/connections/{conn['id']}/identity-mapping/preview",
        json={"identity_mapping": {"session_role": "static:{membership.attr:pg_role}_ro"}},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    body = r.json()
    rows = {row["membership_id"]: row for row in body["members"]}
    assert rows[sales["membership_id"]]["values"] == {"session_role": "sales_team_ro"}
    assert rows[pending["membership_id"]]["pending"] is True
    assert rows[pending["membership_id"]]["values"] == {"session_role": "finance_team_ro"}
    # The org admin has no pg_role → would be blocked.
    admin_rows = [row for row in body["members"] if row["missing"]]
    assert admin_rows and all(row["missing"] == ["session_role"] for row in admin_rows)
    assert "pg_role" in body["attribute_keys"]
    # Previewing never saves.
    detail = test_client.get(f"/api/connections/{conn['id']}", headers=_h(org["admin"], org["id"])).json()
    assert detail["identity_mapping"] is None


@pytest.mark.e2e
def test_only_connection_managers_can_change_or_preview_the_mapping(test_client, org, no_driver_probe):
    member = org["invite"]({"pg_role": "sales_team"})
    conn = _create_pg_connection(test_client, org)
    r = test_client.put(
        f"/api/connections/{conn['id']}",
        json={"identity_mapping": {"session_role": "static:postgres"}},
        headers=_h(member["token"], org["id"]),
    )
    assert r.status_code == 403
    r = test_client.post(
        f"/api/connections/{conn['id']}/identity-mapping/preview",
        json={},
        headers=_h(member["token"], org["id"]),
    )
    assert r.status_code == 403


@pytest.mark.e2e
def test_legacy_per_user_qlik_connection_fails_closed_until_mapped(test_client, org, no_driver_probe):
    """Qlik on-prem used to let members type their own Qlik account (a
    user_required connection). That is gone: such a connection refuses members
    with an actionable error — never falling back to the certificate's own
    all-seeing identity — and saving a mapping turns it into a mapped one."""
    from app.errors import AppError

    dana = org["invite"]({"qlik_user_id": "dana"})
    r = test_client.post(
        "/api/connections",
        json={
            "name": f"qlik-{uuid.uuid4().hex[:6]}",
            "type": "qlik_sense_onprem",
            "config": {"server_url": "https://qlik.example.com"},
            "credentials": {"client_cert": "cert", "client_key": "key"},
            "auth_policy": "user_required",
        },
        headers=_h(org["admin"], org["id"]),
    )
    if r.status_code == 402:
        pytest.skip("per-user auth for this connector needs an enterprise license in this environment")
    assert r.status_code == 200, r.json()
    conn = r.json()

    with pytest.raises(AppError) as exc:
        _client_for(conn["id"], dana["user_id"])
    assert exc.value.status_code == 403
    assert exc.value.error_code == "identity_mapping.unresolved"

    mapping = {"user_directory": "static:CORP", "user_id": "membership.attr:qlik_user_id"}
    assert _set_mapping(test_client, org, conn["id"], mapping).status_code == 200
    detail = test_client.get(f"/api/connections/{conn['id']}", headers=_h(org["admin"], org["id"])).json()
    assert detail["auth_policy"] == "system_only"

    # A later settings save that still carries the old policy cannot undo it.
    r = test_client.put(
        f"/api/connections/{conn['id']}",
        json={"auth_policy": "user_required"},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    assert r.json()["auth_policy"] == "system_only"

    client = _client_for(conn["id"], dana["user_id"])
    assert (client.user_directory, client.user_id) == ("CORP", "dana")
