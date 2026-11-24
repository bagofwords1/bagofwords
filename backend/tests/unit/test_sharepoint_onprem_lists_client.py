"""SharePoint Server lists connector contract; only the remote HTTP boundary is simulated.

The simulated server (`tests/fixtures/fake_sharepoint_lists.py`) behaves like
SharePoint Server where it matters: the List View Threshold refuses filters on
large lists, the lookup threshold refuses wide joins, `$top`/`$skiptoken`
paging, verbose and nometadata JSON, EntityPropertyName keys, taxonomy labels.
"""
import json
import math
from unittest.mock import MagicMock

import pytest
import requests

from app.data_sources.clients import sharepoint_onprem_lists_client as module
from app.data_sources.clients.sharepoint_onprem_lists_client import SharepointOnpremListsClient
from app.schemas.data_source_registry import REGISTRY, resolve_client_class
from tests.fixtures.fake_sharepoint_lists import build_world

SITE = "https://sharepoint.corp.example/sites/ops"


@pytest.fixture
def sp_lists(monkeypatch):
    """The simulated server; every GET from a client session is routed into it."""
    world = build_world()
    world.accept = "application/json;odata=verbose"
    world.raw_status = None  # (status, headers) to answer the next request with

    def respond(session, url, params=None, **kw):
        assert kw["allow_redirects"] is False
        assert session.trust_env is False
        username = getattr(session.auth, "username", None) or "svc-sharepoint"
        principal = username.split("\\")[-1].lower()
        response = MagicMock()
        if world.raw_status:
            status, headers = world.raw_status.pop(0)
            response.status_code, response.headers = status, headers
            response.iter_content.return_value = [b""]
        else:
            status, body = world.handle("GET", url, params, accept=world.accept, principal=principal)
            response.status_code = status
            response.headers = {"Content-Length": str(len(body))}
            response.iter_content.return_value = [body]
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr(requests.Session, "get", respond)
    world.raw_status = []
    return world


def client(**kwargs):
    kwargs.setdefault("username", "CORP\\svc-sharepoint")
    kwargs.setdefault("password", "unit-only")
    c = SharepointOnpremListsClient(site_url=SITE, **kwargs)
    c._sleep = lambda seconds: None
    return c


def query(c, **spec):
    return c.execute_query(json.dumps(spec))


def item_requests(world):
    return [r for r in world.requests if r["path"].endswith("/items")]


def truth(world, title, keep):
    """Expected rows computed straight from the simulated list (folders excluded)."""
    lst = next(l for l in world.lists if l.title == title)
    return [it for it in lst.items if it.get("FileSystemObjectType", 0) != 1 and keep(it)]


# ------------------------------------------------------------------ registry

def test_registered_as_a_tables_connector_with_windows_auth():
    entry = REGISTRY["sharepoint_onprem_lists"]
    assert entry.data_shape == "tables"
    assert entry.credentials_auth.default == "ntlm"
    assert entry.credentials_auth.by_auth["ntlm"].scopes == ["system", "user"]
    assert entry.credentials_auth.by_auth["kerberos"].scopes == ["system"]
    assert resolve_client_class("sharepoint_onprem_lists") is SharepointOnpremListsClient


@pytest.mark.parametrize("url,kwargs", [
    ("http://sharepoint.corp.example/sites/ops", {}),                       # HTTP needs an explicit lab opt-in
    ("https://user:pw@sharepoint.corp.example/sites/ops", {}),
    ("https://sharepoint.corp.example/sites/ops?x=1", {}),
    ("https://sharepoint.corp.example/sites/ops/_api/web", {}),
    ("https://sharepoint.corp.example/sites/../admin", {}),
    ("ftp://sharepoint.corp.example/sites/ops", {}),
    (SITE, {"max_items": 0}),
    (SITE, {"max_items": 1_000_000}),
])
def test_rejects_unsafe_or_invalid_configuration(url, kwargs):
    with pytest.raises(ValueError):
        SharepointOnpremListsClient(site_url=url, username="u", password="p", **kwargs)


def test_ntlm_requires_credentials():
    with pytest.raises(ValueError):
        SharepointOnpremListsClient(site_url=SITE)


# ------------------------------------------------------------------- catalog

