"""Analysis Services extraction source: DAX over XMLA, read in bounded pages.

The HTTP boundary is a fake XMLA endpoint that answers the DAX the source
sends and renders real rowset envelopes (an XSD schema, values as text, nulls
as absent elements). Everything above it runs real: the client's SOAP
handling, typed rowset parsing, catalog resolution, the count check, paging
and the DuckDB artifact.

What this endpoint has that Power BI's does not, and so what is tested here:
a response is the whole result with no cap, so the source must bound each
request itself (and prove it did); values are untyped text; a null is a
missing element; and MDX, which cannot be paged, must keep working as before.
"""
import re
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from xml.sax.saxutils import escape, unescape

import pytest

from app.data_sources.clients.analysis_services_client import AnalysisServicesClient
from app.data_sources.fast import analysis_services_source as ass
from app.data_sources.fast import artifacts, extractor, incremental
from app.data_sources.fast import limits as fast_limits
from app.data_sources.fast.sources import source_for
from tests.unit.test_powerbi_source import FakeDax

BASE = datetime(2026, 1, 1, 12, 0, 0)


# --------------------------------------------------------------------------
# A fake XMLA endpoint
# --------------------------------------------------------------------------

def _tag(name):
    return "".join(c if c.isalnum() else f"_x{ord(c):04X}_" for c in name)


def _xsd(value):
    if isinstance(value, bool):
        return "xsd:boolean"
    if isinstance(value, int):
        return "xsd:long"
    if isinstance(value, float):
        return "xsd:double"
    if isinstance(value, str) and re.match(r"^\d{4}-\d\d-\d\dT", value):
        return "xsd:dateTime"
    return "xsd:string"


def _rowset(rows, columns, typed=True):
    """An XMLA tabular rowset: schema first, then rows with nulls left out."""
    types = {c: next((_xsd(r[c]) for r in rows if r.get(c) is not None), "xsd:string") for c in columns}
    schema = "".join(
        f'<xsd:element sql:field="{escape(c)}" name="{_tag(c)}" '
        + (f'type="{types[c]}" ' if typed else "") + 'minOccurs="0"/>'
        for c in columns
    )
    body = "".join(
        "<row>" + "".join(
            f"<{_tag(c)}" + ("" if typed else f' xsi:type="{types[c]}"') + f">{escape(str(r[c]))}</{_tag(c)}>"
            for c in columns if r.get(c) is not None
        ) + "</row>"
        for r in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        '<ExecuteResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return>'
        '<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:sql="urn:schemas-microsoft-com:xml-sql">'
        '<xsd:schema targetNamespace="urn:schemas-microsoft-com:xml-analysis:rowset">'
        f'<xsd:complexType name="row"><xsd:sequence>{schema}</xsd:sequence></xsd:complexType>'
        "</xsd:schema>"
        f"{body}</root></return></ExecuteResponse></soap:Body></soap:Envelope>"
    ).encode()


class FakeXmla:
    """Routes each Execute to the model of the catalog it names."""

    def __init__(self, models, typed=True):
        self.models = models           # catalog -> FakeDax
        self.typed = typed
        self.statements = []           # (catalog, statement)
        self.largest_response = 0      # rows in the biggest answer to a request for rows
        self.mdx_rows = [{"[Measures].[Revenue]": "42"}]

    def post(self, url, data=None, headers=None, timeout=None, verify=None):
        text = data.decode()
        statement = unescape(re.search(r"<Statement>(.*)</Statement>", text, re.S).group(1))
        catalog = (re.search(r"<Catalog>(.*?)</Catalog>", text) or [None, None])[1]
        self.statements.append((catalog, statement))
        model = self.models[catalog]
        if "EVALUATE" not in statement:
            rows, columns = self.mdx_rows, list(self.mdx_rows[0])
        else:
            rows = model.respond(statement)
            # The fixed-size sample (TOPN(200, <query>)) is not a page of data.
            if not re.match(r"EVALUATE (ROW\(|TOPN\(200, (?!FILTER))", statement):
                self.largest_response = max(self.largest_response, len(rows))
            columns = list(rows[0].keys()) if rows else []
            if rows and set(columns) <= set(model.columns):
                columns = model.columns     # a null in the first row must not drop a column
        resp = MagicMock()
        resp.status_code = 200
        resp.content = _rowset(rows, columns, self.typed)
        resp.text = resp.content.decode()
        return resp

    def data_requests(self):
        return [s for _c, s in self.statements if "FILTER(" in s]


