"""SAP BusinessObjects extraction source: a universe query paged to its count.

The HTTP boundary is a fake Semantic Layer endpoint that implements the query
lifecycle the connector uses (create, run, $metadata, $count, $skip/$top
pages, delete). Above it the real client, source and DuckDB artifact run.

The properties that matter for copying a result rather than answering an
agent: every row arrives exactly once however the server sizes its pages, a
result that ends before its reported count is refused rather than stored, the
count is what the budget check sees, the request limit is the organization's,
and the temporary query is always deleted at the source.
"""
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.data_sources.clients.businessobjects_client import BusinessObjectsClient
from app.data_sources.fast import artifacts, extractor, incremental
from app.data_sources.fast import businessobjects_source as bos
from app.data_sources.fast import limits as fast_limits
from app.data_sources.fast.sources import source_for

HOST = "https://bi.example.com:6405"

# object id -> (name in the universe, OData property, Edm type)
OBJECTS = {
    "o1": ("Order id", "Order_id", "Edm.Int64"),
    "o2": ("Country", "Country", "Edm.String"),
    "o3": ("Sales revenue", "Sales_revenue", "Edm.Decimal"),
    "o4": ("Order date", "Order_date", "Edm.DateTime"),
}


def _resp(status=200, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.headers = {"X-SAP-LogonToken": "token"}
    r.text = text or (json.dumps(payload) if payload is not None else "")
    return r


class FakeBO:
    """One universe ("Sales", id 7) and the result of whatever is queried."""

    def __init__(self, rows, server_page=None, ends_at=None, legacy=False):
        self.verify = True
        self.rows = rows                # list of dicts keyed by object id
        self.server_page = server_page  # the server's own cap on a page, if any
        self.ends_at = ends_at          # rows after this never arrive
        self.legacy = legacy            # OData v2 envelope and /Date()/ values
        self.pages = []                 # (skip, top) of every page request
        self.created = self.deleted = 0
        self.selected = []

    def post(self, url, **kwargs):
        if url.endswith("/logon/long"):
            return _resp(200, {})
        if url.endswith("/sl/v1/queries"):
            root = ET.fromstring(kwargs["data"])
            assert root.attrib["dataSourceId"] == "7"
            self.selected = [e.attrib["id"] for e in root.iter() if e.tag.endswith("resultObject")]
            self.created += 1
            return _resp(200, text="<success><id>q-1</id></success>")
        return _resp(404, text="not found")

    def get(self, url, **kwargs):
        if url.endswith("/data.svc/$metadata"):
            props = "".join(f'<Property Name="{OBJECTS[i][1]}" Type="{OBJECTS[i][2]}" />' for i in self.selected)
            return _resp(200, text=(
                '<Schema><EntityType Name="Flow"><Key><PropertyRef Name="Id" /></Key>'
                f'<Property Name="Id" Type="Edm.Int32" />{props}</EntityType>'
                '<EntityContainer><EntitySet Name="Flows0" EntityType="Flow" /></EntityContainer></Schema>'))
        if url.endswith("/data.svc"):
            return _resp(200, text='<service><collection href="Flows0" /></service>')
        if url.endswith("/data.svc/Flows0/$count"):
            return _resp(200, text=str(len(self.rows)))
        if url.endswith("/data.svc/Flows0"):
            skip, top = int(kwargs["params"]["$skip"]), int(kwargs["params"]["$top"])
            self.pages.append((skip, top))
            if skip >= len(self.rows):
                return _resp(400, text="Only available rows may be requested")
            available = self.rows[: self.ends_at] if self.ends_at is not None else self.rows
            size = min(top, self.server_page or top)
            page = [{"Id": skip + n, **{OBJECTS[i][1]: self._wire(i, r[i]) for i in self.selected}}
                    for n, r in enumerate(available[skip:skip + size])]
            return _resp(200, {"d": {"results": page}} if self.legacy else {"value": page})
        if "/sl/v1/universes/7" in url:
            return _resp(200, {"universe": {"id": "7", "name": "Sales", "outline": {"folder": [{
                "name": "All", "item": [
                    {"id": i, "name": name, "dataType": "String",
                     "type": "measure" if i == "o3" else "dimension"}
                    for i, (name, _p, _t) in OBJECTS.items()]}]}}})
        if "/sl/v1/universes" in url:
            return _resp(200, {"universes": {"universe": [{"id": "7", "name": "Sales", "type": "unx"}]}})
        return _resp(404, text="not found")

    def delete(self, url, **kwargs):
        self.deleted += 1
        return _resp(200, text="<success />")

    def _wire(self, object_id, value):
        """How OData JSON carries a value of this column's type."""
        edm = OBJECTS[object_id][2]
        if value is None:
            return None
        if edm in ("Edm.Int64", "Edm.Decimal"):
            return str(value)
        if edm == "Edm.DateTime":
            if self.legacy:
                return f"/Date({int((value - datetime(1970, 1, 1)).total_seconds() * 1000)})/"
            return value.strftime("%Y-%m-%dT%H:%M:%S")
        return value


def _orders(n):
    return [{"o1": i + 1, "o2": ["FR", "US", None][i % 3], "o3": round(9.5 * (i + 1), 2),
             "o4": datetime(2026, 1, 1 + i % 28, 8, 30)} for i in range(n)]


def _client(server):
    client = BusinessObjectsClient(host=HOST, username="svc", password="pw")
    client._http = server
    return client


def _query(*names, universe="Sales"):
    return json.dumps({"universe": universe, "objects": list(names or ("Order id", "Country"))})


ALL = _query("Order id", "Country", "Sales revenue", "Order date")


def _collect(client, query, batch_rows=1000):
    batches = list(source_for(client).stream_batches(query, batch_rows))
    names = batches[0].column_names
    return names, [r for b in batches for r in zip(*[b.column(c).to_pylist() for c in names])]


def _page(monkeypatch, rows):
    monkeypatch.setattr(bos, "PAGE_ROWS", rows)


# --------------------------------------------------------------------------
# The definition
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "Country, Sales revenue",
    "[1, 2]",
    '{"universe": "Sales"}',
    '{"objects": ["Country"]}',
    '{"universe": "Sales", "objects": []}',
    '{"universe": "Sales", "objects": ["Country", "Country"]}',
])
def test_a_definition_that_is_not_a_universe_and_its_objects_is_refused(bad):
    server = FakeBO(_orders(3))
    with pytest.raises(ValueError):
        _collect(_client(server), bad)
    assert server.created == 0, "nothing may be created at the source for a query that cannot run"


