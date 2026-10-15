"""Priority ERP connector, driven end-to-end against the mock OData service.

Priority is licensed software with no public sandbox, so `tools/priority/mock_server.py`
stands in at the protocol level and is served here over real HTTP in a background
thread. That keeps the test hermetic while still exercising the parts that only
break over the wire: auth header shape, EDMX parsing, query-option round-trips,
and error classification.

The mock is deliberately faithful to the details that shape the client — titles
live in annotations rather than on `<Property>`, PAT auth puts the token in the
username position, `$apply` is unsupported, and one form carries Hebrew titles.
"""
import socket
import threading
import time

import pytest
import uvicorn

from app.data_sources.clients.priority_erp_client import (
    PriorityErpClient,
    _RateLimiter,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_module():
    """The mock Priority service module (its schema and request log)."""
    import sys
    from pathlib import Path
    tools = Path(__file__).resolve().parents[3] / "tools" / "priority"
    sys.path.insert(0, str(tools))
    import mock_server  # noqa: E402
    return mock_server


@pytest.fixture(scope="module")
def priority_root(mock_module):
    """Serve the mock Priority OData service and yield its service root."""
    mock_server = mock_module

    port = _free_port()
    config = uvicorn.Config(mock_server.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("mock Priority server did not start")

    yield f"http://127.0.0.1:{port}{mock_server.ROOT}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _fresh_rate_window(mock_module):
    """The mock enforces Priority Cloud's 100 calls/minute across the whole
    module-scoped server; each test starts with an empty window so the suite's
    own volume never trips it."""
    mock_module._calls.clear()


@pytest.fixture
def client(priority_root):
    return PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")


# ── connection + auth ──────────────────────────────────────────────────────

def test_connect_with_pat(client):
    result = client.test_connection()
    assert result["success"] is True, result
    assert "form(s)" in result["message"]


def test_connection_counts_only_top_level_forms(client, mock_module):
    """Subforms have an EntityType but are not forms you can open; the count
    the admin sees must be the addressable (EntitySet) forms only."""
    result = client.test_connection()
    assert f"{len(mock_module.ROOT_FORMS)} form(s)" in result["message"]


def test_pat_uses_token_as_username_with_literal_pat_password(priority_root):
    """Priority's documented shape: Basic base64("<PAT>:PAT").

    The mock rejects any other arrangement, so a green test here is real
    evidence the header is built the way Priority expects.
    """
    import base64
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")
    header = c._auth_headers()["Authorization"]
    assert header.startswith("Basic ")
    assert base64.b64decode(header[6:]).decode() == "DEMOTOKEN:PAT"


def test_connect_with_basic_api_user(priority_root):
    c = PriorityErpClient(service_root=priority_root, username="demo", password="demo")
    assert c.test_connection()["success"] is True


def test_bearer_token_takes_precedence(priority_root):
    """Per-user OAuth (on-prem) must win over the service credential, so queries
    run as the signed-in user rather than the indexing account."""
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", access_token="user-token")
    assert c._auth_headers() == {"Authorization": "Bearer user-token"}
    assert c.test_connection()["success"] is True


def test_bad_pat_is_rejected_with_a_useful_message(priority_root):
    result = PriorityErpClient(service_root=priority_root, pat="WRONG").test_connection()
    assert result["success"] is False
    assert "authentication failed" in result["message"].lower()
    assert "API licence" in result["message"] or "api licence" in result["message"].lower()


def test_no_credentials_is_reported_not_raised(priority_root):
    result = PriorityErpClient(service_root=priority_root).test_connection()
    assert result["success"] is False
    assert "no credentials" in result["message"].lower()


def test_non_priority_url_is_rejected_before_any_request():
    result = PriorityErpClient(service_root="https://example.com/api", pat="x").test_connection()
    assert result["success"] is False
    assert "odata" in result["message"].lower()


# ── schema discovery ───────────────────────────────────────────────────────

def test_column_titles_come_from_annotations(client):
    """The whole value of the catalog.

    Priority's `<Property>` elements carry no labels — titles live in
    `Priority.OData.Description` annotations. Parsing only properties yields
    bare names like CUSTNAME, which is useless to the agent.
    """
    orders = next(t for t in client.get_schemas() if t.name == "ORDERS")
    titles = {c.name: c.description for c in orders.columns}
    assert titles["ORDNAME"] == "Order Number"
    assert titles["CUSTNAME"] == "Customer Number"
    assert titles["TOTPRICE"] == "Total Price"
    assert orders.description == "Customer Orders"


def test_non_english_titles_survive_intact(client):
    """A real Israeli tenant returns Hebrew titles; nothing may assume ASCII."""
    part = next(t for t in client.get_schemas() if t.name == "PART")
    assert part.description == "פריטים"
    assert {c.name: c.description for c in part.columns}["PARTDES"] == "תיאור פריט"


def test_mandatory_annotation_is_captured(client):
    orders = next(t for t in client.get_schemas() if t.name == "ORDERS")
    mandatory = {c.name for c in orders.columns if (c.metadata or {}).get("mandatory")}
    assert mandatory == {"ORDNAME", "CUSTNAME"}
    assert orders.metadata_json["priority_erp"]["mandatory"] == ["ORDNAME", "CUSTNAME"]


def test_keys_and_composite_keys(priority_root):
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", discover_all=True)
    tables = {t.name: t for t in c.get_schemas()}
    assert [p.name for p in tables["ORDERS"].pks] == ["ORDNAME"]
    # Priority uses composite keys widely; $select must include all of them.
    assert [p.name for p in tables["WARHSBAL"].pks] == ["PARTNAME", "WARHSNAME"]


def test_subforms_become_join_paths(client):
    """Navigation properties are subforms — surfaced as fks so the agent can
    see the join without needing a connector-specific metadata block."""
    orders = next(t for t in client.get_schemas() if t.name == "ORDERS")
    assert ("ORDNAME", "ORDERITEMS") in [(f.column.name, f.references_name) for f in orders.fks]
    assert "ORDERITEMS_SUBFORM" in orders.metadata_json["priority_erp"]["subforms"]


def test_company_is_derived_from_service_root(client):
    orders = next(t for t in client.get_schemas() if t.name == "ORDERS")
    assert orders.metadata_json["priority_erp"]["company"] == "demo"


# ── form selection ─────────────────────────────────────────────────────────

def test_curated_set_is_the_default(client):
    """A tenant can carry thousands of forms and a fresh connection has no
    usage data to rank them, so the default is a curated set."""
    names = [t.name for t in client.get_schemas()]
    assert "ORDERS" in names and "CUSTOMERS" in names
    assert "ACME_PROJECTS" not in names  # customer-specific, not curated


def test_discover_all_includes_customer_specific_forms(priority_root):
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", discover_all=True)
    assert "ACME_PROJECTS" in [t.name for t in c.get_schemas()]


def test_explicit_form_list_is_case_insensitive(priority_root):
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", forms="orders, customers")
    assert sorted(t.name for t in c.get_schemas()) == ["CUSTOMERS", "ORDERS"]


def test_get_schema_falls_back_to_single_entity_metadata(client):
    """Forms outside the selected set resolve via GetMetadataFor (v25.0+)
    rather than regenerating the whole tenant schema."""
    t = client.get_schema("ACME_PROJECTS")
    assert t.name == "ACME_PROJECTS"
    assert {c.name for c in t.columns} == {"PROJCODE", "PROJDES"}


def test_get_schema_unknown_form_raises(client):
    # GetMetadataFor 404s for a form that doesn't exist, which _get turns into
    # a RuntimeError naming the likely cause.
    with pytest.raises(RuntimeError, match="404"):
        client.get_schema("NO_SUCH_FORM_AT_ALL")


# ── querying ───────────────────────────────────────────────────────────────

def test_query_plain_form(client):
    df = client.execute_query("ORDERS")
    assert len(df) == 4
    assert "ORDNAME" in df.columns


def test_query_with_odata_options(client):
    df = client.execute_query(
        "ORDERS?$filter=CUSTNAME eq '1011'&$select=ORDNAME,TOTPRICE&$orderby=TOTPRICE desc"
    )
    assert list(df["ORDNAME"]) == ["SO25001", "SO25003"]
    assert list(df.columns) == ["ORDNAME", "TOTPRICE"]


def test_max_rows_is_applied(client):
    assert len(client.execute_query("ORDERS", max_rows=2)) == 2


def test_empty_query_is_rejected(client):
    with pytest.raises(ValueError):
        client.execute_query("")


def test_apply_aggregation_gives_an_actionable_error(client):
    """Priority documents no $apply. The agent reaches for aggregation
    naturally, so the error must name the limitation and the workaround —
    not surface a bare 501."""
    with pytest.raises(RuntimeError) as exc:
        client.execute_query("ORDERS?$apply=groupby((CUSTNAME))")
    msg = str(exc.value)
    assert "$apply" in msg and "aggregate in code" in msg


def test_unknown_form_query_names_the_likely_cause(client):
    with pytest.raises(RuntimeError) as exc:
        client.execute_query("NOSUCHFORM")
    assert "404" in str(exc.value)


@pytest.mark.parametrize("bad_column", ["NOTACOLUMN", "QPROFIT_X"])
def test_rejected_query_surfaces_priority_error_message(client, bad_column):
    """A 400 from Priority carries its reason in the body; the raised error
    must include it so the agent can fix the query instead of guessing."""
    with pytest.raises(RuntimeError) as exc:
        client.execute_query(f"ORDERS?$select=ORDNAME,{bad_column}&$top=1")
    msg = str(exc.value)
    assert "400" in msg and bad_column in msg


# ── rate limiting ──────────────────────────────────────────────────────────

def test_rate_limiter_disabled_when_non_positive():
    limiter = _RateLimiter(0)
    start = time.monotonic()
    for _ in range(50):
        limiter.acquire()
    assert time.monotonic() - start < 0.5


def test_rate_limiter_allows_burst_up_to_the_cap():
    limiter = _RateLimiter(5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - start < 0.5
    assert len(limiter._times) == 5


def test_rate_limiter_would_block_past_the_cap():
    """Verified without actually sleeping 60s: the bucket is full, so the next
    acquire must be the one that waits."""
    limiter = _RateLimiter(2)
    limiter.acquire()
    limiter.acquire()
    assert len(limiter._times) == 2
    now = time.monotonic()
    assert all(now - t < 60.0 for t in limiter._times)


# ── agent-facing context ───────────────────────────────────────────────────

def test_indexed_catalog_reaches_agent_context_with_titles(client):
    """End-to-end proof of the connector's point: cryptic Priority column names
    arrive in the agent's context carrying their business titles."""
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    ctx = TablesSchemaContext(data_sources=[
        TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Priority", type="priority_erp"),
            tables=client.get_schemas(),
        )
    ])
    out = ctx.render_combined(top_k_per_ds=2, index_limit=200)
    assert '<table name="ORDERS"' in out
    assert 'description="Customer Orders"' in out
    assert 'name="CUSTNAME" dtype="Edm.String" description="Customer Number"' in out
    # the subform join path is visible without a connector-specific block
    assert 'ref_table="ORDERITEMS"' in out


def test_prompt_schema_renders(client):
    text = client.prompt_schema()
    assert "table: ORDERS" in text
    assert "Customer Orders" in text


def test_system_prompt_states_the_aggregation_limitation(client):
    """The agent needs to be told up front, or it will write $apply and fail."""
    prompt = client.system_prompt()
    assert "$apply" in prompt
    assert "$filter" in prompt and "$expand" in prompt


def test_description_names_the_company(client):
    assert "demo" in client.description


def test_description_carries_the_query_guide(client):
    """`description` is the only channel into the codegen prompt.

    The guide lived in `system_prompt()` but was never concatenated in, so the
    coder saw no OData guidance and fell back to the prompt's SQL defaults.
    """
    text = client.description
    assert "demo" in text
    assert "NOT a SQL data source" in text
    assert "$select" in text and "$top" in text
    assert "$apply" in text


def test_system_prompt_pushes_select_and_top(client):
    """A bare form name pulls the whole form before pandas ever filters."""
    prompt = client.system_prompt()
    assert "$select" in prompt and "$top" in prompt
    assert "query_params" in prompt


@pytest.mark.parametrize("sql", [
    "SELECT TOP 10 CUSTNAME, EMAIL FROM CUSTOMERS",
    "  select * from ORDERS",
    "WITH x AS (SELECT 1) SELECT * FROM x",
])
def test_execute_query_rejects_sql_with_an_actionable_message(client, sql):
    """A SELECT would otherwise be URL-encoded into the path and 400."""
    with pytest.raises(ValueError) as exc:
        client.execute_query(sql)
    msg = str(exc.value)
    assert "OData, not SQL" in msg
    assert "$select" in msg and "$filter" in msg


def test_execute_query_still_accepts_odata(client):
    """The guard must not catch legitimate form names or query options."""
    df = client.execute_query("CUSTOMERS?$select=CUSTNAME&$top=2")
    assert "CUSTNAME" in df.columns


# ── subforms ───────────────────────────────────────────────────────────────
# A Priority subform (ORDERITEMS under ORDERS) has an EntityType but no
# EntitySet: `GET {root}/ORDERITEMS` is a 404 on a real tenant, and its rows
# exist only nested inside the parent. The connector must present a subform
# as an ordinary table — one row per line, carrying its parents' columns — and
# do the parent `$expand` itself.

def _rows(df, *cols):
    return sorted(tuple(r) for r in df[list(cols)].itertuples(index=False))


def _no_nested_values(df):
    return not any(isinstance(v, (list, dict)) for col in df.columns for v in df[col])


def _nested_client(priority_root):
    return PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN",
                             forms="ORDERS,ORDERITEMS,ORDERITEMSTEXT")


def test_subform_is_marked_with_its_access_path(client):
    tables = {t.name: t for t in client.get_schemas()}
    meta = tables["ORDERITEMS"].metadata_json["priority_erp"]
    assert meta["subform_of"] == "ORDERS"
    assert meta["navigation"] == "ORDERITEMS_SUBFORM"
    # a top-level form is addressable as-is and carries no subform marker
    assert "subform_of" not in tables["ORDERS"].metadata_json["priority_erp"]


def test_nested_subform_records_the_full_path(priority_root):
    tables = {t.name: t for t in _nested_client(priority_root).get_schemas()}
    meta = tables["ORDERITEMSTEXT"].metadata_json["priority_erp"]
    assert meta["subform_of"] == "ORDERITEMS"
    assert [step["form"] for step in meta["path"]] == ["ORDERS", "ORDERITEMS", "ORDERITEMSTEXT"]


def test_subform_table_carries_its_ancestors_columns(client):
    items = next(t for t in client.get_schemas() if t.name == "ORDERITEMS")
    cols = {c.name: c for c in items.columns}
    assert {"KLINE", "PRICE", "STATDES"} <= set(cols)            # its own
    assert {"ORDNAME", "CURDATE", "CDES", "CUSTNAME"} <= set(cols)  # its parent's
    # both forms have STATDES: the line keeps the name, the parent's is prefixed
    assert cols["STATDES"].description == "Line Status"
    assert cols["ORDERS_STATDES"].description == "Order Stage (Customer Orders)"
    # a line is identified by its order plus its own key
    assert [p.name for p in items.pks] == ["ORDNAME", "KLINE"]


def test_nested_subform_gets_every_ancestor_level(priority_root):
    text = next(t for t in _nested_client(priority_root).get_schemas() if t.name == "ORDERITEMSTEXT")
    names = {c.name for c in text.columns}
    assert {"TEXT", "KLINE", "PRICE", "ORDNAME", "CURDATE"} <= names
    # the nearest ancestor keeps a shared name; the farther one is prefixed
    assert {"STATDES", "ORDERS_STATDES"} <= names


def test_shared_subform_is_marked_ambiguous_not_guessed(priority_root):
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", discover_all=True)
    meta = next(t for t in c.get_schemas() if t.name == "EXTFILES").metadata_json["priority_erp"]
    assert set(meta["parents"]) == {"ORDERS", "CUSTOMERS"}
    assert "path" not in meta


def test_metadata_without_entity_container_keeps_every_type(monkeypatch):
    """Without an EntityContainer there is no way to tell roots from subforms,
    so nothing may be marked — the catalog behaves as before."""
    import requests
    xml = """<?xml version="1.0"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
<Schema Namespace="Priority.OData" xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <EntityType Name="AAA"><Key><PropertyRef Name="K"/></Key><Property Name="K" Type="Edm.String"/>
    <NavigationProperty Name="BBB_SUBFORM" Type="Collection(Priority.OData.BBB)" ContainsTarget="true"/>
    <NavigationProperty Name="CCC_SUBFORM" Type="Collection(Priority.OData.CCC)" ContainsTarget="true"/></EntityType>
  <EntityType Name="BBB"><Key><PropertyRef Name="L"/></Key><Property Name="L" Type="Edm.Int64"/>
    <NavigationProperty Name="CCC_SUBFORM" Type="Collection(Priority.OData.CCC)" ContainsTarget="true"/></EntityType>
  <EntityType Name="CCC"><Key><PropertyRef Name="M"/></Key><Property Name="M" Type="Edm.Int64"/></EntityType>
</Schema></edmx:DataServices></edmx:Edmx>"""

    class _Resp:
        status_code = 200
        text = xml

    monkeypatch.setattr(requests.Session, "get", lambda self, *a, **k: _Resp())
    c = PriorityErpClient(service_root="https://p.example/odata/Priority/t.ini/co", pat="x", discover_all=True)
    tables = {t.name: t for t in c.get_schemas()}
    assert set(tables) == {"AAA", "BBB", "CCC"}
    for t in tables.values():
        meta = t.metadata_json["priority_erp"]
        assert "subform_of" not in meta and "parents" not in meta, t.name
        assert [c.name for c in t.columns] == [{"AAA": "K", "BBB": "L", "CCC": "M"}[t.name]]


def test_subform_queries_like_a_table(client):
    df = client.execute_query("ORDERITEMS")
    assert len(df) == 5
    assert {"ORDNAME", "KLINE", "PRICE"} <= set(df.columns)
    assert _no_nested_values(df)


def test_order_without_lines_yields_no_rows(client):
    df = client.execute_query("ORDERITEMS?$select=ORDNAME,KLINE")
    assert "SO25004" not in set(df["ORDNAME"])


def test_subform_filter_on_its_own_column(client):
    df = client.execute_query("ORDERITEMS?$filter=PRICE gt 1000")
    assert len(df) == 4
    assert (df["PRICE"] > 1000).all()


@pytest.mark.parametrize("since,min_price,expected", [
    ("2026-02-01T00:00:00Z", 1000, [("SO25002", 1150.0), ("SO25003", 1750.05)]),
    ("2026-01-01T00:00:00Z", 0, [("SO25001", 1133.5), ("SO25001", 1200.0), ("SO25002", 1150.0),
                                ("SO25003", 12.75), ("SO25003", 1750.05)]),
    ("2026-03-01T00:00:00Z", 100, [("SO25003", 1750.05)]),
    ("2026-05-01T00:00:00Z", 0, []),
])
def test_subform_filter_mixes_parent_and_own_columns(client, since, min_price, expected):
    """The common question — lines of orders from a period — filters on the
    parent's date and the line's own price in one query."""
    df = client.execute_query(
        f"ORDERITEMS?$filter=CURDATE ge {since} and PRICE gt {min_price}&$select=ORDNAME,PRICE"
    )
    assert (_rows(df, "ORDNAME", "PRICE") if len(df) else []) == expected


def test_or_within_one_level_is_kept_whole(client):
    df = client.execute_query(
        "ORDERITEMS?$filter=(PRICE gt 1500 or TQUANT gt 20) and CDES eq 'Globex'&$select=ORDNAME,KLINE"
    )
    assert _rows(df, "ORDNAME", "KLINE") == [("SO25003", 1), ("SO25003", 2)]


@pytest.mark.parametrize("expr,expected", [
    ("PDES eq 'Doohickey and Sons'", [("SO25003", 1)]),
    ("PDES eq 'O''Brien bolt'", [("SO25003", 2)]),
    ("contains(PDES,'bolt') and CUSTNAME eq '1011'", [("SO25003", 2)]),
    ("not (TQUANT lt 5) and startswith(CDES,'Glo')", [("SO25001", 1), ("SO25003", 1), ("SO25003", 2)]),
])
def test_string_literals_and_functions_survive_translation(client, expr, expected):
    df = client.execute_query(f"ORDERITEMS?$filter={expr}&$select=ORDNAME,KLINE")
    assert _rows(df, "ORDNAME", "KLINE") == expected


def test_select_returns_exactly_the_requested_columns(client):
    df = client.execute_query("ORDERITEMS?$select=PARTNAME,CDES")
    assert list(df.columns) == ["PARTNAME", "CDES"]
    assert len(df) == 5


def test_colliding_parent_column_is_addressed_by_its_prefix(client):
    df = client.execute_query(
        "ORDERITEMS?$filter=ORDERS_STATDES eq 'Final'&$select=ORDNAME,STATDES,ORDERS_STATDES"
    )
    assert set(df["ORDNAME"]) == {"SO25003"}
    assert set(df["STATDES"]) == {"Supplied", "Pending"}   # the lines' own status
    assert set(df["ORDERS_STATDES"]) == {"Final"}           # the order's


@pytest.mark.parametrize("top,expected", [
    (2, [1750.05, 1200.0]),
    (3, [1750.05, 1200.0, 1150.0]),
])
def test_orderby_and_top_apply_across_all_orders(client, top, expected):
    """Inside $expand, $top limits each order separately; the agent asking for
    the most expensive lines means across every order."""
    df = client.execute_query(f"ORDERITEMS?$orderby=PRICE desc&$top={top}&$select=PRICE")
    assert list(df["PRICE"]) == expected


def test_orderby_mixes_parent_and_own_columns(client):
    df = client.execute_query("ORDERITEMS?$orderby=CURDATE desc,PRICE asc&$select=ORDNAME,PRICE")
    assert list(df.itertuples(index=False, name=None)) == [
        ("SO25003", 12.75), ("SO25003", 1750.05), ("SO25002", 1150.0),
        ("SO25001", 1133.5), ("SO25001", 1200.0),
    ]


def test_skip_applies_after_ordering(client):
    df = client.execute_query("ORDERITEMS?$orderby=PRICE desc&$skip=1&$top=2&$select=PRICE")
    assert list(df["PRICE"]) == [1200.0, 1150.0]


def test_max_rows_caps_the_flattened_rows(client):
    assert len(client.execute_query("ORDERITEMS", max_rows=3)) == 3


@pytest.mark.parametrize("expr", [
    "CURDATE ge 2026-02-01T00:00:00Z or PRICE gt 1000",
    "PRICE gt TOTPRICE",
])
def test_clause_mixing_levels_is_refused_not_guessed(client, expr):
    with pytest.raises(ValueError) as exc:
        client.execute_query(f"ORDERITEMS?$filter={expr}")
    msg = str(exc.value)
    assert "ORDERS" in msg and "ORDERITEMS" in msg


def test_unknown_column_in_subform_query_is_named(client):
    with pytest.raises(ValueError) as exc:
        client.execute_query("ORDERITEMS?$filter=NOSUCHCOL eq 1")
    assert "NOSUCHCOL" in str(exc.value)


def test_url_encoded_subform_query(client):
    assert len(client.execute_query("ORDERITEMS?$filter=PRICE%20gt%201000")) == 4


def test_nested_subform_query_filters_at_every_level(priority_root):
    c = _nested_client(priority_root)
    df = c.execute_query(
        "ORDERITEMSTEXT?$filter=CDES eq 'Globex' and PRICE lt 100 and contains(TEXT,'Galv')"
        "&$select=ORDNAME,KLINE,TEXT"
    )
    assert _rows(df, "ORDNAME", "KLINE", "TEXT") == [("SO25003", 2, "Galvanised only")]
    df = c.execute_query("ORDERITEMSTEXT?$filter=CURDATE lt 2026-02-01T00:00:00Z")
    assert _rows(df, "ORDNAME", "KLINE", "TEXTLINE") == [("SO25001", 1, 1), ("SO25001", 1, 2)]


def test_explicit_expand_returns_the_lines_not_a_count(client):
    df = client.execute_query("ORDERS?$filter=ORDNAME eq 'SO25001'&$expand=ORDERITEMS_SUBFORM")
    assert _rows(df, "ORDNAME", "KLINE") == [("SO25001", 1), ("SO25001", 2)]
    assert _no_nested_values(df)
    df = client.execute_query(
        "ORDERS?$select=ORDNAME&$expand=ORDERITEMS_SUBFORM($filter=PRICE gt 1500;$select=PRICE)"
    )
    # the inner filter picks lines; every order asked for stays in the answer
    assert len(df) == 4
    assert _rows(df.dropna(subset=["PRICE"]), "ORDNAME", "PRICE") == [("SO25003", 1750.05)]


def test_expanding_two_subforms_at_once_is_refused(client):
    """Flattening two sibling lists into one frame multiplies rows and skews
    every sum; the agent must ask for each separately."""
    with pytest.raises(ValueError):
        client.execute_query("ORDERS?$expand=ORDERITEMS_SUBFORM,ORDERSTEXT_SUBFORM")


def test_shared_subform_query_names_its_parents(priority_root):
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", discover_all=True)
    with pytest.raises(ValueError) as exc:
        c.execute_query("EXTFILES")
    assert "ORDERS" in str(exc.value) and "CUSTOMERS" in str(exc.value)


def test_attached_catalog_routes_without_a_metadata_crawl(priority_root, mock_module):
    """At query time the client gets the indexed catalog from the DB; it must
    route a subform from that alone, never regenerating $metadata."""
    catalog = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN").get_schemas()
    fresh = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")
    fresh.attach_table_metadata([{"name": t.name, "metadata_json": t.metadata_json} for t in catalog])
    mock_module.REQUEST_LOG.clear()
    df = fresh.execute_query("ORDERITEMS?$filter=PRICE gt 1000")
    assert len(df) == 4
    assert not any("metadata" in p.lower() for p in mock_module.REQUEST_LOG)


def test_unknown_form_404_names_the_subform_cause(client):
    with pytest.raises(RuntimeError) as exc:
        client.execute_query("NOSUCHFORM")
    msg = str(exc.value)
    assert "404" in msg and "subform" in msg.lower()


def test_agent_context_shows_the_subform_access_path(client):
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    tables = client.get_schemas()
    ctx = TablesSchemaContext(data_sources=[
        TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Priority", type="priority_erp"),
            tables=tables,
        )
    ])
    out = ctx.render_combined(top_k_per_ds=len(tables), index_limit=200)
    assert 'subform_of="ORDERS"' in out and 'navigation="ORDERITEMS_SUBFORM"' in out
    # the order's columns on the line table are marked as the order's
    assert 'name="CURDATE" dtype="Edm.DateTimeOffset" description="Order Date" from_form="ORDERS"' in out
    subforms = [t for t in tables if "subform_of" in t.metadata_json["priority_erp"]]
    assert out.count("subform_of=") == len(subforms)