def _client(models, catalog="Retail", typed=True, indexed=None):
    client = AnalysisServicesClient(host="https://ssas.example.com/olap/msmdpump.dll",
                                    username="u", password="p", catalog=catalog)
    server = FakeXmla(models, typed)
    client._http = server
    if indexed is not None:
        client.attach_table_metadata(indexed)
    return client, server


def _sales(n, *, tie_at_end=0, blanks=0):
    rows = []
    for i in range(n - tie_at_end - blanks):
        rows.append({
            "Sales[OrderID]": i + 1,
            "Sales[OrderDate]": (BASE + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S"),
            "Sales[Region]": ["East", "West"][i % 2] if i % 5 else None,
            "Sales[Revenue]": 10.5 * (i + 1),
        })
    last = rows[-1]["Sales[OrderDate]"]
    for i in range(tie_at_end):
        rows.append({"Sales[OrderID]": 100000 + i, "Sales[OrderDate]": last,
                     "Sales[Region]": "Tie", "Sales[Revenue]": 1.0})
    for i in range(blanks):
        rows.append({"Sales[OrderID]": 200000 + i, "Sales[OrderDate]": None,
                     "Sales[Region]": "Blank", "Sales[Revenue]": 2.0})
    return rows


def _collect(client, query, batch_rows=1000):
    batches = list(source_for(client).stream_batches(query, batch_rows))
    names = batches[0].column_names
    return names, [r for b in batches for r in zip(*[b.column(c).to_pylist() for c in names])]


def _page(monkeypatch, rows):
    monkeypatch.setattr(ass, "PAGE_ROWS", rows)


def _indexed(catalog, table, dax=True):
    return {"name": f"{catalog}/{table}", "metadata_json": {"analysis_services": {
        "catalog": catalog, "tableName": table, "supportsDax": dax,
        "modelType": "TABULAR" if dax else "MULTIDIMENSIONAL"}}}


# --------------------------------------------------------------------------
# Typed rows
# --------------------------------------------------------------------------

@pytest.mark.parametrize("typed", [True, False], ids=["schema-types", "per-value-types"])
def test_values_keep_their_types_and_nulls_keep_their_place(typed):
    """XMLA sends text; the type is in the schema or on the value. Numbers
    must land as numbers, dates as timestamps, and a null as a null in the
    right column rather than a shifted row."""
    client, _ = _client({"Retail": FakeDax(_sales(12))}, typed=typed)
    names, rows = _collect(client, "EVALUATE Sales")
    assert names == ["OrderID", "OrderDate", "Region", "Revenue"]
    by_id = {r[0]: r for r in rows}
    assert by_id[2] == (2, BASE + timedelta(days=1), "West", 21.0)
    assert by_id[1][2] is None and by_id[6][2] is None, "a missing element is a null Region"
    assert len(rows) == 12


def test_estimate_is_the_exact_count_the_budget_check_sees():
    client, _ = _client({"Retail": FakeDax(_sales(37))})
    est = extractor.estimate(client, "EVALUATE Sales")
    assert est.supported and est.rows == 37
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est, max_rows=36)
    extractor.check_budget(est, max_rows=37)


def test_preview_is_bounded_at_the_server():
    client, server = _client({"Retail": FakeDax(_sales(500))})
    cols, rows = extractor.preview(client, "EVALUATE Sales", 7)
    assert server.statements[-1][1].startswith("EVALUATE TOPN(7,")
    assert [c["name"] for c in cols] == ["OrderID", "OrderDate", "Region", "Revenue"]
    assert len(rows) == 7


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------

def test_a_large_result_arrives_whole_and_no_response_exceeds_a_page(monkeypatch):
    """The endpoint would return every row in one response. Each request is
    bounded instead, and the windows still cover every row exactly once,
    including rows tied on the window column and rows with none."""
    _page(monkeypatch, 60)
    client, server = _client({"Retail": FakeDax(_sales(900, tie_at_end=9, blanks=11))})
    _names, rows = _collect(client, "EVALUATE Sales")
    ids = [r[0] for r in rows]
    assert len(ids) == 900 and len(set(ids)) == 900
    assert len(server.data_requests()) > 900 // 60
    assert server.largest_response <= 61, "a request asked the server for more than a page"


def test_a_window_denser_than_a_page_is_split_not_stored_short(monkeypatch):
    """Half the rows sit in one day: the first even split overflows there."""
    _page(monkeypatch, 40)
    rows = _sales(200)
    for r in rows[100:]:
        r["Sales[OrderDate]"] = (BASE + timedelta(days=100, seconds=rows.index(r))).strftime("%Y-%m-%dT%H:%M:%S")
    client, server = _client({"Retail": FakeDax(rows)})
    _names, got = _collect(client, "EVALUATE Sales")
    assert len({r[0] for r in got}) == 200
    assert server.largest_response <= 41


