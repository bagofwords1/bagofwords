"""Priority ERP extraction source: one form paged to its end.

The HTTP boundary is a fake Priority OData endpoint that answers the requests
the source makes (`$count`, `$top` / `$skip` pages, `$filter`, `$since`) and
can misbehave the ways a real one is documented to: capping a response at its
own row limit without saying so, throttling with 429, timing out on a large
page, not answering `$count` at all. Above it the real client, source and
DuckDB artifact run. One test at the end goes over real HTTP to the repo's
mock Priority service, for what a fake session cannot show: URL encoding.

The properties that matter for copying a form rather than answering an agent:
every row arrives exactly once however the server sizes its pages, a result
shorter than its count is refused rather than stored, the count is what the
budget check sees and is never invented, the request limit is the
organization's, and sub-forms are refused before anything is saved.
"""
import re
import socket
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests
from fastapi import HTTPException

from app.data_sources.clients import priority_erp_client
from app.data_sources.clients.priority_erp_client import PriorityErpClient
from app.data_sources.fast import artifacts, extractor, incremental
from app.data_sources.fast import limits as fast_limits
from app.data_sources.fast import priority_erp_source as pes
from app.data_sources.fast.sources import source_for
from app.services.custom_query_service import CustomQueryService

ROOT = "https://erp.example.com/odata/Priority/tabula.ini/demo"
_ISO = re.compile(r"(?<!')\b(\d{4}-\d{2}-\d{2}T[\d:.]+Z)")


def _when(text):
    return datetime.fromisoformat(text[:-1] + "+00:00")


class _Resp:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else ("" if payload is None else str(payload))
        self.headers = headers or {}
        self.reason = ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Null:
    """A null in a filter: equal to null, and in no order with anything."""

    def __eq__(self, other):
        return other is None or isinstance(other, _Null)

    def _never(self, _other):
        return False

    __lt__ = __le__ = __gt__ = __ge__ = _never
    __hash__ = None


class FakePriority:
    """A service root with forms of rows, standing in for `requests.Session`."""

    def __init__(self, forms, *, count=True, max_lines=None, ends_at=None,
                 throttle=(), retry_after=None, slow_above=None, ignore_top=False):
        self.forms = forms
        self.count = count              # whether FORM/$count is answered
        self.max_lines = max_lines      # the server's own cap on one response
        self.ends_at = ends_at          # rows past this never arrive
        self.throttle = set(throttle)   # request numbers (1-based) answered 429
        self.retry_after = retry_after
        self.slow_above = slow_above    # a page asking for more than this times out
        self.ignore_top = ignore_top
        self.requests = []              # (path, options) of every request
        self.pages = []                 # (skip, top) of every page request
        self.closed = 0
        self.headers = {}
        self.verify = True

    def close(self):
        self.closed += 1

    def get(self, url, params=None, timeout=None, headers=None):
        parts = urlsplit(url)
        path = unquote(parts.path)[len(urlsplit(ROOT).path) + 1:]
        opts = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.requests.append((path, opts))
        if len(self.requests) in self.throttle:
            return _Resp(429, {"error": {"message": "throttled"}},
                         headers={"Retry-After": str(self.retry_after)} if self.retry_after else {})
        form, _, tail = path.partition("/")
        if form not in self.forms:
            return _Resp(404, {"error": {"message": f"Resource not found for the segment '{form}'."}})
        if "$expand" in opts:
            raise AssertionError("an $expand request reached the server")
        rows = [r for r in self.forms[form] if self._matches(r, opts)]
        if tail == "$count":
            if not self.count:
                return _Resp(400, {"error": {"message": "The query option is not supported."}})
            return _Resp(200, text=str(len(rows)))
        if opts.get("$orderby"):
            for part in reversed(opts["$orderby"].split(",")):
                name, _, direction = part.strip().partition(" ")
                rows.sort(key=lambda r: (r.get(name) is None, r.get(name)), reverse=direction == "desc")
        skip, top = int(opts.get("$skip", 0)), opts.get("$top")
        if top is not None:
            self.pages.append((skip, int(top)))
            if self.slow_above is not None and int(top) > self.slow_above:
                raise requests.Timeout("read timed out")
        available = rows[: self.ends_at] if self.ends_at is not None else rows
        size = len(available) if top is None or self.ignore_top else int(top)
        if self.max_lines is not None:
            size = min(size, self.max_lines)
        page = available[skip:skip + size]
        if opts.get("$select"):
            names = opts["$select"].split(",")
            page = [{n: r.get(n) for n in names} for r in page]
        return _Resp(200, {"@odata.context": f"{ROOT}/$metadata#{form}", "value": page})

    @staticmethod
    def _matches(row, opts):
        if opts.get("$since") and not (row.get("UDATE") and _when(row["UDATE"]) >= _when(opts["$since"])):
            return False
        expr = opts.get("$filter")
        if not expr:
            return True
        py = _ISO.sub(r"D('\1')", expr)
        for odata, op in ((" eq null", " == None"), (" eq ", " == "), (" ge ", " >= "),
                          (" gt ", " > "), (" lt ", " < "), (" le ", " <= ")):
            py = py.replace(odata, op)
        values = {k: _Null() if v is None else _when(v) if isinstance(v, str) and _ISO.fullmatch(v) else v
                  for k, v in row.items()}
        return bool(eval(py, {"__builtins__": {}, "D": _when}, values))  # noqa: S307 - test fake


