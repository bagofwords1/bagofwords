"""Teradata connector contract, against a stand-in for the `teradatasql` driver.

The driver is the boundary: `tests/mocks/fake_teradatasql.py` replaces it with
a DuckDB-backed fake that reproduces the real driver's parameter checks, error
text (one line plus a Go stack trace), DBC dictionary views and cancel(). The
client, the shared engine pool, query cancellation, the timeout wrapper and the
schema renderer all run for real.

What the connector promises:
  * discovery shows exactly the tables and views the user can query, every
    column typed (views too), with primary-index / key information
  * one Teradata logon serves many queries (logons are slow and audited)
  * a query the agent gives up on stops on the server, not only in BOW
  * errors reach the user and the agent as one readable line, keeping the
    driver's exception type and the Teradata error code
  * the TLS / encryption settings on the form reach the driver
"""

import base64
import importlib.util
import pathlib
import re
import sys
import threading
import time

import pytest

from app.data_sources import engine_pool
from app.data_sources import query_cancellation as qc

FAKE_PATH = pathlib.Path(__file__).resolve().parents[1] / "mocks" / "fake_teradatasql.py"
USER, PASSWORD = "bow_reader", "Reader#2026"


@pytest.fixture
def fake(monkeypatch):
    spec = importlib.util.spec_from_file_location("teradatasql", FAKE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "teradatasql", module)
    monkeypatch.delenv("FAKE_TERADATA_QVCI", raising=False)
    engine_pool.dispose_all()
    yield module
    engine_pool.dispose_all()


def _client(**overrides):
    from app.data_sources.clients.teradata_client import TeradataClient

    kwargs = {"host": "td.example.com", "database": "sales, finance, hr", "user": USER, "password": PASSWORD}
    kwargs.update(overrides)
    return TeradataClient(**kwargs)


def _tables(client):
    return {t.name: t for t in client.get_schemas()}


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def test_discovery_lists_only_objects_the_user_can_select_from(fake):
    tables = _tables(_client())

    # hr.salaries is in DBC.TablesV but the user has no access (error 3523 if
    # queried); the join index is not a table.
    assert "hr.salaries" not in tables
    assert not any("ji_orders_by_date" in name for name in tables)
    assert {"sales.orders", "sales.customers", "sales.v_monthly_revenue",
            "finance.product_sales"} <= set(tables)
    for t in tables.values():
        assert t.metadata_json["schema"] == t.name.split(".")[0]


@pytest.mark.parametrize("qvci", ["0", "1"])
def test_view_columns_are_typed_whether_or_not_qvci_is_enabled(fake, monkeypatch, qvci):
    # DBC.ColumnsV reports view columns with a NULL type.
    monkeypatch.setenv("FAKE_TERADATA_QVCI", qvci)
    view = _tables(_client())["sales.v_monthly_revenue"]

    assert view.metadata_json["teradata"]["kind"] == "view"
    dtypes = {c.name: c.dtype for c in view.columns}
    assert dtypes == {"month_start": "DATE", "region": "CHAR(4)", "revenue": "DECIMAL(18,2)"}


def test_a_broken_view_does_not_fail_discovery(fake):
    tables = _tables(_client())

    assert "finance.v_broken" in tables
    assert all(c.dtype for c in tables["sales.v_monthly_revenue"].columns)


def test_column_types_carry_length_precision_and_character_set(fake):
    tables = _tables(_client())
    orders = {c.name: c.dtype for c in tables["sales.orders"].columns}
    events = {c.name: c.dtype for c in tables["sales.order_events"].columns}

    assert orders["amount"] == "DECIMAL(12,2)"
    assert orders["status"] == "VARCHAR(20)"
    # UNICODE stores 2 bytes per character: 400 bytes is VARCHAR(200).
    assert orders["note"] == "VARCHAR(200) CHARACTER SET UNICODE"
    assert events["valid_period"] == "PERIOD(DATE)"
    assert events["event_id"] == "BIGINT"
    assert all(c.dtype for t in tables.values() if t.name != "finance.v_broken" for c in t.columns)


