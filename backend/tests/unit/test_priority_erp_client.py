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
def priority_root():
    """Serve the mock Priority OData service and yield its service root."""
    import sys
    from pathlib import Path
    tools = Path(__file__).resolve().parents[3] / "tools" / "priority"
    sys.path.insert(0, str(tools))
    import mock_server  # noqa: E402

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


@pytest.fixture
def client(priority_root):
    return PriorityErpClient(service_root=priority_root, pat="DEMOTOKEN")


# ── connection + auth ──────────────────────────────────────────────────────

def test_connect_with_pat(client):
    result = client.test_connection()
    assert result["success"] is True, result
    assert "form(s)" in result["message"]


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


def test_keys_and_composite_keys(client):
    tables = {t.name: t for t in client.get_schemas()}
    assert [p.name for p in tables["ORDERS"].pks] == ["ORDNAME"]
    # Priority uses composite keys widely; $select must include all of them.
    assert [p.name for p in tables["ORDERITEMS"].pks] == ["ORDNAME", "KLINE"]


def test_subforms_become_join_paths(client):
    """Navigation properties are subforms — surfaced as fks so the agent can
    see the join without needing a connector-specific metadata block."""
    orders = next(t for t in client.get_schemas() if t.name == "ORDERS")
    assert [(f.column.name, f.references_name) for f in orders.fks] == [("ORDNAME", "ORDERITEMS")]
    assert orders.metadata_json["priority_erp"]["subforms"] == ["ORDERITEMS_SUBFORM"]


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


def test_expand_subform_is_summarised_not_raw(client):
    """Nested subform lists would make the DataFrame unusable downstream, so
    they collapse to a count."""
    df = client.execute_query("ORDERS?$filter=ORDNAME eq 'SO25001'&$expand=ORDERITEMS_SUBFORM")
    assert df.iloc[0]["ORDERITEMS_SUBFORM"] == 2


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