def test_catalog_is_the_sites_visible_generic_lists(sp_lists):
    names = {t.name for t in client().get_schemas()}
    # Hidden plumbing, workflow history, document libraries and lists rooted in
    # another site are not tables of this connection.
    assert names == {"Expenses", "Tickets", "Projects", "Rota", "Salaries"}


def test_hidden_lists_are_opt_in(sp_lists):
    assert "TaxonomyHiddenList" in {t.name for t in client(include_hidden=True).get_schemas()}


@pytest.mark.parametrize("scope", ["Expenses, tickets", " expenses ,TICKETS "])
def test_list_scope_by_name(sp_lists, scope):
    assert {t.name for t in client(lists=scope).get_schemas()} == {"Expenses", "Tickets"}


def test_list_scope_by_id(sp_lists):
    expenses = next(l for l in sp_lists.lists if l.title == "Expenses")
    assert [t.name for t in client(lists="{" + expenses.id.upper() + "}").get_schemas()] == ["Expenses"]


def test_columns_use_display_names_and_typed_dtypes(sp_lists):
    table = next(t for t in client().get_schemas() if t.name == "Expenses")
    dtypes = {c.name: c.dtype for c in table.columns}
    assert dtypes["Amount"] == "float"            # internal name field_2
    assert dtypes["2017"] == "float"              # internal _x0032_017, REST key OData__x0032_017
    assert dtypes["Submitted"] == "datetime"
    assert dtypes["Approved"] == "boolean"
    assert dtypes["ID"] == "integer"
    assert dtypes["Total incl. VAT"] == "float"   # calculated currency
    for name in ("Approver", "Reviewers", "Project", "Region", "Tags", "Receipt", "Created By"):
        assert dtypes[name] == "string"
    # System plumbing never reaches the catalog.
    assert not {"Content Type", "Attachments", "Edit", "Version", "Approval Status", "Name",
                "App Created By", "Region_0", "owshiddenversion"} & set(dtypes)
    meta = table.metadata_json["sharepoint_onprem_list"]
    assert meta["internal_names"]["Amount"] == "field_2"
    assert meta["item_count"] == 7


def test_catalog_is_permission_filtered_per_identity(sp_lists):
    assert "Salaries" in {t.name for t in client(username="CORP\\dana").get_schemas()}
    assert "Salaries" not in {t.name for t in client(username="CORP\\noa").get_schemas()}


def test_test_connection_reports_identity_and_lists_without_reading_items(sp_lists):
    result = client().test_connection()
    assert result["success"] is True
    assert result["details"]["list_count"] == 5
    assert not item_requests(sp_lists)


def test_test_connection_failure_does_not_leak_the_server_message(sp_lists):
    sp_lists.fail_next.append((401, {"error": {"code": "x", "message": {"value": "secret-internal-host-dc01"}}}))
    result = client().test_connection()
    assert result["success"] is False
    assert "secret-internal-host-dc01" not in result["message"]


# -------------------------------------------------------------------- values

def test_values_are_normalized(sp_lists):
    df = query(client(), list="Expenses")
    rows = {r["Title"]: r for r in df.to_dict("records")}
    # The folder inside the list is not a row.
    assert "Archive" not in rows
    assert len(df) == len(truth(sp_lists, "Expenses", lambda it: True))
    hotel = rows["Hotel Berlin"]
    assert hotel["Amount"] == 610.5 and hotel["2017"] == 3.0
    assert hotel["Approver"] == "Dana Levi"                     # person → name
    assert hotel["Reviewers"] == "Noa Cohen; Avi Mizrahi"       # multi-person, incl. a user without email
    assert hotel["Project"] == "Data Center Move"               # lookup → target title
    assert hotel["Tags"] == "Q1"
    assert hotel["Region"] == "North"                           # taxonomy label, not the WssId
    assert hotel["Receipt"] == "https://receipts.corp.example/2"
    assert hotel["Total incl. VAT"] == pytest.approx(610.5 * 1.17, rel=1e-6)
    assert hotel["Submitted"] == "2026-03-03T08:00:00Z"         # UTC ISO string
    assert hotel["Notes"] == "Receipt for Hotel Berlin\nPaid & filed"   # rich text → text
    assert hotel["Approved"] is True or hotel["Approved"] == True  # noqa: E712
    lunch = rows["Team lunch"]
    assert lunch["Approver"] is None and lunch["Project"] is None and lunch["Reviewers"] is None
    assert math.isnan(lunch["2017"]) if isinstance(lunch["2017"], float) else lunch["2017"] is None
    assert df.columns[0] == "id"
    assert not df.attrs.get("truncated") and "bow_notice" not in df.attrs