def test_a_result_that_grows_after_the_count_is_still_read_whole(monkeypatch):
    """Counted under a page, over a page by the time it is fetched: the one
    bounded response cannot hold it, so it must not be taken as the result."""
    _page(monkeypatch, 50)
    model = FakeDax(_sales(40))
    client, _ = _client({"Retail": model})
    original = model.respond
    grown = []

    def respond(dax):
        answer = original(dax)
        if "COUNTROWS" in dax and not grown:
            grown.append(True)
            model.rows = _sales(180)
        return answer

    model.respond = respond
    _names, rows = _collect(client, "EVALUATE Sales")
    assert len({r[0] for r in rows}) == 180


def test_more_than_a_page_with_nothing_to_window_on_is_refused_when_saving(monkeypatch):
    _page(monkeypatch, 20)
    labels = [{"T[label]": f"x{i}"} for i in range(55)]
    client, _ = _client({"Retail": FakeDax(labels)})
    est = extractor.estimate(client, "EVALUATE T")
    assert est.rows == 55 and est.source_max_rows == 20
    with pytest.raises(extractor.ExtractionRefused, match="numeric or date column"):
        extractor.check_budget(est)
    with pytest.raises(RuntimeError, match="numeric or date column"):
        _collect(client, "EVALUATE T")


def test_fewer_rows_than_counted_fails_the_refresh(monkeypatch):
    _page(monkeypatch, 60)
    for model in (FakeDax(_sales(30), withhold=2), FakeDax(_sales(400), withhold=1)):
        client, _ = _client({"Retail": model})
        with pytest.raises(RuntimeError, match="rows counted"):
            _collect(client, "EVALUATE Sales")


def test_a_short_extraction_leaves_no_artifact_behind(monkeypatch, tmp_path):
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    client, _ = _client({"Retail": FakeDax(_sales(30), withhold=2)})
    with pytest.raises(RuntimeError):
        extractor.extract_to_artifact(client, "EVALUATE Sales", "sales", "conn-1")
    assert not list(tmp_path.rglob("*.db"))


def test_the_request_limit_is_the_organizations(monkeypatch):
    _page(monkeypatch, 20)
    client, _ = _client({"Retail": FakeDax(_sales(600))})
    client.extraction_limits = fast_limits.FastLimits(max_requests=16)
    with pytest.raises(RuntimeError, match="16 requests"):
        _collect(client, "EVALUATE Sales")
    est = extractor.estimate(client, "EVALUATE Sales")
    assert est.source_max_rows == 16 * 20, "said when saving, before any window is fetched"

    client.extraction_limits = fast_limits.FastLimits(max_requests=400)
    assert len(_collect(client, "EVALUATE Sales")[1]) == 600


def test_extraction_lands_typed_in_a_duckdb_artifact(monkeypatch, tmp_path):
    import duckdb

    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    _page(monkeypatch, 60)
    client, _ = _client({"Retail": FakeDax(_sales(250))})
    res = extractor.extract_to_artifact(client, "EVALUATE Sales", "sales", "conn-1")
    assert res.row_count == 250
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{res.artifact_path}' AS a (ENCRYPTION_KEY '{res.artifact_key}', READ_ONLY)")
    total, newest = con.execute(
        'SELECT sum("Revenue"), max("OrderDate") FROM a.main.sales WHERE "OrderID" <= 250').fetchone()
    con.close()
    assert total == pytest.approx(10.5 * 250 * 251 / 2)
    assert newest == BASE + timedelta(days=249)


# --------------------------------------------------------------------------
# Which catalog
# --------------------------------------------------------------------------

def test_the_configured_catalog_is_the_target():
    client, server = _client({"Retail": FakeDax(_sales(5)), "Finance": FakeDax(_sales(9))}, catalog="Finance")
    assert extractor.estimate(client, "EVALUATE Sales").rows == 9
    assert {c for c, _s in server.statements} == {"Finance"}


def test_without_one_the_catalog_is_found_from_the_tables_the_dax_names():
    indexed = [_indexed("Retail", "Sales"), _indexed("Finance", "Ledger")]
    client, server = _client({"Retail": FakeDax(_sales(5)), "Finance": FakeDax(_sales(9))},
                             catalog=None, indexed=indexed)
    assert extractor.estimate(client, "EVALUATE Ledger").rows == 9
    assert extractor.estimate(client, "EVALUATE Sales").rows == 5
    assert [c for c, _s in server.statements][0] == "Finance"