@pytest.mark.parametrize("query", [_query("Country", universe="Finance"), _query("Margin")])
def test_an_unknown_universe_or_object_is_named_in_the_error(query):
    server = FakeBO(_orders(3))
    with pytest.raises(ValueError, match="Finance|Margin"):
        _collect(_client(server), query)
    assert server.created == 0


def test_objects_may_be_given_as_one_comma_separated_string():
    server = FakeBO(_orders(4))
    names, rows = _collect(_client(server), json.dumps({"universe": "Sales", "objects": "Order id, Country"}))
    assert names == ["Order id", "Country"] and len(rows) == 4


# --------------------------------------------------------------------------
# Estimate and preview
# --------------------------------------------------------------------------

def test_estimate_is_the_exact_count_the_budget_check_sees():
    server = FakeBO(_orders(83))
    est = extractor.estimate(_client(server), ALL)
    assert est.supported and est.rows == 83
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est, max_rows=82)
    extractor.check_budget(est, max_rows=83)
    assert not server.pages, "an estimate reads no rows"
    assert server.created == server.deleted == 1


def test_a_failing_estimate_downgrades_instead_of_raising():
    est = extractor.estimate(_client(FakeBO(_orders(3))), _query("Margin"))
    assert not est.supported and "Margin" in est.note


def test_preview_reads_one_bounded_page_and_cleans_up():
    server = FakeBO(_orders(500))
    cols, rows = extractor.preview(_client(server), ALL, 6)
    assert [c["name"] for c in cols] == ["Order id", "Country", "Sales revenue", "Order date"]
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert server.pages == [(0, 6)] and server.deleted == 1


# --------------------------------------------------------------------------
# Paging to the count
# --------------------------------------------------------------------------