def test_primary_index_and_keys_are_attached(fake):
    tables = _tables(_client())

    orders = tables["sales.orders"]  # unique primary index
    assert orders.metadata_json["teradata"]["primary_index"] == "order_id"
    assert orders.metadata_json["teradata"]["primary_index_unique"] is True
    assert [p.name for p in orders.pks] == ["order_id"]

    customers = tables["sales.customers"]  # declared primary key
    assert [p.name for p in customers.pks] == ["customer_id"]

    sales = tables["finance.product_sales"]  # non-unique primary index: not a key
    assert sales.metadata_json["teradata"]["primary_index"] == "product_id, region"
    assert not sales.pks


def test_primary_index_reaches_the_agent_schema_context(fake):
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    tables = list(_tables(_client()).values())
    ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="1", name="TD", type="teradata"), tables=tables,
    )])
    out = ctx.render("full")

    assert 'primary_index="product_id, region"' in out
    assert 'kind="view"' in out


def test_connection_test_count_matches_discovery_without_reading_columns(fake):
    client = _client()
    count = client.count_tables()
    requests_for_count = fake.STATS["requests"]

    assert count == len(client.get_schemas())
    assert not any("Columns" in s for s in fake.STATS["statements"][:requests_for_count])


# --------------------------------------------------------------------------
# Sessions, cancellation, timeouts
# --------------------------------------------------------------------------

def test_many_queries_share_one_teradata_logon(fake):
    client = _client()
    for _ in range(5):
        client.execute_query("SELECT TOP 1 order_id FROM sales.orders")
    client.get_schemas()

    assert fake.STATS["logons"] == 1


def test_health_probe_leaves_no_session_behind(fake):
    from app.data_sources.engine_pool import ephemeral

    with ephemeral():
        assert _client().test_connection()["success"] is True
    assert fake.STATS["logons"] == fake.STATS["logoffs"] == 1


def test_cancelling_a_running_query_stops_it_on_the_server(fake):
    client = _client()
    result = {}

    def run():
        try:
            client.execute_query("SELECT * FROM sales.slow_scan")
        except Exception as e:
            result["error"] = e

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 5
    while fake.STATS["active"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    outcome = qc.cancel_thread(client, worker.ident)
    worker.join(5)

    assert outcome == "cancelled (teradata cancel)"
    assert not worker.is_alive()
    assert "[Error 3110]" in str(result["error"])
    assert fake.STATS["cancels"] == 1
    # The pooled session survives the cancel.
    assert len(client.execute_query("SELECT 1 AS x")) == 1


def test_agent_query_timeout_cancels_the_query_on_the_server(fake):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper, QueryTimeoutError

    client = _client()
    timings = []
    wrapper = QueryCapturingClientWrapper(client, [], timings, query_timeout_seconds=1)
    with pytest.raises(QueryTimeoutError):
        wrapper.execute_query("SELECT * FROM sales.slow_scan")

    assert timings[0]["cancellation"] == "cancelled (teradata cancel)"
    deadline = time.monotonic() + 5
    while fake.STATS["active"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.STATS["cancels"] == 1
    assert fake.STATS["active"] == 0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql,code", [
    ("SELECT * FROM sales.orders LIMIT 5", "3706"),
    ("SELECT TOP 5 * FROM sales.orders QUALIFY ROW_NUMBER() OVER (ORDER BY order_id) = 1", "3706"),
    ("SELECT * FROM sales.no_such_table", "3807"),
    ("SELECT * FROM hr.salaries", "3523"),
])
def test_query_errors_are_one_line_with_type_and_code_kept(fake, sql, code):
    with pytest.raises(fake.OperationalError) as excinfo:
        _client().execute_query(sql)

    message = str(excinfo.value)
    assert "\n" not in message
    assert f"[Error {code}]" in message
    assert "gosqldriver" not in message
    assert "[Version" not in message


@pytest.mark.parametrize("overrides,code", [
    ({"password": "wrong"}, "8017"),
    ({"host": "unreachable.invalid"}, "493"),
    ({"database": "no_such_db"}, "3802"),
])
def test_connection_test_failures_are_one_readable_line(fake, overrides, code):
    result = _client(**overrides).test_connection()

    assert result["success"] is False
    assert f"[Error {code}]" in result["message"]
    assert "\n" not in result["message"]


def test_errors_raised_inside_a_connection_block_keep_their_type(fake):
    class Abort(Exception):
        pass

    with pytest.raises(Abort), _client().connect():
        raise Abort("stop")


# --------------------------------------------------------------------------
# Connection settings
# --------------------------------------------------------------------------