@pytest.mark.parametrize("indexed,query,problem", [
    ([_indexed("Retail", "Sales"), _indexed("Finance", "Sales")], "EVALUATE Sales", "2 catalogs"),
    ([_indexed("Retail", "Sales"), _indexed("Finance", "Ledger")], "EVALUATE Budget", "which catalog"),
    ([_indexed("Retail", "Sales"), _indexed("Cube", "Orders", dax=False)], "EVALUATE Orders", "Multidimensional"),
    ([], "EVALUATE Sales", "no catalog configured"),
])
def test_a_catalog_that_cannot_be_told_is_refused_not_guessed(indexed, query, problem):
    client, server = _client({}, catalog=None, indexed=indexed)
    with pytest.raises(RuntimeError, match=problem):
        _collect(client, query)
    assert not server.statements, "nothing may be sent to a guessed catalog"


# --------------------------------------------------------------------------
# MDX and refresh windows
# --------------------------------------------------------------------------

MDX = "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [Sales]"


def test_mdx_is_still_read_as_the_connector_reads_it():
    """Multidimensional models answer only MDX, which has no count and no
    windows. Such a table keeps the one-request path it had."""
    client, server = _client({"Retail": FakeDax(_sales(3))})
    est = extractor.estimate(client, MDX)
    assert not est.supported
    names, rows = _collect(client, MDX)
    assert rows == [("42",)] and len(server.statements) == 1
    assert server.statements[0][1] == MDX


def test_a_refresh_window_is_written_into_dax_and_taken_locally_for_mdx():
    client, _ = _client({"Retail": FakeDax(_sales(3))})
    window = incremental.Window(lo=datetime(2026, 3, 1), hi=None)
    dax = incremental.render(client, "EVALUATE Sales", "Sales[OrderDate]", incremental.KIND_TIMESTAMP, window)
    assert dax.startswith("EVALUATE FILTER(Sales, Sales[OrderDate] >= dt\"2026-03-01T00:00:00\"")
    assert incremental.filters_at_source(client, "EVALUATE Sales")

    assert incremental.render(client, MDX, "Revenue", incremental.KIND_NUMBER, window) == MDX
    assert not incremental.filters_at_source(client, MDX)
    with pytest.raises(incremental.IncrementalConfigError):
        incremental.render(client, MDX + " WHERE x > {{since}}", "x", incremental.KIND_NUMBER, window)


def test_an_incremental_window_reads_only_the_rows_in_it(monkeypatch):
    client, server = _client({"Retail": FakeDax(_sales(120))})
    window = incremental.Window(lo=BASE + timedelta(days=100), hi=BASE + timedelta(days=110))
    dax = incremental.render(client, "EVALUATE Sales", "Sales[OrderDate]", incremental.KIND_TIMESTAMP, window)
    _names, rows = _collect(client, dax)
    assert sorted(r[0] for r in rows) == list(range(101, 111))
    assert server.largest_response == 10


# --------------------------------------------------------------------------
# The connector's statement and catalog checks
# --------------------------------------------------------------------------

# DAX the connector's own execute_query refuses (`check_xmla_statement`).
_REFUSED_DAX = {
    "a nested comment": "EVALUATE Sales /* a /* b */ */",
    "a refused word": "EVALUATE Sales INTO Copy",
    "an ambiguous line end": "EVALUATE Sales -- note" + chr(0x2028) + "x",
}


@pytest.mark.parametrize("dax", list(_REFUSED_DAX.values()), ids=list(_REFUSED_DAX))
def test_dax_the_connector_refuses_is_refused_on_every_extraction_path_and_nothing_is_sent(dax):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, server = _client({"Retail": FakeDax(_sales(5))})
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(dax)
    with pytest.raises(ReadOnlyViolation):
        extractor.preview(client, dax, 3)
    with pytest.raises(ReadOnlyViolation):
        _collect(client, dax)
    assert not extractor.estimate(client, dax).supported
    assert server.statements == []


def test_a_pinned_catalog_other_than_the_configured_one_is_refused_and_nothing_is_sent():
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, server = _client({"Retail": FakeDax(_sales(5)), "Finance": FakeDax(_sales(9))}, catalog="Retail")
    client.extraction_target = {"catalog": "Finance"}
    with pytest.raises(ReadOnlyViolation):
        extractor.preview(client, "EVALUATE Sales", 3)
    assert server.statements == []
    client.extraction_target = {"catalog": "retail"}
    _cols, rows = extractor.preview(client, "EVALUATE Sales", 3)
    assert len(rows) == 3 and {c for c, _s in server.statements} == {"Retail"}
