"""Incremental discovery for the Tableau client (`prior_tables`).

Tableau's get_schemas has the same shape as Power BI's: a cheap identity-scoped
listing of published datasources, then TWO metadata calls per datasource
(VizQL read-metadata + Metadata GraphQL). On sites with hundreds of published
datasources, re-reading all of them on every interactive Reload is the slow
part — datasources already in the indexed catalog must be rebuilt from it,
with only NEW datasources paying the per-datasource metadata calls.
"""
from unittest.mock import MagicMock

from app.data_sources.clients.tableau_client import TableauClient


def _mk_client() -> TableauClient:
    c = TableauClient(server_url="https://tableau.example.com", pat_name="n", pat_token="t")
    c._auth_token = "tok"
    c._site_id = "site"
    c._http = MagicMock()
    return c


def _prior_entry(luid, cols=("Sales", "Region")):
    return {
        "columns": [{"name": c, "dtype": "REAL"} for c in cols],
        "pks": [], "fks": [],
        "metadata_json": {"tableau": {"datasourceLuid": luid, "name": "old"}},
    }


def _wire(c, datasources, live_fields):
    """Mock the site: listing returns `datasources`; per-datasource metadata
    returns live_fields (dict luid -> fields) and records which were fetched."""
    c.list_published_datasources = MagicMock(return_value=datasources)
    calls = []

    def combined(luid):
        calls.append(luid)
        return None, live_fields.get(luid, [])

    c._combined_fields_for_datasource = MagicMock(side_effect=combined)
    return calls


class TestTableauIncrementalDiscovery:
    def test_known_reused_new_introspected(self):
        c = _mk_client()
        calls = _wire(
            c,
            [
                {"id": "luid-known", "name": "Sales DS", "project_id": "p", "project_name": "Analytics"},
                {"id": "luid-new", "name": "Fresh DS", "project_id": "p", "project_name": "Analytics"},
            ],
            {"luid-new": [{"fieldCaption": "X", "dataType": "STRING"}]},
        )
        out = c.get_schemas(prior_tables={"Analytics/Sales DS": _prior_entry("luid-known")})
        assert calls == ["luid-new"]
        assert {t.name for t in out} == {"Analytics/Sales DS", "Analytics/Fresh DS"}
        reused = next(t for t in out if t.name == "Analytics/Sales DS")
        assert [col.name for col in reused.columns] == ["Sales", "Region"]
        assert reused.metadata_json["tableau"]["datasourceLuid"] == "luid-known"

    def test_all_known_makes_zero_metadata_calls(self):
        c = _mk_client()
        calls = _wire(c, [{"id": "l1", "name": "DS", "project_id": "p", "project_name": "P"}], {})
        out = c.get_schemas(prior_tables={"P/DS": _prior_entry("l1")})
        assert calls == []
        assert [t.name for t in out] == ["P/DS"]

    def test_renamed_or_moved_datasource_reuses_columns_under_new_path(self):
        c = _mk_client()
        _wire(c, [{"id": "l1", "name": "DS v2", "project_id": "p2", "project_name": "NewProj"}], {})
        out = c.get_schemas(prior_tables={"OldProj/DS": _prior_entry("l1")})
        assert [t.name for t in out] == ["NewProj/DS v2"]
        assert out[0].metadata_json["tableau"]["projectName"] == "NewProj"

    def test_vanished_datasource_dropped(self):
        c = _mk_client()
        _wire(c, [{"id": "l1", "name": "DS", "project_id": "p", "project_name": "P"}], {})
        out = c.get_schemas(prior_tables={
            "P/DS": _prior_entry("l1"),
            "P/Gone": _prior_entry("l-gone"),
        })
        assert [t.name for t in out] == ["P/DS"]

    def test_prior_entry_without_columns_is_reintrospected(self):
        c = _mk_client()
        calls = _wire(
            c,
            [{"id": "l1", "name": "DS", "project_id": "p", "project_name": "P"}],
            {"l1": [{"fieldCaption": "Sales", "dataType": "REAL"}]},
        )
        out = c.get_schemas(prior_tables={"P/DS": _prior_entry("l1", cols=())})
        assert calls == ["l1"]
        assert [col.name for col in out[0].columns] == ["Sales"]

    def test_no_prior_tables_introspects_everything(self):
        c = _mk_client()
        calls = _wire(
            c,
            [{"id": "l1", "name": "DS", "project_id": "p", "project_name": "P"}],
            {"l1": [{"fieldCaption": "Sales", "dataType": "REAL"}]},
        )
        c.get_schemas()
        assert calls == ["l1"]

    def test_aget_schemas_forwards_prior_tables(self):
        import asyncio
        c = _mk_client()
        calls = _wire(c, [{"id": "l1", "name": "DS", "project_id": "p", "project_name": "P"}], {})
        out = asyncio.run(c.aget_schemas(prior_tables={"P/DS": _prior_entry("l1")}))
        assert calls == []
        assert [t.name for t in out] == ["P/DS"]
