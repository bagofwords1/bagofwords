"""
Tables selector "All tables / My tables" toggle for delegated connections.

Contract:
  * The toggle lives in the agent-manager table picker. Per connection, it
    opens a user_required connection's whole org catalog only when that
    connection has org credentials AND the caller may manage the connection.
    On an agent with several delegated connections, exactly the managed ones
    open; the rest stay in the caller's own view, reported with the reason.
  * No toggle for an agent manager who manages none of those connections, for
    anyone who cannot manage the agent (`catalog_view=all` changes nothing for
    them), or when the default view already shows every org catalog.
  * Reload with `catalog_view=all` refreshes the opened connections with the
    org's credentials (authoritative: tables deleted at the source disappear);
    every other connection keeps the caller's own token.
"""
import uuid
import asyncio
from datetime import datetime, timezone

import pytest

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.services.data_source_service import DataSourceService, _WARM_ATTEMPTS


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _meta(name):
    return {"powerbi": {"datasetId": f"ds-{name}", "tableName": name}}


def _payload(name):
    return {
        "name": name, "columns": [{"name": "id", "dtype": "int"}],
        "pks": [], "fks": [], "metadata_json": _meta(name),
    }


# What each identity sees at each source. Names are deliberately arbitrary.
# "a" is the connection the manager manages, "b" one they do not.
ORG_CATALOG = {"a": ["Orders", "Ledger", "Payroll"], "b": ["Invoices", "Budget"]}
PERSONAL_CATALOG = {"a": ["Orders"], "b": ["Invoices"]}


class _FakePowerBIClient:
    """Driver boundary. The service principal (client_id) sees the org
    catalog; a delegated token (access_token) sees the personal slice."""
    org_catalog = {}
    crawls: list = []

    def __init__(self, **params):
        if params.get("access_token"):
            self.identity, self.key = "user", params["access_token"]
        else:
            self.identity, self.key = "system", params.get("client_id")

    async def aget_schemas(self, progress_callback=None, prior_catalog=None, **kw):
        _FakePowerBIClient.crawls.append((self.key, self.identity))
        source = self.org_catalog if self.identity == "system" else PERSONAL_CATALOG
        return [_payload(n) for n in source[self.key]]


class _FakeWarehouseClient:
    def __init__(self, **params):
        pass

    async def aget_schemas(self, progress_callback=None, prior_catalog=None, **kw):
        return [{"name": "public.customers", "columns": [{"name": "id", "dtype": "int"}],
                 "pks": [], "fks": [], "metadata_json": {"schema": "public"}}]


_FAKES = {"powerbi": _FakePowerBIClient, "postgresql": _FakeWarehouseClient}


@pytest.fixture(autouse=True)
def _fake_source(monkeypatch):
    _WARM_ATTEMPTS.clear()
    _FakePowerBIClient.org_catalog = {k: list(v) for k, v in ORG_CATALOG.items()}
    _FakePowerBIClient.crawls = []

    import app.services.connection_service as connection_service_module
    monkeypatch.setattr(connection_service_module, "resolve_client_class", lambda t: _FAKES[t])

    async def _user_client(self, db, data_source, connection=None, user=None, **kw):
        key = (connection.name or "").split(" ")[1]
        return _FakePowerBIClient(access_token=key)
    monkeypatch.setattr(DataSourceService, "_construct_user_catalog_client", _user_client)
    yield
    _WARM_ATTEMPTS.clear()


