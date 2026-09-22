"""Power BI native extraction source — windowed DAX extraction over executeQueries.

The HTTP boundary is faked with a small DAX server; everything above it — the
client's request handling, target resolution, the count check, window
splitting, batch shaping and the DuckDB artifact it ends up in — runs real.

The properties under test are the ones this source has and a cursor-based
source does not. executeQueries serves at most 100,000 rows or 1,000,000 values
per response and truncates silently, and DAX's WINDOW cannot page a base table,
so extraction is split into value windows over a numeric or date column and
verified against COUNTROWS. What has to hold: every row lands exactly once, a
tie on the cursor is not dropped at a window edge, blank cursors are not lost,
the model the DAX targets is found from the tables it names, and anything short
of the count fails loudly.
"""

import json
import re
from datetime import datetime, timedelta

import pytest
import requests

from app.data_sources.clients.powerbi_client import PowerBIClient
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast import powerbi_source as pbs
from app.data_sources.fast.powerbi_source import PowerBISource, parse_dax, preview_expr

WS = "ws-1"
SALES_DS = "ds-sales"
LEADS_DS = "ds-leads"
BASE = datetime(2026, 1, 1, 12, 0, 0)


# --------------------------------------------------------------------------
# A fake executeQueries endpoint
# --------------------------------------------------------------------------