def test_system_prompt_teaches_querying_a_subform_as_a_table(client):
    """The guide must show a subform queried directly by name — the agent
    copies the examples it is given."""
    import re
    assert re.search(r"\bORDERITEMS\?\$filter=", client.system_prompt())


@pytest.mark.parametrize("expr,expected", [
    # `and` binds tighter than `or`: P-100 lines, plus P-300 lines over 1500
    ("PARTNAME eq 'P-100' or PARTNAME eq 'P-300' and PRICE gt 1500",
     [("SO25001", 1), ("SO25002", 1), ("SO25003", 1)]),
    ("PARTNAME eq 'P-400' or PARTNAME eq 'P-100' and TQUANT gt 5",
     [("SO25001", 1), ("SO25003", 2)]),
])
def test_or_binds_looser_than_and(client, expr, expected):
    df = client.execute_query(f"ORDERITEMS?$filter={expr}&$select=ORDNAME,KLINE")
    assert _rows(df, "ORDNAME", "KLINE") == expected


def test_or_across_forms_hidden_by_and_is_refused(client):
    """`CDES eq 'Globex' or CUSTNAME eq '1011' and PRICE gt 1000` is
    A or (B and C) — splitting on the `and` would silently drop rows."""
    with pytest.raises(ValueError):
        client.execute_query("ORDERITEMS?$filter=CDES eq 'Globex' or CUSTNAME eq '1011' and PRICE gt 1000")