def test_select_returns_exactly_the_requested_columns_in_order(sp_lists):
    df = query(client(), list="Expenses", select=["Amount", "title", "2017"])
    assert list(df.columns) == ["id", "Amount", "Title", "2017"]


def test_unknown_select_column_names_the_available_ones(sp_lists):
    with pytest.raises(ValueError) as e:
        query(client(), list="Expenses", select=["Amount", "Nope"])
    assert "Nope" in str(e.value) and "Amount" in str(e.value)


def test_unknown_list_names_the_available_ones(sp_lists):
    with pytest.raises(ValueError) as e:
        query(client(), list="Invoices")
    assert "Expenses" in str(e.value)


def test_nometadata_and_verbose_responses_read_the_same(sp_lists):
    verbose = query(client(), list="Expenses")
    sp_lists.accept = "application/json;odata=nometadata"
    sp_lists.requests.clear()
    plain = query(client(), list="Expenses")
    pd_testing = pytest.importorskip("pandas.testing")
    pd_testing.assert_frame_equal(verbose, plain)


# ------------------------------------------------- filtering past the threshold

FILTERS = [
    ("Status eq 'open'", lambda it: it["Status"] == "Open"),
    ("Priority ge 4 and Status ne 'Closed'", lambda it: it["Priority"] >= 4 and it["Status"] != "Closed"),
    ("contains(Title,'vpn')", lambda it: "vpn" in it["Title"].lower()),
    ("Opened ge '2026-11-01T00:00:00Z'", lambda it: it["Opened"] >= "2026-11-01T00:00:00Z"),
    ("Assigned To eq 'noa cohen' and ID gt 11000", lambda it: it["AssignedTo"] == 8 and it["ID"] > 11000),
]


@pytest.mark.parametrize("expr,keep", FILTERS)
def test_filters_on_a_list_over_the_threshold_match_the_full_list(sp_lists, expr, keep):
    """Any column filters correctly on a 12,000-item list (threshold 5,000)."""
    expected = truth(sp_lists, "Tickets", keep)
    df = query(client(), list="Tickets", filter=expr, limit=20000)
    assert sorted(df["id"]) == sorted(it["ID"] for it in expected)
    assert "bow_notice" not in df.attrs
    # The server never saw a filter or a sort.
    for r in item_requests(sp_lists):
        assert "$filter" not in r["params"] and "$orderby" not in r["params"]


def test_orderby_and_limit_apply_after_the_filter(sp_lists):
    expected = sorted(truth(sp_lists, "Tickets", lambda it: it["Status"] == "Open"),
                      key=lambda it: (-it["Priority"], it["ID"]))[:25]
    df = query(client(), list="Tickets", filter="Status eq 'Open'", orderby="Priority desc", limit=25)
    assert list(df["id"]) == [it["ID"] for it in expected]
    assert df.attrs["truncated"] is True and "limit" in df.attrs["bow_notice"]


def test_paging_walks_ids_with_skiptokens_on_the_site(sp_lists):
    c = client()
    c.page_size = 1000
    query(c, list="Tickets", filter="Status eq 'Open'")
    reqs = item_requests(sp_lists)
    assert len(reqs) == math.ceil(12000 / 1000)
    last = 0
    for r in reqs:
        assert r["path"].startswith("/sites/ops/_api/web/lists(guid'")
        token = r["params"].get("$skiptoken", "")
        start = int(token.split("p_ID=")[1]) if token else 0
        assert start == last
        last = start + 1000


def test_paging_reads_every_item_when_the_server_sends_no_next_links(sp_lists):
    sp_lists.link_pages = False
    df = query(client(), list="Tickets", filter="ID gt 0", limit=20000)
    assert len(df) == 12000 and "bow_notice" not in df.attrs


def test_an_unfiltered_query_stops_reading_at_its_limit(sp_lists):
    c = client()
    c.page_size = 100
    df = query(c, list="Tickets", limit=150)
    assert list(df["id"]) == list(range(1, 151))
    assert len(item_requests(sp_lists)) == 2
    assert df.attrs["truncated"] is True and "12,000" in df.attrs["bow_notice"]