class FakeDax:
    """Enough DAX to answer what the source actually sends.

    Holds one in-memory table per dataset and understands the statement shapes
    the source builds: COUNTROWS, TOPN, MINX/MAXX/blanks, FILTER windows,
    ISBLANK, and a bare EVALUATE of the body. Ceilings are applied the way the
    real endpoint applies them — silently.
    """

    def __init__(self, rows, ceiling_rows=None, ceiling_values=None, withhold=0):
        self.rows = rows                       # list[dict] keyed by 'T[col]'
        self.columns = list(rows[0].keys()) if rows else []
        self.ceiling_rows = ceiling_rows
        self.ceiling_values = ceiling_values
        self.withhold = withhold
        self.queries = []

    def respond(self, dax):
        self.queries.append(dax)
        body = dax.split("EVALUATE", 1)[1].strip()
        m = re.match(r'ROW\("n", COUNTROWS\((.*)\)\)$', body, re.S)
        if m:
            return [{"[n]": len(self._eval(m.group(1)))}]
        m = re.match(r'ROW\("lo", MINX\((.*?), (\S+)\), "hi", MAXX\(.*\)\)$', body, re.S)
        if m:
            col = m.group(2)
            vals = [r[col] for r in self.rows if r.get(col) is not None]
            blanks = len(self.rows) - len(vals)
            return [{"[lo]": min(vals) if vals else None, "[hi]": max(vals) if vals else None,
                     "[blanks]": blanks or None}]
        m = re.match(r"TOPN\((\d+), (.*)\)$", body, re.S)
        if m:
            n = int(m.group(1))
            inner = m.group(2).split(", ")[0] if "], " in m.group(2) else m.group(2)
            return self._truncate(self._eval(inner)[:n])
        return self._truncate(self._eval(body))

    def _eval(self, expr):
        expr = expr.strip()
        m = re.match(r"FILTER\((.*), ISBLANK\((\S+)\)\)$", expr, re.S)
        if m:
            return [r for r in self._eval(m.group(1)) if r.get(m.group(2)) is None]
        m = re.match(r"FILTER\((.*), (\S+) >= (.+?) && \2 (<=?) (.+)\)$", expr, re.S)
        if m:
            col, lo, op, hi = m.group(2), _lit(m.group(3)), m.group(4), _lit(m.group(5))
            out = []
            for r in self._eval(m.group(1)):
                v = r.get(col)
                if v is None:
                    continue
                v = _lit_value(v)
                if v >= lo and (v <= hi if op == "<=" else v < hi):
                    out.append(r)
            return out
        # the admin's body itself
        return list(self.rows)

    def _truncate(self, rows):
        out = rows
        if self.ceiling_rows is not None:
            out = out[: self.ceiling_rows]
        if self.ceiling_values is not None and self.columns:
            out = out[: max(1, self.ceiling_values // len(self.columns))]
        if self.withhold and out:
            out = out[: -self.withhold]
        return out


def _lit(s):
    s = s.strip()
    if s.startswith('dt"'):
        return datetime.fromisoformat(s[3:-1])
    return float(s)


def _lit_value(v):
    if isinstance(v, str):
        return datetime.fromisoformat(v)
    return float(v)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, servers):
        self.servers = servers      # dataset id -> FakeDax
        self.urls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.urls.append(url)
        ds_id = url.split("/datasets/")[1].split("/")[0]
        server = self.servers.get(ds_id)
        if server is None:
            return FakeResponse({"error": {"code": "PowerBINotAuthorizedException"}}, 401)
        rows = server.respond(json["queries"][0]["query"])
        return FakeResponse({"results": [{"tables": [{"rows": rows}]}]})

    def close(self):
        pass


def _client(monkeypatch, servers, catalog=None, target=None):
    session = FakeSession(servers)
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = PowerBIClient(tenant_id="t", client_id="c", access_token="tok")
    client.attach_table_metadata(catalog if catalog is not None else CATALOG)
    client.extraction_target = target
    return client, session


CATALOG = [
    {"name": "SalesPush/Sales", "metadata_json": {"powerbi": {
        "datasetId": SALES_DS, "workspaceId": WS, "datasetName": "SalesPush", "tableName": "Sales"}}},
    {"name": "SalesPush/Customers", "metadata_json": {"powerbi": {
        "datasetId": SALES_DS, "workspaceId": WS, "datasetName": "SalesPush", "tableName": "Customers"}}},
    {"name": "Leads/Mock Leads", "metadata_json": {"powerbi": {
        "datasetId": LEADS_DS, "workspaceId": WS, "datasetName": "Leads", "tableName": "Mock Leads"}}},
]


def _sales(n, *, tie_at_end=0, blanks=0, dates=True):
    rows = []
    for i in range(n - tie_at_end - blanks):
        rows.append({
            "Sales[OrderID]": i + 1,
            "Sales[OrderDate]": (BASE + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S") if dates else None,
            "Sales[Region]": ["East", "West"][i % 2],
            "Sales[Revenue]": 10.5 * (i + 1),
        })
    last = rows[-1]["Sales[OrderDate]"] if rows else BASE.strftime("%Y-%m-%dT%H:%M:%S")
    for i in range(tie_at_end):
        rows.append({"Sales[OrderID]": 100000 + i, "Sales[OrderDate]": last,
                     "Sales[Region]": "Tie", "Sales[Revenue]": 1.0})
    for i in range(blanks):
        rows.append({"Sales[OrderID]": 200000 + i, "Sales[OrderDate]": None,
                     "Sales[Region]": "Blank", "Sales[Revenue]": 2.0})
    return rows


def _collect(src, dax, batch_rows=1000):
    batches = list(src.stream_batches(dax, batch_rows))
    names = batches[0].column_names if batches else []
    rows = [r for b in batches for r in zip(*[b.column(c).to_pylist() for c in names])]
    return names, rows


# --------------------------------------------------------------------------
# Resolution & gating
# --------------------------------------------------------------------------

def test_the_powerbi_client_resolves_to_its_native_source():
    from app.data_sources.fast.sources import source_for

    client = PowerBIClient(tenant_id="t", client_id="c", client_secret="s")
    assert isinstance(source_for(client), PowerBISource)


def test_powerbi_is_an_accelerable_verified_type():
    from app.services.custom_query_service import (
        ACCELERABLE_TYPES, VERIFIED_TYPES, is_accelerable_type,
    )

    assert "powerbi" in ACCELERABLE_TYPES
    assert "powerbi" in VERIFIED_TYPES
    assert is_accelerable_type("powerbi")


# --------------------------------------------------------------------------
# Parsing the admin's DAX
# --------------------------------------------------------------------------

def test_parse_splits_define_body_and_order_by():
    q = parse_dax('DEFINE VAR __t = FILTER(Sales, Sales[Region] = "East")\n'
                  'EVALUATE __t ORDER BY Sales[OrderID] DESC, Sales[Revenue];')
    assert q.define.startswith("DEFINE VAR __t")
    assert q.body == "__t"
    assert q.order_items == [("Sales[OrderID]", "DESC"), ("Sales[Revenue]", "ASC")]
    assert q.statement('ROW("n", COUNTROWS(__t))').endswith('EVALUATE ROW("n", COUNTROWS(__t))')
    assert preview_expr(q, 5) == "TOPN(5, __t, Sales[OrderID], DESC, Sales[Revenue], ASC)"


def test_parse_ignores_keywords_inside_strings_comments_and_parens():
    q = parse_dax('EVALUATE FILTER(Sales, Sales[Note] = "ORDER BY me") -- EVALUATE not really\n'
                  'ORDER BY Sales[OrderID]')
    assert q.body == 'FILTER(Sales, Sales[Note] = "ORDER BY me")'
    assert q.order_by == "ORDER BY Sales[OrderID]"


def test_start_at_makes_the_order_clause_opaque_to_topn():
    q = parse_dax("EVALUATE Sales ORDER BY Sales[OrderID] START AT 5")
    assert q.body == "Sales"
    assert q.order_items is None
    assert preview_expr(q, 3) == "TOPN(3, Sales)"


@pytest.mark.parametrize("bad, msg", [
    ("SELECT * FROM Sales", "must contain EVALUATE"),
    ("EVALUATE Sales EVALUATE Customers", "single EVALUATE"),
    ("EVALUATE", "needs a table expression"),
    ("   ", "cannot be empty"),
])
def test_parse_refuses_what_is_not_one_dax_query(bad, msg):
    with pytest.raises(RuntimeError, match=msg):
        parse_dax(bad)


# --------------------------------------------------------------------------
# Which semantic model
# --------------------------------------------------------------------------

def test_target_is_resolved_from_the_tables_the_dax_names(monkeypatch):
    client, session = _client(monkeypatch, {SALES_DS: FakeDax(_sales(5))})
    target = pbs.resolve_target(client, "EVALUATE FILTER(Sales, Sales[Region] = \"East\")")
    assert target["datasetId"] == SALES_DS and target["workspaceId"] == WS

    quoted = pbs.resolve_target(client, "EVALUATE 'Mock Leads'")
    assert quoted["datasetId"] == LEADS_DS


def test_an_explicit_target_wins_over_resolution(monkeypatch):
    client, _ = _client(monkeypatch, {}, target={"datasetId": "pinned", "workspaceId": "w"})
    assert pbs.resolve_target(client, "EVALUATE Sales")["datasetId"] == "pinned"


def test_unknown_tables_and_ambiguous_names_are_refused(monkeypatch):
    client, _ = _client(monkeypatch, {})
    with pytest.raises(RuntimeError, match="none of the tables it references"):
        pbs.resolve_target(client, "EVALUATE Nope")

    ambiguous = CATALOG + [{"name": "Other/Sales", "metadata_json": {"powerbi": {
        "datasetId": "ds-other", "workspaceId": WS, "datasetName": "Other", "tableName": "Sales"}}}]
    client, _ = _client(monkeypatch, {}, catalog=ambiguous)
    with pytest.raises(RuntimeError, match="2 semantic models \\(Other, SalesPush\\)"):
        pbs.resolve_target(client, "EVALUATE Sales")


def test_an_empty_catalog_says_to_index_first(monkeypatch):
    client, _ = _client(monkeypatch, {}, catalog=[])
    with pytest.raises(RuntimeError, match="catalog is empty"):
        pbs.resolve_target(client, "EVALUATE Sales")


# --------------------------------------------------------------------------
# Estimate & preview
# --------------------------------------------------------------------------

def test_estimate_is_the_exact_count(monkeypatch):
    client, _ = _client(monkeypatch, {SALES_DS: FakeDax(_sales(42))})
    est = extractor.estimate(client, "EVALUATE Sales")
    assert est.supported and est.rows == 42
    assert "COUNTROWS" in est.note


def test_estimate_refuses_at_save_time_when_nothing_can_window(monkeypatch):
    rows = [{"T[label]": f"x{i}"} for i in range(30)]
    client, _ = _client(monkeypatch, {SALES_DS: FakeDax(rows)}, target={"datasetId": SALES_DS})
    monkeypatch.setattr(pbs, "MAX_RESPONSE_ROWS", 10)
    est = extractor.estimate(client, "EVALUATE T")
    assert est.rows == 30 and est.source_max_rows == 10
    with pytest.raises(extractor.ExtractionRefused, match="numeric or date column"):
        extractor.check_budget(est)


def test_preview_is_one_bounded_topn_with_clean_names(monkeypatch):
    server = FakeDax(_sales(50))
    client, _ = _client(monkeypatch, {SALES_DS: server})
    cols, rows = extractor.preview(client, "EVALUATE Sales", 5)
    assert [c["name"] for c in cols] == ["OrderID", "OrderDate", "Region", "Revenue"]
    assert len(rows) == 5
    assert all("TOPN(5, Sales" in q for q in server.queries[-1:])


def test_preview_keeps_the_admins_order_for_display(monkeypatch):
    server = FakeDax(_sales(50))
    client, _ = _client(monkeypatch, {SALES_DS: server})
    extractor.preview(client, "EVALUATE Sales ORDER BY Sales[Revenue] DESC", 5)
    sent = server.queries[-1]
    assert "TOPN(5, Sales, Sales[Revenue], DESC)" in sent
    assert sent.rstrip().endswith("ORDER BY Sales[Revenue] DESC")


def test_preview_cuts_the_ties_topn_keeps(monkeypatch):
    class Ties(FakeDax):
        def respond(self, dax):
            rows = super().respond(dax)
            return rows + rows[:2] if "TOPN(" in dax else rows

    client, _ = _client(monkeypatch, {SALES_DS: Ties(_sales(10))})
    _cols, rows = extractor.preview(client, "EVALUATE Sales", 3)
    assert len(rows) == 3


def test_a_bad_dax_error_reaches_the_admin_with_the_engines_words(monkeypatch):
    class Broken(FakeSession):
        def request(self, method, url, json=None, headers=None, timeout=None):
            return FakeResponse({"error": {"pbi.error": {"details": [
                {"detail": {"value": "Query (1, 10) The value for 'Nope' cannot be determined."}}]}}}, 400)

    session = Broken({})
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = PowerBIClient(tenant_id="t", client_id="c", access_token="tok")
    client.attach_table_metadata(CATALOG)
    with pytest.raises(RuntimeError, match="cannot be determined"):
        PowerBISource(client).preview("EVALUATE Sales", 5)


# --------------------------------------------------------------------------
# Extraction: single response, then windows
# --------------------------------------------------------------------------

def test_small_results_are_one_request_and_dates_become_timestamps(monkeypatch):
    server = FakeDax(_sales(20))
    client, _ = _client(monkeypatch, {SALES_DS: server})
    names, rows = _collect(PowerBISource(client), "EVALUATE Sales")
    assert names == ["OrderID", "OrderDate", "Region", "Revenue"]
    assert len(rows) == 20
    assert isinstance(rows[0][1], datetime)
    # count + sample + the fetch itself
    assert sum("FILTER(" in q for q in server.queries) == 0


def test_large_results_are_windowed_over_the_date_column_and_complete(monkeypatch):
    server = FakeDax(_sales(1000, tie_at_end=7), ceiling_rows=120)
    monkeypatch.setattr(pbs, "MAX_RESPONSE_ROWS", 120)
    client, _ = _client(monkeypatch, {SALES_DS: server})
    names, rows = _collect(PowerBISource(client), "EVALUATE Sales")
    ids = [r[0] for r in rows]
    assert len(ids) == 1000 and len(set(ids)) == 1000
    assert all("Sales[OrderDate]" in q for q in server.queries if "FILTER(" in q)


def test_blank_cursor_rows_are_fetched_on_their_own(monkeypatch):
    server = FakeDax(_sales(400, blanks=5), ceiling_rows=100)
    monkeypatch.setattr(pbs, "MAX_RESPONSE_ROWS", 100)
    client, _ = _client(monkeypatch, {SALES_DS: server})
    _names, rows = _collect(PowerBISource(client), "EVALUATE Sales")
    assert len(rows) == 400
    assert sum(1 for r in rows if r[2] == "Blank") == 5
    assert any("ISBLANK(" in q for q in server.queries)


def test_a_numeric_column_windows_when_there_is_no_date(monkeypatch):
    server = FakeDax(_sales(500, dates=False), ceiling_rows=90)
    monkeypatch.setattr(pbs, "MAX_RESPONSE_ROWS", 90)
    client, _ = _client(monkeypatch, {SALES_DS: server})
    _names, rows = _collect(PowerBISource(client), "EVALUATE Sales")
    assert len(rows) == 500
    used = {re.search(r"FILTER\(Sales, (\S+) >=", q).group(1)
            for q in server.queries if "FILTER(Sales, " in q and ">=" in q}
    assert used <= {"Sales[OrderID]", "Sales[Revenue]"} and used


def test_the_values_ceiling_counts_too(monkeypatch):
    # 4 columns, 30-value ceiling: 7 rows per response, silently.
    server = FakeDax(_sales(100), ceiling_values=30)
    monkeypatch.setattr(pbs, "MAX_RESPONSE_VALUES", 30)
    client, _ = _client(monkeypatch, {SALES_DS: server})
    _names, rows = _collect(PowerBISource(client), "EVALUATE Sales")
    assert len(rows) == 100


def test_a_short_response_is_refused_not_stored(monkeypatch):
    server = FakeDax(_sales(50), withhold=3)
    client, _ = _client(monkeypatch, {SALES_DS: server})
    with pytest.raises(RuntimeError, match="47 of 50 rows"):
        _collect(PowerBISource(client), "EVALUATE Sales")


def test_too_many_rows_on_one_cursor_value_is_a_clear_error(monkeypatch):
    rows = [{"T[k]": 1 if i < 40 else 2, "T[v]": i} for i in range(50)]
    server = FakeDax(rows, ceiling_rows=10)
    monkeypatch.setattr(pbs, "MAX_RESPONSE_ROWS", 10)
    client, _ = _client(monkeypatch, {SALES_DS: server}, target={"datasetId": SALES_DS})
    # Force the poorly-spread column to be the cursor.
    monkeypatch.setattr(pbs._Sample, "_classify", lambda self: (
        self.kinds.update({"T[k]": pbs.KIND_INT}), setattr(self, "cursor", "T[k]")))
    with pytest.raises(RuntimeError, match="share a single T\\[k\\] value"):
        _collect(PowerBISource(client), "EVALUATE T")


def test_zero_rows_has_no_shape_and_says_so(monkeypatch):
    class Empty(FakeDax):
        def respond(self, dax):
            return [{"[n]": None}] if "COUNTROWS" in dax else []

    client, _ = _client(monkeypatch, {SALES_DS: Empty([])})
    with pytest.raises(RuntimeError, match="returns no rows"):
        _collect(PowerBISource(client), "EVALUATE Sales")


# --------------------------------------------------------------------------
# All the way into an artifact
# --------------------------------------------------------------------------

def test_extraction_lands_in_a_duckdb_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(artifacts, "artifacts_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(artifacts, "new_artifact_path",
                        lambda cid: tmp_path / f"{cid}.duckdb", raising=False)
    server = FakeDax(_sales(300), ceiling_rows=80)
    monkeypatch.setattr(pbs, "MAX_RESPONSE_ROWS", 80)
    client, _ = _client(monkeypatch, {SALES_DS: server})

    res = extractor.extract_to_artifact(client, "EVALUATE Sales", "sales_cache", "conn-1")
    assert res.row_count == 300
    assert [c["name"] for c in res.columns] == ["OrderID", "OrderDate", "Region", "Revenue"]

    con = artifacts.connect_encrypted(res.artifact_path, res.artifact_key)
    n, = con.execute('SELECT count(*) FROM "sales_cache"').fetchone()
    lo, hi = con.execute('SELECT min("OrderDate"), max("OrderDate") FROM "sales_cache"').fetchone()
    con.close()
    assert n == 300
    assert lo == BASE and hi == BASE + timedelta(days=299)