def test_explicit_expand_keeps_an_order_without_lines(client):
    df = client.execute_query("ORDERS?$filter=ORDNAME eq 'SO25004'&$expand=ORDERITEMS_SUBFORM")
    assert list(df["ORDNAME"]) == ["SO25004"]


def test_percent_encoded_expand_is_flattened(client):
    df = client.execute_query("ORDERS?$filter=ORDNAME%20eq%20'SO25001'&%24expand=ORDERITEMS_SUBFORM")
    assert _rows(df, "ORDNAME", "KLINE") == [("SO25001", 1), ("SO25001", 2)]
    assert _no_nested_values(df)


def test_single_entity_subform(priority_root):
    """Priority's text subforms hold one record per parent, not a list."""
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN", forms="ORDERS,ORDERSTEXT")
    df = c.execute_query("ORDERSTEXT?$select=ORDNAME,TEXT")
    assert _rows(df, "ORDNAME", "TEXT") == [("SO25001", "Priority customer")]
    df = c.execute_query("ORDERS?$select=ORDNAME&$expand=ORDERSTEXT_SUBFORM")
    assert len(df) == 4  # every order, the one with a remark carrying it
    assert set(df.loc[df["ORDNAME"] == "SO25001", "TEXT"]) == {"Priority customer"}