async def _seed(org_id, token_holder_ids):
    """Seeded directly: a user_required Power BI connection needs an Enterprise
    license to create, and a delegated OAuth token cannot be minted through the
    API without a live identity provider."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        warehouse = Connection(
            organization_id=org_id, name=f"Warehouse {suffix}", type="postgresql",
            config={"host": "localhost", "port": 5432, "database": "demo"},
            auth_policy="system_only",
        )
        warehouse.encrypt_credentials({"user": "u", "password": "p"})
        delegated = {}
        for key in ("a", "b"):
            conn = Connection(
                organization_id=org_id, name=f"PBI {key} {suffix}", type="powerbi",
                config={}, auth_policy="user_required", allowed_user_auth_modes=["oauth"],
            )
            conn.encrypt_credentials({"tenant_id": "t", "client_id": key, "client_secret": "s"})
            delegated[key] = conn
        # Delegated-only: no service principal stored, so no org catalog.
        without_org = Connection(
            organization_id=org_id, name=f"PBI personal {suffix}", type="powerbi",
            config={"auth_type": "oauth"}, auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conns = [warehouse, *delegated.values(), without_org]
        db.add_all(conns)
        await db.flush()

        ds = DataSource(name=f"Agent {suffix}", organization_id=org_id, is_active=True, is_public=True)
        db.add(ds)
        await db.flush()
        for conn in conns:
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))

        # Org catalogs as the service principals indexed them. "Ledger" is
        # selected for the agent although the personal token cannot see it.
        catalog = [(warehouse, "public.customers", {"schema": "public"}, True)]
        active = {"Orders", "Ledger", "Invoices"}
        for key, names in ORG_CATALOG.items():
            catalog += [(delegated[key], n, _meta(n), n in active) for n in names]
        for conn, name, meta, is_active in catalog:
            ct = ConnectionTable(name=name, connection_id=conn.id, columns=[{"name": "id", "dtype": "int"}],
                                 pks=[], fks=[], no_rows=0, metadata_json=meta)
            db.add(ct)
            await db.flush()
            db.add(DataSourceTable(
                name=name, datasource_id=ds.id, connection_table_id=ct.id, is_active=is_active,
                metadata_json=meta, columns=ct.columns, pks=[], fks=[], no_rows=0,
            ))

        for user_id in token_holder_ids:
            for key, conn in (*delegated.items(), ("personal", without_org)):
                cred = UserConnectionCredentials(
                    connection_id=str(conn.id), user_id=str(user_id), organization_id=str(org_id),
                    auth_mode="oauth", is_active=True, is_primary=True,
                    last_used_at=datetime.now(timezone.utc),
                )
                cred.encrypt_credentials({"access_token": key})
                db.add(cred)
        await db.commit()
        return {
            "ds_id": str(ds.id), "conn_a": str(delegated["a"].id),
            "conn_b": str(delegated["b"].id), "without_org": str(without_org.id),
        }


@pytest.fixture
def cast(bootstrap_admin, invite_user_to_org, grant_resource):
    admin = bootstrap_admin("tsel_admin")
    org_id, admin_token = admin["org_id"], admin["token"]
    manager = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    agent_only = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    member = invite_user_to_org(org_id=org_id, admin_token=admin_token)
    ids = asyncio.run(_seed(org_id, [u["user_id"] for u in (manager, agent_only, member)]))

    def _grant(resource_type, resource_id, user, perms):
        resp = grant_resource(
            resource_type=resource_type, resource_id=resource_id, principal_type="user",
            principal_id=user["user_id"], permissions=perms, user_token=admin_token, org_id=org_id,
        )
        assert resp.status_code == 200, resp.text

    _grant("data_source", ids["ds_id"], manager, ["manage"])
    _grant("connection", ids["conn_a"], manager, ["manage_connection"])
    _grant("data_source", ids["ds_id"], agent_only, ["manage"])
    return {"admin": admin, "manager": manager, "agent_only": agent_only, "member": member, **ids}


def _tables(test_client, cast, who, catalog_view=None):
    params = {"page": 1, "page_size": 100}
    if catalog_view:
        params["catalog_view"] = catalog_view
    resp = test_client.get(
        f"/api/data_sources/{cast['ds_id']}/full_schema", params=params,
        headers=_headers(cast[who]["token"], cast["admin"]["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {t["name"] for t in body["tables"]}, body.get("catalog_view")


def _reload(test_client, cast, who, catalog_view=None):
    params = {"catalog_view": catalog_view} if catalog_view else {}
    resp = test_client.get(
        f"/api/data_sources/{cast['ds_id']}/refresh_schema", params=params,
        headers=_headers(cast[who]["token"], cast["admin"]["org_id"]),
    )
    assert resp.status_code == 200, resp.text


OWN_VIEW = {"public.customers", "Orders", "Invoices"}


@pytest.mark.e2e
def test_manager_default_view_reports_which_connections_the_toggle_opens(test_client, cast):
    names, view = _tables(test_client, cast, "manager")
    assert names == OWN_VIEW
    assert view is not None and view["all_tables"] is False
    assert [c["id"] for c in view["applies_to"]] == [cast["conn_a"]]
    reasons = {c["id"]: c["reason"] for c in view["not_applicable"]}
    assert reasons == {cast["conn_b"]: "no_permission", cast["without_org"]: "no_org_credentials"}
    # "Ledger" is selected for the agent but outside the manager's own access.
    assert view["selected_inaccessible_count"] == 1


@pytest.mark.e2e
def test_all_tables_opens_only_the_connections_the_caller_manages(test_client, cast):
    names, view = _tables(test_client, cast, "manager", catalog_view="all")
    assert view["all_tables"] is True
    assert names == OWN_VIEW | set(ORG_CATALOG["a"])
    assert "Budget" not in names, "an unmanaged connection must stay in the caller's own view"


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["agent_only", "member"])
def test_no_toggle_without_a_managed_connection(test_client, cast, who):
    default_names, default_view = _tables(test_client, cast, who)
    all_names, all_view = _tables(test_client, cast, who, catalog_view="all")
    assert default_view is None and all_view is None
    assert all_names == default_names
    assert not ({"Ledger", "Payroll", "Budget"} & all_names)


@pytest.mark.e2e
def test_no_toggle_when_default_view_already_shows_org_catalog(test_client, cast):
    # Org admin without a personal token already sees the org catalogs.
    names, view = _tables(test_client, cast, "admin")
    assert view is None
    assert {n for names_ in ORG_CATALOG.values() for n in names_} <= names


@pytest.mark.e2e
def test_all_tables_reload_uses_org_credentials_only_on_managed_connections(test_client, cast):
    _FakePowerBIClient.org_catalog["a"] = ["Orders", "Ledger"]  # Payroll dropped at the source

    _reload(test_client, cast, "manager", catalog_view="all")

    assert ("a", "system") in _FakePowerBIClient.crawls
    assert ("b", "system") not in _FakePowerBIClient.crawls
    names, _ = _tables(test_client, cast, "manager", catalog_view="all")
    assert names == OWN_VIEW | {"Ledger"}


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["agent_only", "member"])
def test_reload_without_a_managed_connection_ignores_all(test_client, cast, who):
    _FakePowerBIClient.org_catalog["a"] = ["Orders", "Ledger"]

    _reload(test_client, cast, who, catalog_view="all")

    assert not [c for c in _FakePowerBIClient.crawls if c[1] == "system"]
    # A personal crawl may only add to the shared catalog, never prune it.
    names, _ = _tables(test_client, cast, "manager", catalog_view="all")
    assert "Payroll" in names
