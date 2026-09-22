"""Live refresh must detect drift and preserve every model identity."""

import json
from unittest.mock import Mock

import pytest

from app.data_sources.clients.powerbi_client import PowerBIClient


class Tenant:
    def __init__(self, models):
        self.models = models
        self.hidden = False
        self.rich = False
        self.unreadable = False

    def response(self, method, url, **kwargs):
        payload = {}
        status = 200
        if url.endswith("/groups"):
            payload = {
                "value": []
                if self.hidden
                else [{"id": ws, "name": "Workspace"} for ws in dict.fromkeys(m[0] for m in self.models)]
            }
        elif url.endswith("/datasets"):
            ws = url.split("/groups/")[1].split("/")[0]
            payload = {"value": [{"id": ds, "name": "Analytics"} for w, ds, cols in self.models if w == ws]}
        elif url.endswith("/reports"):
            payload = {"value": []}
        elif "executeQueries" in url:
            ds = url.split("/datasets/")[1].split("/")[0]
            query = kwargs.get("json", {}).get("queries", [{}])[0].get("query", "")
            if self.unreadable:
                status = 403
            elif self.rich and "INFO.VIEW.COLUMNS" in query:
                payload = {
                    "results": [
                        {
                            "tables": [
                                {
                                    "rows": [
                                        {"[Kind]": "C", "[Tbl]": "Orders", "[Name]": "customer_id", "[Info1]": "Int64"},
                                        {"[Kind]": "C", "[Tbl]": "Customers", "[Name]": "id", "[Info1]": "Int64"},
                                        {
                                            "[Kind]": "R",
                                            "[Tbl]": "Orders",
                                            "[Name]": "customer_id",
                                            "[Info1]": "Customers",
                                            "[Info2]": "id",
                                            "[Flag]": True,
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                }
            elif "COLUMNSTATISTICS" in query:
                cols = next(cols for w, d, cols in self.models if d == ds)
                payload = {
                    "results": [
                        {"tables": [{"rows": [{"[Table Name]": "Orders", "[Column Name]": col} for col in cols]}]}
                    ]
                }
            elif "ROW(" in query and any(d == ds for w, d, cols in self.models):
                payload = {"results": [{"tables": [{"rows": [{"[t]": ds}]}]}]}
            else:
                status = 400
        else:
            status = 403
        r = Mock(status_code=status, headers={}, text=json.dumps(payload))
        r.json.return_value = payload
        return r

    def get(self, url, **kw):
        return self.response("GET", url, **kw)

    def post(self, url, **kw):
        return self.response("POST", url, **kw)

    def request(self, method, url, **kw):
        return self.response(method, url, **kw)


def client(tenant, **kwargs):
    c = PowerBIClient(access_token="test-token", **kwargs)
    c._http = tenant
    return c


def prior(tables):
    return {
        t.name: {
            "columns": [c.model_dump() for c in t.columns],
            "fks": [fk.model_dump() for fk in t.fks or []],
            "metadata_json": t.metadata_json,
        }
        for t in tables
    }


def test_explicit_refresh_reads_changed_columns_with_prior_catalog():
    tenant = Tenant([("ws-a", "ds-a", ["old_name", "removed"])])
    saved = prior(client(tenant).get_schemas())
    tenant.models = [("ws-a", "ds-a", ["new_name", "added"])]
    refreshed = client(tenant).get_schemas(force_refresh=True, prior_tables=saved)
    assert {c.name for t in refreshed for c in t.columns} == {"new_name", "added"}


@pytest.mark.parametrize("same_workspace", [True, False])
@pytest.mark.parametrize("reverse", [True, False])
def test_same_named_models_have_distinct_queryable_table_names(same_workspace, reverse):
    models = [("ws-a", "ds-a", ["a"]), ("ws-a" if same_workspace else "ws-b", "ds-b", ["b"])]
    if reverse:
        models.reverse()
    tables = client(Tenant(models)).get_schemas()
    assert len(tables) == 2
    assert len({t.name for t in tables}) == 2
    assert {t.metadata_json["powerbi"]["datasetId"] for t in tables} == {"ds-a", "ds-b"}
    assert {tuple(c.name for c in t.columns) for t in tables} == {("a",), ("b",)}


def test_full_refresh_keeps_item_shared_candidates_without_reusing_columns():
    tenant = Tenant([("ws-a", "ds-a", ["old"])])
    saved = prior(client(tenant).get_schemas())
    tenant.models = [("ws-a", "ds-a", ["new"])]
    tenant.hidden = True
    refreshed = client(tenant).get_schemas(force_refresh=True, prior_tables=saved)
    assert {c.name for t in refreshed for c in t.columns} == {"new"}


def test_workspace_filter_does_not_probe_excluded_prior_models():
    tenant = Tenant([("ws-a", "ds-a", ["old"])])
    saved = prior(client(tenant).get_schemas())
    tenant.hidden = True
    c = client(tenant, workspaces="other-workspace")
    assert c.get_schemas(force_refresh=True, prior_tables=saved) == []


def test_collision_names_are_independent_of_discovery_order():
    models = [("ws-a", "ds-a", ["a"]), ("ws-b", "ds-b", ["b"])]

    def names(rows):
        return {t.metadata_json["powerbi"]["datasetId"]: t.name for t in rows}

    assert names(client(Tenant(models)).get_schemas()) == names(client(Tenant(list(reversed(models)))).get_schemas())


def test_relationships_stay_with_their_model_across_collision_and_subset_refresh():
    tenant = Tenant([("ws-a", "ds-a", ["a"]), ("ws-b", "ds-b", ["b"])])
    tenant.rich = True
    tables = client(tenant).get_schemas()
    assert any(t.fks for t in tables)

    def check(rows):
        by_name = {t.name: t for t in rows}
        for t in rows:
            for fk in t.fks or []:
                assert fk.references_name in by_name
                assert (
                    by_name[fk.references_name].metadata_json["powerbi"]["datasetId"]
                    == t.metadata_json["powerbi"]["datasetId"]
                )

    check(tables)
    saved = prior([t for t in tables if t.metadata_json["powerbi"]["datasetId"] == "ds-a"])
    tenant.models = [("ws-a", "ds-a", ["a"])]
    check(client(tenant).get_schemas(prior_tables=saved))


def test_colliding_tables_route_queries_by_exact_name_and_reject_ambiguous_aliases():
    c = client(Tenant([("ws-a", "ds-a", ["a"]), ("ws-b", "ds-b", ["b"])]))
    tables = c.get_schemas()
    with pytest.raises(ValueError):
        c.get_schema("Orders")
    c.attach_table_metadata([{"name": t.name, "metadata_json": t.metadata_json} for t in tables])
    for table in tables:
        result = c.execute_query('EVALUATE ROW("t", 1)', table_name=table.name)
        assert result.iloc[0, 0] == table.metadata_json["powerbi"]["datasetId"]
    for alias in ["Orders", "Analytics"]:
        with pytest.raises(ValueError):
            c.execute_query('EVALUATE ROW("t", 1)', table_name=alias)