@pytest.mark.parametrize("attached", [
    [],                                                                    # nothing active
    [{"name": "ORDERITEMS", "metadata_json": {"priority_erp": {"form": "ORDERITEMS"}}}],  # indexed before subforms
])
def test_catalog_without_subform_paths_still_routes_a_subform(priority_root, attached):
    """An install indexed before subform support must not keep the 404 until
    someone refreshes the schema."""
    c = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")
    c.attach_table_metadata(attached)
    assert len(c.execute_query("ORDERITEMS?$filter=PRICE gt 1000")) == 4


def test_current_catalog_settles_unknown_names_without_a_crawl(priority_root, mock_module):
    """With a catalog indexed with subform support, a 404 on a name it does
    not mark as a subform is final — no minutes-long $metadata regeneration."""
    catalog = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN").get_schemas()
    fresh = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")
    fresh.attach_table_metadata([{"name": t.name, "metadata_json": t.metadata_json} for t in catalog])
    mock_module.REQUEST_LOG.clear()
    with pytest.raises(RuntimeError, match="404"):
        fresh.execute_query("NOSUCHFORM")
    assert not any("metadata" in p.lower() for p in mock_module.REQUEST_LOG)



def test_subform_result_names_the_request_actually_sent(client):
    """The agent's code says ORDERITEMS, which Priority does not serve. The
    result carries the parent request that was sent, for the run output, so a
    reader who knows Priority can recognise and replay it."""
    df = client.execute_query("ORDERITEMS?$filter=CURDATE ge 2026-02-01T00:00:00Z and PRICE gt 1000")
    note = df.attrs.get("bow_notice") or ""
    assert "ORDERS?" in note and "$expand=ORDERITEMS_SUBFORM(" in note
    assert "CURDATE ge 2026-02-01T00:00:00Z" in note and "PRICE gt 1000" in note