def _invoices(n, null_dates=()):
    """`n` rows, one a day from 2026-01-01, a few without a change date."""
    return [{
        "IVNUM": f"IN{i + 1:06d}",
        "CUSTNAME": ["1011", "2020", "3030"][i % 3],
        "TOTAL": round(10.5 * (i + 1), 2),
        "UDATE": None if i in null_dates else f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T08:30:00Z",
    } for i in range(n)]


@pytest.fixture
def serve(monkeypatch):
    """Put a FakePriority behind the real client; returns (client, server)."""
    def _serve(server_or_rows, **kwargs):
        server = server_or_rows if isinstance(server_or_rows, FakePriority) \
            else FakePriority({"INVOICES": server_or_rows}, **kwargs)
        monkeypatch.setattr(priority_erp_client, "pinned_session", lambda *a, **k: server)
        client = PriorityErpClient(service_root=ROOT, pat="token", max_calls_per_minute=0)
        return client, server
    return _serve


@pytest.fixture
def waits(monkeypatch):
    """Waits for the rate limit are recorded, not slept."""
    slept = []
    monkeypatch.setattr(pes, "_sleep", slept.append)
    return slept


def _page(monkeypatch, rows):
    monkeypatch.setattr(pes, "PAGE_ROWS", rows)
    monkeypatch.setattr(pes, "MIN_PAGE_ROWS", max(1, rows // 8))


def _collect(client, query, batch_rows=1000):
    batches = list(source_for(client).stream_batches(query, batch_rows))
    names = batches[0].column_names
    return names, [r for b in batches for r in zip(*[b.column(c).to_pylist() for c in names], strict=True)]


def _ids(rows):
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# The definition
# --------------------------------------------------------------------------

REFUSED = [
    "SELECT * FROM INVOICES",
    "INVOICES?$expand=INVOICEITEMS_SUBFORM",
    "INVOICES?$filter=CUSTNAME eq '1011'&$expand=INVOICEITEMS_SUBFORM($select=PARTNAME)",
    "INVOICES('IN000001')",
    "INVOICES('IN000001')/INVOICEITEMS_SUBFORM",
    "INVOICES?$apply=aggregate(TOTAL with sum as T)",
    "INVOICES?$top=ten",
    "",
]


@pytest.mark.parametrize("bad", REFUSED)
def test_a_definition_that_is_not_one_pageable_form_is_refused_before_any_request(serve, bad):
    client, server = serve(_invoices(5))
    with pytest.raises(ValueError):
        _collect(client, bad)
    with pytest.raises(ValueError):
        source_for(client).preview(bad, 5)
    assert not server.requests, "nothing may be sent for a definition that cannot be cached"


@pytest.mark.parametrize("bad", [d for d in REFUSED if d])
def test_saving_refuses_such_a_definition_with_a_400(bad):
    connection = SimpleNamespace(type="priority_erp")
    with pytest.raises(HTTPException) as refused:
        CustomQueryService.ensure_definition_allowed(connection, bad)
    assert refused.value.status_code == 400 and refused.value.detail


def test_saving_accepts_a_pageable_definition_and_leaves_other_connectors_alone():
    ok = "INVOICES?$filter=CUSTNAME eq '1011'&$select=IVNUM,TOTAL&$orderby=IVNUM&$top=500"
    CustomQueryService.ensure_definition_allowed(SimpleNamespace(type="priority_erp"), ok)
    CustomQueryService.ensure_definition_allowed(SimpleNamespace(type="priority_erp"),
                                                 "INVOICES?$since={{since}}")
    # What Priority must refuse is another connector's ordinary query.
    CustomQueryService.ensure_definition_allowed(SimpleNamespace(type="postgresql"), REFUSED[0])


CATALOG = [
    {"name": "INVOICES", "metadata_json": {"priority_erp": {"catalog_version": 2, "form": "INVOICES"}}},
    {"name": "INVOICEITEMS", "metadata_json": {"priority_erp": {
        "catalog_version": 2, "subform_of": "INVOICES", "navigation": "INVOICEITEMS_SUBFORM",
        "path": [{"form": "INVOICES", "keys": ["IVNUM"]},
                 {"form": "INVOICEITEMS", "keys": ["KLINE"], "navigation": "INVOICEITEMS_SUBFORM"}],
    }}},
]


def test_saving_refuses_a_sub_form_the_catalog_knows_and_names_its_parent():
    connection = SimpleNamespace(type="priority_erp")
    with pytest.raises(HTTPException) as refused:
        CustomQueryService.ensure_definition_allowed(connection, "INVOICEITEMS?$select=PARTNAME", CATALOG)
    assert refused.value.status_code == 400 and "INVOICES" in refused.value.detail
    CustomQueryService.ensure_definition_allowed(connection, "INVOICES?$select=IVNUM", CATALOG)


def test_a_sub_form_named_as_a_table_is_refused_and_names_its_parent(serve):
    client, server = serve(_invoices(5))
    client.attach_table_metadata(CATALOG)
    with pytest.raises(ValueError, match="INVOICES"):
        _collect(client, "INVOICEITEMS?$select=PARTNAME")
    assert not server.requests


# --------------------------------------------------------------------------
# Estimate and preview
# --------------------------------------------------------------------------

def test_estimate_is_the_exact_count_of_the_filtered_form_and_reads_no_rows(serve):
    client, server = serve(_invoices(90))
    matching = sum(1 for r in _invoices(90) if r["CUSTNAME"] == "2020")
    est = extractor.estimate(client, "INVOICES?$filter=CUSTNAME eq '2020'&$select=IVNUM")
    assert est.supported and est.rows == matching
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est, max_rows=matching - 1)
    extractor.check_budget(est, max_rows=matching)
    assert not server.pages, "an estimate reads no rows"


@pytest.mark.parametrize("options, expected", [("$top=40", 40), ("$top=400", 83), ("$skip=80", 3),
                                               ("$skip=10&$top=20", 20), ("$skip=500", 0)])
def test_the_definitions_own_top_and_skip_bound_the_estimate_and_the_table(serve, monkeypatch, options, expected):
    _page(monkeypatch, 16)
    client, _server = serve(_invoices(83))
    query = f"INVOICES?$orderby=IVNUM&{options}"
    assert extractor.estimate(client, query).rows == expected
    _names, rows = _collect(client, query)
    skip = int(dict(parse_qsl(options)).get("$skip", 0))
    assert _ids(rows) == [f"IN{i + 1:06d}" for i in range(skip, skip + expected)]


def test_a_server_without_count_gives_no_estimate_rather_than_an_invented_one(serve, monkeypatch):
    _page(monkeypatch, 20)
    client, server = serve(_invoices(70), count=False)
    est = extractor.estimate(client, "INVOICES")
    assert not est.supported and est.rows is None and "$count" in est.note
    extractor.check_budget(est, max_rows=1)     # nothing to refuse on
    assert len(_collect(client, "INVOICES")[1]) == 70, "the form is still read to its end"
    assert not any(skip + top <= 70 and top == 0 for skip, top in server.pages)


def test_a_failing_estimate_downgrades_instead_of_raising(serve):
    client, _server = serve(_invoices(3))
    est = extractor.estimate(client, REFUSED[1])
    assert not est.supported and "$expand" in est.note


def test_preview_is_one_bounded_request(serve):
    client, server = serve(_invoices(500))
    cols, rows = extractor.preview(client, "INVOICES?$select=IVNUM,TOTAL,UDATE", 6)
    assert [c["name"] for c in cols] == ["IVNUM", "TOTAL", "UDATE"]
    assert _ids(rows) == [f"IN{i:06d}" for i in range(1, 7)]
    assert server.pages == [(0, 6)]


# --------------------------------------------------------------------------
# Paging to the end
# --------------------------------------------------------------------------

@pytest.mark.parametrize("count", [True, False], ids=["counted", "no-count"])
@pytest.mark.parametrize("max_lines", [None, 7, 1], ids=["as-asked", "server-caps-responses", "one-row-responses"])
def test_every_row_arrives_once_however_the_server_caps_a_response(serve, monkeypatch, max_lines, count):
    """Priority cuts a response at its own row limit with nothing in it to say
    so; that must cost requests, never rows."""
    _page(monkeypatch, 25)
    client, server = serve(_invoices(130), max_lines=max_lines, count=count)
    _names, rows = _collect(client, "INVOICES")
    assert _ids(rows) == [f"IN{i:06d}" for i in range(1, 131)]
    assert all(top <= 25 for _skip, top in server.pages), "a page asked for more than the page size"


def test_rows_are_handed_over_a_page_at_a_time_so_progress_moves(serve, monkeypatch):
    _page(monkeypatch, 20)
    client, server = serve(_invoices(90))
    batches = source_for(client).stream_batches("INVOICES", 50_000)
    seen = []
    for batch in batches:
        seen.append((batch.num_rows, len(server.pages)))
    assert [rows for rows, _pages in seen] == [20, 20, 20, 20, 10]
    assert [pages for _rows, pages in seen] == [1, 2, 3, 4, 5], "each page was yielded before the next was asked for"


def test_a_result_that_ends_before_its_count_is_refused(serve, monkeypatch):
    _page(monkeypatch, 25)
    client, _server = serve(_invoices(130), ends_at=90)
    with pytest.raises(RuntimeError, match="before its row count"):
        _collect(client, "INVOICES")


def test_a_server_that_ignores_top_is_refused_rather_than_stored_with_repeats(serve, monkeypatch):
    _page(monkeypatch, 25)
    client, _server = serve(_invoices(130), ignore_top=True)
    with pytest.raises(RuntimeError, match="returned"):
        _collect(client, "INVOICES")


def test_a_short_result_leaves_no_artifact_and_a_whole_one_is_queryable(serve, monkeypatch, tmp_path):
    import duckdb

    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    _page(monkeypatch, 25)
    client, _server = serve(_invoices(130), ends_at=90)
    with pytest.raises(RuntimeError):
        extractor.extract_to_artifact(client, "INVOICES", "invoices", "conn-1")
    assert not list(tmp_path.rglob("*.db"))

    client, _server = serve(_invoices(130), max_lines=9)
    progress = []
    res = extractor.extract_to_artifact(client, "INVOICES", "invoices", "conn-1", on_rows=progress.append)
    assert res.row_count == 130
    assert progress == sorted(progress) and len(progress) > 5 and progress[-1] == 130
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{res.artifact_path}' AS a (ENCRYPTION_KEY '{res.artifact_key}', READ_ONLY)")
    total, newest = con.execute("SELECT sum(TOTAL), max(UDATE) FROM a.main.invoices").fetchone()
    con.close()
    assert total == pytest.approx(10.5 * 130 * 131 / 2)
    # Dates arrive as text; stored as text they could not be compared or be a cursor.
    assert newest.astimezone(UTC) == datetime(2026, 5, 18, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize("query, expected", [
    ("INVOICES?$filter=CUSTNAME eq 'nobody'&$select=IVNUM,TOTAL", ["IVNUM", "TOTAL"]),
    ("INVOICES?$filter=CUSTNAME eq 'nobody'", ["IVNUM", "CUSTNAME", "TOTAL", "UDATE"]),
])
def test_an_empty_result_is_an_empty_table_with_its_columns(serve, query, expected):
    client, _server = serve(_invoices(4))
    names, rows = _collect(client, query)
    assert names == expected and rows == []


def test_an_empty_form_with_no_select_has_no_shape_and_says_so(serve):
    client, _server = serve([])
    with pytest.raises(RuntimeError, match=r"\$select"):
        _collect(client, "INVOICES")


def test_the_forms_key_orders_the_pages_when_the_catalog_knows_it(serve, monkeypatch):
    """Offset paging needs one order across requests; the key breaks ties after
    the admin's own order, and is left out when $select would not return it."""
    _page(monkeypatch, 10)
    client, server = serve(_invoices(30))
    client.attach_table_metadata([{"name": "INVOICEITEMS", "metadata_json": {"priority_erp": {
        "catalog_version": 2, "subform_of": "INVOICES",
        "path": [{"form": "INVOICES", "keys": ["IVNUM"]}, {"form": "INVOICEITEMS", "keys": ["KLINE"]}],
    }}}])

    def orders(query):
        server.requests.clear()
        _collect(client, query)
        return {opts.get("$orderby") for path, opts in server.requests if "$top" in opts}

    server.requests.clear()
    source_for(client).preview("INVOICES?$orderby=CUSTNAME desc&$top=1", 5)
    assert server.requests[-1][1]["$orderby"] == "CUSTNAME desc,IVNUM", "the preview shows the row extraction stores"

    assert orders("INVOICES") == {"IVNUM"}
    assert orders("INVOICES?$orderby=CUSTNAME desc") == {"CUSTNAME desc,IVNUM"}
    assert orders("INVOICES?$select=CUSTNAME,TOTAL") == {None}


# --------------------------------------------------------------------------
# Rate limit, slow pages, request limit, cancelling
# --------------------------------------------------------------------------

def test_a_429_is_waited_out_and_the_page_read_again(serve, monkeypatch, waits):
    _page(monkeypatch, 20)
    client, server = serve(_invoices(90), throttle={3, 4}, retry_after=7)
    _names, rows = _collect(client, "INVOICES")
    assert _ids(rows) == [f"IN{i:06d}" for i in range(1, 91)], "a throttled page is neither lost nor repeated"
    assert waits == [7.0, 7.0], "the server's Retry-After is what is waited"


def test_a_wait_is_bounded_when_the_server_names_none_or_a_long_one(serve, monkeypatch, waits):
    _page(monkeypatch, 20)
    client, _server = serve(_invoices(30), throttle={2})
    _collect(client, "INVOICES")
    client, _server = serve(_invoices(30), throttle={2}, retry_after=3600)
    _collect(client, "INVOICES")
    assert len(waits) == 2 and all(0 < w <= pes.RATE_LIMIT_MAX_WAIT_SECONDS for w in waits)


def test_a_rate_limit_that_never_clears_fails_the_refresh(serve, monkeypatch, waits, tmp_path):
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    _page(monkeypatch, 20)
    client, server = serve(_invoices(90), throttle=set(range(4, 200)))
    with pytest.raises(Exception, match="429"):
        extractor.extract_to_artifact(client, "INVOICES", "invoices", "conn-1")
    assert not list(tmp_path.rglob("*.db")), "the pages read before it are not kept as a table"
    assert len(waits) == pes.RATE_LIMIT_RETRIES
    assert len(server.requests) < 20, "it stops asking"


def test_a_page_that_times_out_is_read_in_smaller_pages(serve, monkeypatch):
    _page(monkeypatch, 40)
    client, server = serve(_invoices(130), slow_above=12)
    _names, rows = _collect(client, "INVOICES")
    assert _ids(rows) == [f"IN{i:06d}" for i in range(1, 131)]
    assert server.pages[0][1] == 40 and server.pages[-1][1] <= 12


def test_a_page_that_times_out_at_the_smallest_size_fails(serve, monkeypatch):
    _page(monkeypatch, 40)
    client, _server = serve(_invoices(130), slow_above=0)
    with pytest.raises(RuntimeError, match="in time"):
        _collect(client, "INVOICES")


def test_the_request_limit_is_the_organizations(serve, monkeypatch):
    _page(monkeypatch, 10)
    client, server = serve(_invoices(400))
    client.extraction_limits = fast_limits.FastLimits(max_requests=16)
    est = extractor.estimate(client, "INVOICES")
    assert est.rows == 400 and est.source_max_rows < 400, "said when saving, from the count"
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est)
    server.requests.clear()
    with pytest.raises(RuntimeError, match="16 requests"):
        _collect(client, "INVOICES")
    assert len(server.requests) == 16

    client.extraction_limits = fast_limits.FastLimits(max_requests=60)
    extractor.check_budget(extractor.estimate(client, "INVOICES"))
    assert len(_collect(client, "INVOICES")[1]) == 400


@pytest.mark.parametrize("query, fits", [
    ("INVOICES?$orderby=IVNUM&$top=150", True),     # the cap ends the read
    ("INVOICES?$orderby=IVNUM&$top=151", False),    # the form runs out first
    ("INVOICES?$orderby=IVNUM", False),
])
def test_a_read_its_own_top_ends_needs_no_closing_request(serve, monkeypatch, query, fits):
    """16 requests are one count and 15 pages of 10. 150 rows fit exactly
    when the definition's $top is what ends the read; when the form runs out
    instead, the empty page that proves it is one request too many. The
    estimate must say what the extraction then does."""
    _page(monkeypatch, 10)
    client, _server = serve(_invoices(150))
    client.extraction_limits = fast_limits.FastLimits(max_requests=16)
    est = extractor.estimate(client, query)
    assert est.rows == 150
    if fits:
        extractor.check_budget(est)
        assert len(_collect(client, query)[1]) == 150
    else:
        with pytest.raises(extractor.ExtractionRefused):
            extractor.check_budget(est)
        with pytest.raises(RuntimeError, match="16 requests"):
            _collect(client, query)


def test_retries_count_against_the_request_limit(serve, monkeypatch, waits):
    _page(monkeypatch, 10)
    client, server = serve(_invoices(60), throttle={2, 3, 5, 6, 8, 9})
    client.extraction_limits = fast_limits.FastLimits(max_requests=8)   # enough for 60 rows unthrottled
    with pytest.raises(RuntimeError, match="8 requests"):
        _collect(client, "INVOICES")
    assert len(server.requests) == 8


def test_stopping_partway_asks_for_no_further_page(serve, monkeypatch):
    _page(monkeypatch, 10)
    client, server = serve(_invoices(100))
    batches = source_for(client).stream_batches("INVOICES", 10)
    next(batches)
    batches.close()
    assert len(server.pages) == 1 and server.closed == 1


def test_a_cancelled_refresh_stops_between_pages_and_stores_nothing(serve, monkeypatch, tmp_path):
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    _page(monkeypatch, 10)
    client, server = serve(_invoices(100))
    with pytest.raises(extractor.ExtractionAborted):
        extractor.extract_to_artifact(client, "INVOICES", "invoices", "conn-1",
                                      should_abort=lambda: len(server.pages) >= 3)
    assert len(server.pages) < 10, "the rest of the form was not read"
    assert not list(tmp_path.rglob("*.db"))


# --------------------------------------------------------------------------
# Refresh windows
# --------------------------------------------------------------------------

def _window(lo=None, hi=None, nulls=False):
    return incremental.Window(lo=lo, hi=hi, include_nulls=nulls)


def test_a_refresh_window_is_filtered_at_the_source(serve, monkeypatch):
    """Only the window's rows are requested: fewer rows, and fewer requests."""
    _page(monkeypatch, 10)
    data = _invoices(84)
    client, server = serve(data)
    query = "INVOICES?$filter=CUSTNAME eq '1011'&$select=IVNUM,UDATE"
    assert incremental.filters_at_source(client, query)
    lo, hi = datetime(2026, 2, 10, tzinfo=UTC), datetime(2026, 3, 5, tzinfo=UTC)
    rendered = incremental.render(client, query, "UDATE", incremental.KIND_TIMESTAMP, _window(lo, hi))
    names, rows = _collect(client, rendered)
    expected = [r["IVNUM"] for r in data if r["CUSTNAME"] == "1011"
                and "2026-02-10T00:00:00Z" <= r["UDATE"] < "2026-03-05T00:00:00Z"]
    assert expected and _ids(rows) == expected
    assert names == ["IVNUM", "UDATE"]
    assert extractor.estimate(client, rendered).rows == len(expected)
    assert len(server.pages) <= 4


def test_rows_with_no_cursor_value_ride_along_with_an_open_window(serve):
    data = _invoices(40, null_dates={3, 17})
    client, _server = serve(data)
    lo = datetime(2026, 2, 1, tzinfo=UTC)
    rendered = incremental.render(client, "INVOICES", "UDATE", None, _window(lo, nulls=True))
    got = set(_ids(_collect(client, rendered)[1]))
    assert got == {r["IVNUM"] for r in data if r["UDATE"] is None or r["UDATE"] >= "2026-02-01T00:00:00Z"}
    assert {"IN000004", "IN000018"} <= got


def _refresh(client, query, base, config, window, tmp_rows=None):
    """One incremental refresh of `base`, as the service runs it."""
    from app.data_sources.fast import loader

    at_source = incremental.filters_at_source(client, query)
    rendered = incremental.render(client, query, config.cursor_column, None, window)
    limits = loader.Limits(max_rows=10**6, max_bytes=10**9, deadline=time.monotonic() + 600)
    return loader.apply_increment(
        source_for(client), rendered, base.artifact_path, base.artifact_key, "t", "conn-1",
        loader.new_run_id(), config, window, limits, batch_rows=1000,
        wrapped=at_source, filter_locally=not at_source)


def _stored(result, column):
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{result.artifact_path}' AS a (ENCRYPTION_KEY '{result.artifact_key}', READ_ONLY)")
    values = [r[0] for r in con.execute(f'SELECT "{column}" FROM a.main.t ORDER BY 1').fetchall()]
    con.close()
    return values


def test_refreshing_a_window_twice_neither_repeats_nor_loses_rows(serve, monkeypatch, tmp_path):
    """The bound sent to Priority is the bound the stored tail is replaced
    from, also when it falls between two rows of the same second."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    data = [{"IVNUM": f"IN{i:03d}", "UDATE": f"2026-03-01T12:00:00.{i:03d}000Z"} for i in range(0, 1000, 125)]
    client, _server = serve(data)
    config = incremental.RefreshConfig(mode=incremental.MODE_APPEND, cursor_column="UDATE")
    table = extractor.extract_to_artifact(client, "INVOICES", "t", "conn-1", config=config)
    window = _window(datetime(2026, 3, 1, 12, 0, 0, 750_000, tzinfo=UTC), nulls=True)
    for _ in range(2):
        table = _refresh(client, "INVOICES", table, config, window)
        assert _stored(table, "IVNUM") == [r["IVNUM"] for r in data]


@pytest.mark.parametrize("bounds", ["$skip=10", "$top=25", "$skip=5&$top=30"])
def test_a_definitions_own_bounds_apply_to_the_table_not_to_the_refresh_window(serve, monkeypatch, tmp_path, bounds):
    """$top / $skip come after $filter, so a window in the filter would skip
    or cap rows of the window. Such a table is read whole instead."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    _page(monkeypatch, 16)
    data = [{"ID": i, "NAME": f"row {i}"} for i in range(1, 61)]
    client, _server = serve(data)
    query = f"INVOICES?$orderby=ID&{bounds}"
    assert not incremental.filters_at_source(client, query)
    config = incremental.RefreshConfig(mode=incremental.MODE_APPEND, cursor_column="ID")
    table = extractor.extract_to_artifact(client, query, "t", "conn-1", config=config)
    defined = _stored(table, "ID")
    assert defined and len(defined) < 60

    table = _refresh(client, query, table, config, _window(defined[len(defined) // 2], nulls=True))
    assert _stored(table, "ID") == defined


@pytest.mark.parametrize("bounds", ["$top=100", "$skip=10"])
def test_placeholders_are_refused_in_a_definition_with_its_own_bounds(serve, bounds):
    query = f"INVOICES?$since={{{{since}}}}&{bounds}"
    with pytest.raises(HTTPException) as refused:
        CustomQueryService.ensure_definition_allowed(SimpleNamespace(type="priority_erp"), query)
    assert refused.value.status_code == 400
    client, server = serve(_invoices(3))
    with pytest.raises(incremental.IncrementalConfigError):
        incremental.render(client, query, "UDATE", incremental.KIND_TIMESTAMP,
                           _window(datetime(2026, 1, 1, tzinfo=UTC)))
    assert not server.requests


def test_since_is_used_by_placing_the_placeholder(serve):
    data = _invoices(60)
    client, server = serve(data)
    query = "INVOICES?$since={{since}}&$select=IVNUM,UDATE"
    lo = datetime(2026, 2, 20, 6, 0, tzinfo=UTC)
    rendered = incremental.render(client, query, "UDATE", incremental.KIND_TIMESTAMP, _window(lo))
    assert _ids(_collect(client, rendered)[1]) == [r["IVNUM"] for r in data if r["UDATE"] >= "2026-02-20T06:00:00Z"]
    assert {opts.get("$since") for _path, opts in server.requests} == {"2026-02-20T06:00:00Z"}

    # A full load opens the window: every row, and still a value the source accepts.
    everything = incremental.render(client, query, "UDATE", incremental.KIND_TIMESTAMP, _window())
    assert "{{" not in everything and len(_collect(client, everything)[1]) == 60


def test_a_window_cannot_be_written_onto_a_definition_or_cursor_that_is_not_safe(serve):
    client, _server = serve(_invoices(3))
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(incremental.IncrementalConfigError):
        incremental.render(client, "INVOICES", "UDATE ge 1 or 1", None, _window(lo))
    with pytest.raises(incremental.IncrementalConfigError):
        incremental.render(client, "INVOICES?$since={{since}}", "IVNUM", incremental.KIND_TEXT, _window("x' or '1"))


# --------------------------------------------------------------------------
# Over real HTTP, against the repo's mock Priority service
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_priority():
    import sys
    from pathlib import Path

    import uvicorn

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "priority"))
    import mock_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mock_server.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("mock Priority server did not start")
    mock_server._calls.clear()
    yield mock_server, f"http://127.0.0.1:{port}{mock_server.ROOT}"
    server.should_exit = True
    thread.join(timeout=10)


def test_the_requests_survive_a_real_http_round_trip(mock_priority, monkeypatch):
    """The mock does not answer $count (Priority does not document it), so
    this is also the no-count path end to end: paged rows equal one request's."""
    mock_server, root = mock_priority
    _page(monkeypatch, 2)
    client = PriorityErpClient(service_root=root, pat=mock_server.VALID_PAT)
    whole = client.execute_query("ORDERS?$select=ORDNAME,CUSTNAME&$orderby=ORDNAME")
    assert len(whole) > 2, "the mock must hold more than one page for this to mean anything"

    query = "ORDERS?$select=ORDNAME,CUSTNAME&$orderby=ORDNAME"
    names, rows = _collect(client, query)
    assert names == ["ORDNAME", "CUSTNAME"]
    assert [list(r) for r in rows] == whole.values.tolist()
    assert not extractor.estimate(client, query).supported

    customer = whole["CUSTNAME"].iloc[0]
    filtered = f"ORDERS?$filter=CUSTNAME eq '{customer}'&$select=ORDNAME,CUSTNAME&$orderby=ORDNAME"
    assert _ids(_collect(client, filtered)[1]) == whole[whole["CUSTNAME"] == customer]["ORDNAME"].tolist()
    with pytest.raises(ValueError):
        _collect(client, "ORDERS?$expand=ORDERITEMS_SUBFORM")