@pytest.mark.parametrize("server_page", [None, 7, 1], ids=["as-asked", "server-caps-pages", "one-row-pages"])
def test_every_row_arrives_once_however_the_server_sizes_its_pages(monkeypatch, server_page):
    """A server that returns fewer rows than asked must cost requests, never
    rows: the next page starts from what actually arrived."""
    _page(monkeypatch, 25)
    server = FakeBO(_orders(130), server_page=server_page)
    _names, rows = _collect(_client(server), ALL)
    assert [r[0] for r in rows] == list(range(1, 131))
    assert all(skip + top <= 130 for skip, top in server.pages), "a page was requested past the count"
    assert server.deleted == 1


@pytest.mark.parametrize("legacy", [False, True], ids=["odata-v4", "odata-v2"])
def test_values_land_in_their_declared_types(legacy):
    """OData JSON carries 64-bit integers and decimals as strings and dates
    as text; stored that way, no numeric or date query on the table works."""
    server = FakeBO(_orders(6), legacy=legacy)
    names, rows = _collect(_client(server), ALL)
    assert names == ["Order id", "Country", "Sales revenue", "Order date"]
    assert rows[1] == (2, "US", 19.0, datetime(2026, 1, 2, 8, 30))
    assert rows[2][1] is None


def test_a_result_that_ends_before_its_count_is_refused(monkeypatch):
    _page(monkeypatch, 25)
    server = FakeBO(_orders(130), ends_at=90)
    with pytest.raises(RuntimeError, match="before its reported row count"):
        _collect(_client(server), ALL)
    assert server.deleted == 1


def test_a_short_result_leaves_no_artifact_and_a_whole_one_is_queryable(monkeypatch, tmp_path):
    import duckdb

    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    _page(monkeypatch, 25)
    with pytest.raises(RuntimeError):
        extractor.extract_to_artifact(_client(FakeBO(_orders(130), ends_at=90)), ALL, "orders", "conn-1")
    assert not list(tmp_path.rglob("*.db"))

    res = extractor.extract_to_artifact(_client(FakeBO(_orders(130))), ALL, "orders", "conn-1")
    assert res.row_count == 130
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{res.artifact_path}' AS a (ENCRYPTION_KEY '{res.artifact_key}', READ_ONLY)")
    total, newest = con.execute('SELECT sum("Sales revenue"), max("Order date") FROM a.main.orders').fetchone()
    con.close()
    assert total == pytest.approx(9.5 * 130 * 131 / 2)
    assert newest == datetime(2026, 1, 28, 8, 30)


def test_an_empty_result_is_an_empty_table_with_its_columns():
    names, rows = _collect(_client(FakeBO([])), ALL)
    assert names == ["Order id", "Country", "Sales revenue", "Order date"] and rows == []


def test_stopping_partway_deletes_the_query_at_the_source(monkeypatch):
    """A cancelled refresh closes the batch stream; the temporary query must
    not be left on the server."""
    _page(monkeypatch, 10)
    server = FakeBO(_orders(100))
    batches = source_for(_client(server)).stream_batches(ALL, 10)
    next(batches)
    batches.close()
    assert server.deleted == 1 and len(server.pages) == 1


def test_the_request_limit_is_the_organizations(monkeypatch):
    _page(monkeypatch, 10)
    server = FakeBO(_orders(400))
    client = _client(server)
    client.extraction_limits = fast_limits.FastLimits(max_requests=16)
    est = extractor.estimate(client, ALL)
    assert est.source_max_rows == 160, "said when saving, from the count"
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est)
    with pytest.raises(RuntimeError, match="16 requests"):
        _collect(client, ALL)
    assert len(server.pages) == 16

    client.extraction_limits = fast_limits.FastLimits(max_requests=40)
    extractor.check_budget(extractor.estimate(client, ALL))
    assert len(_collect(client, ALL)[1]) == 400


def test_a_refresh_window_is_taken_from_the_result_not_written_into_the_query():
    """The universe query has no filter, so an incremental table runs whole."""
    client = _client(FakeBO(_orders(3)))
    assert not incremental.filters_at_source(client, ALL)
    window = incremental.Window(lo=datetime(2026, 1, 5), hi=None)
    assert incremental.render(client, ALL, "Order date", incremental.KIND_TIMESTAMP, window) == ALL