def test_plain_form_result_carries_no_notice(client):
    assert "bow_notice" not in client.execute_query("ORDERS?$top=1").attrs


def test_subform_result_carries_the_bare_sent_request(client):
    """The Queries tab shows exactly what Priority received: the parent request,
    with nothing around it, matching the run-output notice."""
    df = client.execute_query("ORDERITEMS?$filter=PRICE gt 1000&$select=ORDNAME,PRICE")
    sent = df.attrs.get("bow_sent_request")
    assert sent and sent.startswith("ORDERS?") and "$expand=ORDERITEMS_SUBFORM(" in sent
    assert sent in df.attrs["bow_notice"]
    assert "bow_sent_request" not in client.execute_query("ORDERS?$top=1").attrs


# ── titles: what users call forms and columns ─────────────────────────────
# Priority names everything twice: a technical name (PORDERS, SUPNAME) that the
# API and queries use, and a title ("Purchase Orders", "Supplier Number" — Hebrew
# on an Israeli tenant) that users see. The agent must know both, and answers
# should show titles.

def test_form_title_is_recorded_and_reaches_the_agent(client):
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    tables = {t.name: t for t in client.get_schemas()}
    assert tables["ORDERS"].metadata_json["priority_erp"]["title"] == "Customer Orders"
    assert tables["PART"].metadata_json["priority_erp"]["title"] == "פריטים"
    ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="1", name="Priority", type="priority_erp"),
        tables=list(tables.values()),
    )])
    out = ctx.render_combined(top_k_per_ds=len(tables), index_limit=200)
    assert 'title="Customer Orders"' in out and 'title="פריטים"' in out