def test_tls_and_encryption_settings_reach_the_driver(fake):
    pem = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUD\n-----END CERTIFICATE-----\n"
    client = _client(sslmode="verify-full", ssl_ca=pem, encryptdata=False)
    assert client.test_connection()["success"] is True

    sent = fake.STATS["last_logon"]
    assert sent["sslmode"] == "VERIFY-FULL"
    assert sent["encryptdata"] == "false"
    padded = sent["sslbase64"] + "=" * (-len(sent["sslbase64"]) % 4)
    assert base64.urlsafe_b64decode(padded).decode().strip() == pem.strip()
    assert all(isinstance(v, str) for v in sent.values())


def test_defaults_prefer_tls_and_encrypt_data(fake):
    _client().test_connection()
    sent = fake.STATS["last_logon"]

    assert sent["sslmode"] == "PREFER"
    assert sent["encryptdata"] == "true"


def test_invalid_ssl_mode_is_rejected():
    with pytest.raises(ValueError):
        _client(sslmode="skip-verify")


def test_additional_params_reach_the_driver_as_strings(fake):
    client = _client(additional_params={"cop": False, "Account": "$M_acct", "coplast": "true"})
    assert client.test_connection()["success"] is True

    sent = fake.STATS["last_logon"]
    assert sent["cop"] == "false"
    assert sent["account"] == "$M_acct"
    assert sent["coplast"] == "true"


def test_additional_params_cannot_override_credentials_or_tls(fake):
    extras = {"password": "x", "sslmode": "DISABLE", "logdata": "token=abc", "encryptdata": "false"}
    client = _client(additional_params=extras)

    result = client.test_connection()
    assert result["success"] is False
    for key in extras:
        assert key in result["message"]

    # A connection saved before this check still runs, without them.
    client.execute_query("SELECT 1 AS x")
    sent = fake.STATS["last_logon"]
    assert sent["password"] == "***"  # the fake masks it; the real one was used
    assert sent["sslmode"] == "PREFER"
    assert sent["encryptdata"] == "true"
    assert "logdata" not in sent


def test_every_allowed_additional_param_is_a_real_driver_parameter(fake):
    from app.data_sources.clients.teradata_client import ALLOWED_EXTRA_PARAMS

    assert ALLOWED_EXTRA_PARAMS <= fake.KNOWN_PARAMS


def test_every_config_field_is_accepted_by_the_client():
    import inspect

    from app.data_sources.clients.teradata_client import TeradataClient
    from app.schemas.data_sources.configs import TeradataConfig, TeradataCredentials

    accepted = set(inspect.signature(TeradataClient.__init__).parameters)
    fields = set(TeradataConfig.model_fields) | set(TeradataCredentials.model_fields)
    assert fields <= accepted


def test_editing_a_connection_drops_its_pooled_sessions(fake):
    import json

    from app.services.connection_service import _invalidate_engine_pool

    client = _client()
    client.execute_query("SELECT 1 AS x")
    assert any(k[0] == client.teradata_uri for k in engine_pool._engines)

    class Connection:
        id = "c1"
        type = "teradata"
        config = json.dumps({"host": "td.example.com", "database": "sales, finance, hr"})

        def decrypt_credentials(self):
            return {"user": USER, "password": PASSWORD}

    _invalidate_engine_pool(Connection())
    assert not any(k[0] == client.teradata_uri for k in engine_pool._engines)


# --------------------------------------------------------------------------
# Agent guidance
# --------------------------------------------------------------------------

def test_prompt_states_the_connection_query_timeout_only_when_known(fake):
    client = _client()
    # The org default is resolved elsewhere; no number is better than a wrong one.
    assert not re.search(r"time out after \d+s", client.description)

    client._bow_connection_query_timeout = 240
    assert "240s" in client.description


def test_relative_date_hint_uses_teradata_functions():
    from app.data_sources.clients.teradata_client import TeradataClient

    assert "CURRENT_DATE" in TeradataClient.relative_date_hint
    assert "ADD_MONTHS" in TeradataClient.relative_date_hint


def test_prompt_says_read_only_and_examples_cannot_be_mistaken_for_real_tables(fake):
    # A real LLM (Haiku) attributed a sample query's `order_status` column to a
    # real sales.orders table, and offered to run a DELETE "after confirmation".
    client = _client()
    text = client.description
    real_tables = [t.name for t in client.get_schemas()]

    assert "read-only" in text.lower()
    for name in real_tables:
        assert name not in text