def test_a_limit_equal_to_the_list_size_is_not_reported_as_cut(sp_lists):
    df = query(client(), list="Projects", limit=2)
    assert len(df) == 2 and "bow_notice" not in df.attrs and not df.attrs["truncated"]


def test_a_filtered_scan_that_hits_max_items_says_what_it_covered(sp_lists):
    c = client(max_items=3000)
    df = query(c, list="Tickets", filter="Status eq 'Open'")
    expected = truth(sp_lists, "Tickets", lambda it: it["Status"] == "Open" and it["ID"] <= 3000)
    assert sorted(df["id"]) == [it["ID"] for it in expected]
    assert df.attrs["truncated"] is True
    assert "3,000" in df.attrs["bow_notice"] and "12,000" in df.attrs["bow_notice"]


def test_filter_errors_name_the_columns(sp_lists):
    with pytest.raises(ValueError) as e:
        query(client(), list="Tickets", filter="Severity eq 'High'")
    assert "Severity" in str(e.value) and "Priority" in str(e.value)


def test_a_threshold_refusal_while_paging_is_explained(sp_lists):
    sp_lists.fail_next.append((500, {"error": {"code": "-2147024860, Microsoft.SharePoint.SPQueryThrottledException",
                                               "message": {"value": "exceeds the list view threshold"}}}))
    with pytest.raises(ValueError) as e:
        query(client(), list="Projects")
    assert "List View Threshold" in str(e.value)


# ------------------------------------------------------- person/lookup columns

def test_wide_person_columns_stay_under_the_lookup_threshold(sp_lists):
    df = query(client(), list="Rota")
    row = df.to_dict("records")[0]
    named = [k for k in row if k.startswith("Person") and isinstance(row[k], str)]
    as_ids = [k for k in row if k.startswith("Person") and not isinstance(row[k], str)]
    assert named and as_ids and len(named) + len(as_ids) == 14
    assert "lookup" in df.attrs["bow_notice"] and all(c in df.attrs["bow_notice"] for c in as_ids)


def test_a_lower_server_lookup_threshold_falls_back_to_ids(sp_lists):
    sp_lists.lookup_threshold = 1
    df = query(client(), list="Expenses", select=["Title", "Approver", "Project"])
    rows = {r["Title"]: r for r in df.to_dict("records")}
    assert rows["Hotel Berlin"]["Approver"] == 7 and rows["Hotel Berlin"]["Project"] == 1
    assert "Approver" in df.attrs["bow_notice"]


def test_a_query_naming_few_people_gets_names(sp_lists):
    df = query(client(), list="Rota", select=["Title", "Person 13", "Person 14"])
    assert df.to_dict("records")[0]["Person 14"] == "Noa Cohen"
    assert "bow_notice" not in df.attrs


# ------------------------------------------------------------ transport safety

def test_throttling_is_retried_after_the_servers_delay(sp_lists):
    waits = []
    c = client()
    c._sleep = waits.append
    sp_lists.raw_status.extend([(503, {"Retry-After": "3"}), (429, {"Retry-After": "120"})])
    assert len(query(c, list="Projects")) == 2
    assert waits == [3.0, module.THROTTLE_MAX_WAIT_SECONDS]


def test_redirects_are_refused(sp_lists):
    sp_lists.raw_status.append((302, {"Location": "https://evil.example/"}))
    with pytest.raises(ValueError) as e:
        query(client(), list="Projects")
    assert "redirect" in str(e.value).lower()


def test_an_oversized_page_is_retried_smaller(sp_lists, monkeypatch):
    monkeypatch.setattr(module, "RESPONSE_BYTE_LIMIT", 60_000)
    c = client()
    df = query(c, list="Tickets", filter="ID le 600")
    assert sorted(df["id"]) == list(range(1, 601))
    sizes = [int(r["params"]["$top"]) for r in item_requests(sp_lists)]
    assert sizes[0] == module.PAGE_SIZE and min(sizes) < module.PAGE_SIZE


@pytest.mark.parametrize("status", [401, 403, 404])
def test_http_errors_are_mapped_without_provider_text(sp_lists, status):
    sp_lists.fail_next.append((status, {"error": {"code": "x", "message": {"value": "C:\\inetpub\\secret.config"}}}))
    with pytest.raises(ValueError) as e:
        query(client(), list="Projects")
    assert "secret.config" not in str(e.value) and str(status) in str(e.value)