def test_agent_is_told_to_answer_with_titles_once_per_source(client):
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema
    from app.ai.prompt_formatters import Table, TableColumn

    other = Table(name="sales", columns=[TableColumn(name="id", dtype="int")], pks=[], fks=[])
    ctx = TablesSchemaContext(data_sources=[
        TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Priority", type="priority_erp"),
            tables=client.get_schemas()),
        TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="2", name="Warehouse", type="postgresql"),
            tables=[other]),
    ])
    for out in (ctx.render_combined(top_k_per_ds=10, index_limit=200), ctx.render()):
        assert out.count("<presentation_note>") == 1
        note_at = out.index("<presentation_note>")
        assert out.rfind('name="Priority"', 0, note_at) > out.rfind('name="Warehouse"', 0, note_at)


def test_column_titles_are_stored_for_query_time_headers(client):
    tables = {t.name: t for t in client.get_schemas()}
    assert tables["ORDERS"].metadata_json["priority_erp"]["column_titles"]["ORDNAME"] == "Order Number"
    items = tables["ORDERITEMS"].metadata_json["priority_erp"]["column_titles"]
    assert items["PRICE"] == "Unit Price" and items["CURDATE"] == "Order Date"
    assert items["STATDES"] == "Line Status"
    # a renamed (colliding) parent column says whose it is
    assert items["ORDERS_STATDES"] == "Order Stage (Customer Orders)"
    # the label also travels as structured column metadata (per-user schemas)
    ordname = next(c for c in tables["ORDERS"].columns if c.name == "ORDNAME")
    assert ordname.metadata["title"] == "Order Number"


@pytest.mark.parametrize("query,expected", [
    ("ORDERS?$select=ORDNAME,CDES", {"ORDNAME": "Order Number", "CDES": "Customer Name"}),
    ("ORDERITEMS?$select=ORDNAME,PRICE,STATDES,ORDERS_STATDES",
     {"ORDNAME": "Order Number", "PRICE": "Unit Price", "STATDES": "Line Status",
      "ORDERS_STATDES": "Order Stage (Customer Orders)"}),
    ("ORDERS?$filter=ORDNAME eq 'SO25001'&$select=ORDNAME,STATDES&$expand=ORDERITEMS_SUBFORM($select=KLINE,STATDES)",
     {"ORDNAME": "Order Number", "KLINE": "Line Number", "STATDES": "Line Status",
      "ORDERS_STATDES": "Order Stage (Customer Orders)"}),
])
def test_results_carry_the_titles_of_their_columns(client, query, expected):
    client.get_schemas()  # in production the indexed catalog is attached instead
    df = client.execute_query(query)
    assert df.attrs.get("bow_column_titles") == expected


def test_attached_catalog_supplies_titles_without_a_metadata_crawl(priority_root, mock_module):
    catalog = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN").get_schemas()
    fresh = PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")
    fresh.attach_table_metadata([{"name": t.name, "metadata_json": t.metadata_json} for t in catalog])
    mock_module.REQUEST_LOG.clear()
    df = fresh.execute_query("CUSTOMERS?$select=CUSTNAME,CUSTDES")
    assert df.attrs["bow_column_titles"] == {"CUSTNAME": "Customer Number", "CUSTDES": "Customer Name"}
    assert not any("metadata" in p.lower() for p in mock_module.REQUEST_LOG)
