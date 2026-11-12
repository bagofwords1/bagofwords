"""Every way sandbox code can hand a statement to a guarded client is checked.

Contract: for a client the read-only guard knows (by type, so subclasses
included), the statement is resolved from whatever argument shape the code
used — positional, the client's own parameter name, `.query(...)` — and goes
through the read-only guard and the agent's table selection before anything
reaches the source. The client then receives exactly the text that was checked
and captured. Shapes that cannot be resolved to one text statement are refused
with nothing sent. Clients the guard does not know keep their own calling
convention untouched.

Runs through `StreamingCodeExecutor.execute_code` against a real SQLite file,
in both sandbox modes. Subprocess mode sends arguments child -> parent as
JSON, which turns unknown types into text, so the expectations differ per
mode where that matters.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import (
    StreamingCodeExecutor,
    TableAccessError,
    UnsafeSQLError,
)
from app.ai.code_execution.query_params import verify_identity_binds_in_queries
from app.ai.code_execution.sql_guard import ReadOnlyViolation, dialect_for_client, ensure_read_only
from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR, TableScope, TableScopeViolation
from app.data_sources.clients.bigquery_client import BigqueryClient
from app.data_sources.clients.presto_client import PrestoClient
from app.data_sources.clients.splunk_client import SplunkClient
from app.data_sources.clients.sqlite_client import SqliteClient
from app.schemas.param_schema import ParamSpec

_LINUX = sys.platform.startswith("linux")


@pytest.fixture(params=[
    "inprocess",
    pytest.param("subprocess", marks=pytest.mark.skipif(not _LINUX, reason="sandbox child needs Linux")),
])
def mode(request, monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


@pytest.fixture
def shop(tmp_path):
    """A SQLite file with an enabled table (`orders`) and a disabled one (`secret`)."""
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(i, i * 10.0) for i in range(1, 6)])
    con.execute("CREATE TABLE secret (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO secret VALUES (1)")
    con.commit()
    con.close()
    return path


def _orders_left(path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        con.close()


def _scoped(client):
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(["orders"], ["secret"]))
    return client


class RecordingSqliteClient(SqliteClient):
    """The real SQLite client, recording what reaches its `execute_query` —
    the driver boundary. Also proves a subclass is guarded like its parent."""

    def __init__(self, database: str):
        super().__init__(database=database)
        self.sent: list = []

    def execute_query(self, sql):
        self.sent.append(sql)
        return super().execute_query(sql)


def _run(clients, body: str):
    code = "def generate_df(ds_clients, excel_files):\n" + "".join(
        f"    {line}\n" for line in body.strip().splitlines()
    )
    return StreamingCodeExecutor().execute_code(code=code, ds_clients=clients, excel_files=[])


# Built at runtime so the static scan of the code has no literal to match.
_RUNTIME_DROP = "bytes.fromhex('44524f50205441424c45206f7264657273').decode()"

_NAMED_CALLS = [
    "execute_query(sql={q})",
    "query(sql={q})",
    "execute_query(query={q})",
    "query({q})",
]


@pytest.mark.parametrize("call", _NAMED_CALLS)
def test_statement_passed_by_name_reads_only_enabled_tables(mode, shop, call):
    client = _scoped(SqliteClient(database=str(shop)))
    with pytest.raises(TableAccessError):
        _run({"shop": client}, "return ds_clients['shop']." + call.format(q="'SELECT * FROM secret'"))
    df, _, _ = _run({"shop": client},
                    "return ds_clients['shop']." + call.format(q="'SELECT COUNT(*) AS n FROM orders'"))
    assert int(df["n"].iloc[0]) == 5


@pytest.mark.parametrize("call", _NAMED_CALLS)
def test_write_built_at_runtime_is_refused_under_any_name(mode, shop, call):
    client = SqliteClient(database=str(shop))
    with pytest.raises(UnsafeSQLError):
        _run({"shop": client}, f"sql = {_RUNTIME_DROP}\nreturn ds_clients['shop']." + call.format(q="sql"))
    assert _orders_left(shop) == 5


@pytest.mark.parametrize("call", [
    "execute_query('SELECT 1 AS a', sql='SELECT 2 AS a')",
    "execute_query('SELECT 1 AS a', 'SELECT 2 AS a')",
    "execute_query('SELECT 1 AS a', timeout=5)",
    "execute_query(sql='SELECT 1 AS a', timeout=5)",
    "execute_query(statement='SELECT 1 AS a')",
])
def test_unresolvable_call_shape_is_refused_and_nothing_is_sent(mode, shop, call):
    client = RecordingSqliteClient(str(shop))
    with pytest.raises(UnsafeSQLError):
        _run({"shop": client}, "return ds_clients['shop']." + call)
    assert client.sent == []


@pytest.mark.parametrize("statement", ["{'sql': 'SELECT 1'}", "['SELECT 1']", "1", "None"])
@pytest.mark.parametrize("form", ["execute_query({s})", "execute_query(sql={s})"])
def test_non_text_statement_is_refused_in_both_modes(mode, shop, statement, form):
    client = RecordingSqliteClient(str(shop))
    with pytest.raises(UnsafeSQLError):
        _run({"shop": client}, "return ds_clients['shop']." + form.format(s=statement))
    assert client.sent == []


@pytest.mark.parametrize("mode", ["inprocess"], indirect=True)
def test_bytes_statement_is_refused_in_process(mode, shop):
    client = RecordingSqliteClient(str(shop))
    with pytest.raises(UnsafeSQLError):
        _run({"shop": client}, f"return ds_clients['shop'].execute_query({_RUNTIME_DROP}.encode())")
    assert client.sent == []
    assert _orders_left(shop) == 5


@pytest.mark.skipif(not _LINUX, reason="sandbox child needs Linux")
@pytest.mark.parametrize("mode", ["subprocess"], indirect=True)
def test_bytes_statement_crosses_the_sandbox_as_checked_text(mode, shop):
    """Subprocess mode turns bytes into their text form on the way to the
    parent; that text is checked like any other, never sent unchecked."""
    client = RecordingSqliteClient(str(shop))
    try:
        _run({"shop": client}, f"return ds_clients['shop'].execute_query({_RUNTIME_DROP}.encode())")
    except UnsafeSQLError:
        assert client.sent == []
    except Exception:
        pass  # the source rejected text the guard had allowed
    for text in client.sent:
        assert type(text) is str
        ensure_read_only(client, text)  # only guard-approved text was sent
    assert _orders_left(shop) == 5


def test_subclass_of_guarded_client_is_guarded(mode, shop):
    client = _scoped(RecordingSqliteClient(str(shop)))
    with pytest.raises(UnsafeSQLError):
        _run({"shop": client}, f"return ds_clients['shop'].execute_query({_RUNTIME_DROP})")
    with pytest.raises(TableAccessError):
        _run({"shop": client}, "return ds_clients['shop'].execute_query('SELECT * FROM secret')")
    assert client.sent == []
    assert _orders_left(shop) == 5


def test_guard_identity_follows_the_most_specific_registered_class():
    class CustomPresto(PrestoClient):
        pass

    assert dialect_for_client(object.__new__(PrestoClient)) == "presto"
    assert dialect_for_client(object.__new__(CustomPresto)) == "presto"


def test_guard_returns_the_text_to_send():
    client = SqliteClient(database=":memory:")
    assert ensure_read_only(client, "SELECT 1 AS a") == "SELECT 1 AS a"
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, "DROP TABLE orders")


@pytest.mark.parametrize("call", [
    "execute_query(sql='SELECT id FROM orders WHERE id = :id', params={'id': 2})",
    "execute_query('SELECT id FROM orders WHERE id = :id', params={'id': 2})",
    "query(sql='SELECT id FROM orders WHERE id = :id', params={'id': 2})",
])
def test_client_receives_exactly_the_captured_text(mode, shop, call, monkeypatch):
    # ensure_read_only's contract is "returns what to send" (a later phase
    # returns canonical text). A guard that returns new text shows whether the
    # capture and the client both get the returned value, not the input.
    import app.ai.code_execution.code_execution as code_execution

    real_guard = code_execution.ensure_read_only
    returned: list = []

    def guard_returning_new_text(client, statement):
        checked = "/* checked */ " + real_guard(client, statement)
        returned.append(checked)
        return checked

    monkeypatch.setattr(code_execution, "ensure_read_only", guard_returning_new_text)
    client = RecordingSqliteClient(str(shop))
    df, _, captured = _run({"shop": client}, "return ds_clients['shop']." + call)
    assert df["id"].tolist() == [2]
    assert len(client.sent) == 1
    assert client.sent == returned
    assert [str(q) for q in captured] == returned
    assert ":id" not in client.sent[0]


@pytest.mark.parametrize("call", [
    "execute_query('SELECT 1 AS a', 'SELECT 2 AS a')",
    "execute_query(statement='SELECT 1 AS a')",
    "execute_query(sql='SELECT 1 AS a', timeout=5)",
    "execute_query(['SELECT 1'])",
])
def test_call_shape_refusal_names_only_the_call_problem(mode, shop, call):
    client = RecordingSqliteClient(str(shop))
    with pytest.raises(UnsafeSQLError) as refused:
        _run({"shop": client}, "return ds_clients['shop']." + call)
    assert "Only SELECT" not in str(refused.value)


def test_statement_refusal_still_says_only_select_is_allowed(mode, shop):
    client = RecordingSqliteClient(str(shop))
    with pytest.raises(UnsafeSQLError) as refused:
        _run({"shop": client}, f"return ds_clients['shop'].execute_query({_RUNTIME_DROP})")
    assert "Only SELECT queries are allowed." in str(refused.value)


def test_empty_statement_reaches_the_driver_as_today(mode, shop):
    """No new rule for blank text: the guard passes it and the driver decides."""
    client = RecordingSqliteClient(str(shop))
    try:
        _run({"shop": client}, "return ds_clients['shop'].execute_query('   ')")
    except (UnsafeSQLError, TableAccessError) as e:  # pragma: no cover - the failure being guarded against
        pytest.fail(f"blank statement refused by the guard: {e}")
    except Exception:
        pass
    assert client.sent == ["   "]


class KeywordOnlyClient:
    """A Qlik-style client the guard does not know: keyword arguments only."""

    def __init__(self):
        self.received: list = []

    def execute_query(self, **kwargs):
        self.received.append(kwargs)
        return pd.DataFrame([{"rows": 1}])


def test_unguarded_keyword_only_client_receives_its_arguments_unchanged(mode):
    client = KeywordOnlyClient()
    df, _, _ = _run({"qlik": client},
                    "return ds_clients['qlik'].execute_query(app='Sales', dimensions=['Region'], "
                    "measures=['Sum(Amount)'])")
    assert df["rows"].tolist() == [1]
    assert client.received == [{"app": "Sales", "dimensions": ["Region"], "measures": ["Sum(Amount)"]}]


class RecordingBigqueryClient(BigqueryClient):
    """BigQuery's real call signature, recording at the driver boundary."""

    def execute_query(self, sql, maximum_bytes_billed=None, use_query_cache=None):
        self.sent.append((sql, maximum_bytes_billed, use_query_cache))
        return pd.DataFrame([{"a": 1}])


def _bigquery(cap):
    client = object.__new__(RecordingBigqueryClient)
    client.maximum_bytes_billed = cap
    client.use_query_cache = False
    client.sent = []
    return client


@pytest.mark.parametrize("cap,extra", [
    (1000, "maximum_bytes_billed=500"),
    (1000, "maximum_bytes_billed=1000, use_query_cache=True"),
    (None, "maximum_bytes_billed=10**12"),
    (None, "use_query_cache=False"),
])
def test_bigquery_cost_options_within_the_connection_cap_are_sent(mode, cap, extra):
    client = _bigquery(cap)
    _run({"bq": client}, f"return ds_clients['bq'].execute_query('SELECT 1 AS a', {extra})")
    assert len(client.sent) == 1
    assert client.sent[0][0] == "SELECT 1 AS a"


@pytest.mark.parametrize("cap,extra", [
    (1000, "maximum_bytes_billed=5000"),
    (1000, "maximum_bytes_billed=0"),
    (1000, "maximum_bytes_billed=-1"),
    (1000, "maximum_bytes_billed=True"),
    (1000, "maximum_bytes_billed='500'"),
    (None, "maximum_bytes_billed=1.5"),
    (None, "use_query_cache='yes'"),
])
def test_bigquery_cost_options_outside_the_rules_are_refused(mode, cap, extra):
    client = _bigquery(cap)
    with pytest.raises(UnsafeSQLError) as refused:
        _run({"bq": client}, f"return ds_clients['bq'].execute_query('SELECT 1 AS a', {extra})")
    assert client.sent == []
    assert "Only SELECT" not in str(refused.value)


class _ClientRaising(SqliteClient):
    """A client whose own dispatch-point check refuses the statement."""

    def __init__(self, database, exc):
        super().__init__(database=database)
        self.exc = exc

    def execute_query(self, sql):
        raise self.exc


@pytest.mark.parametrize("exc,expected", [
    (ReadOnlyViolation("refused by the client"), UnsafeSQLError),
    (TableScopeViolation("table not enabled"), TableAccessError),
])
def test_violation_raised_inside_a_client_surfaces_as_the_normal_refusal(mode, shop, exc, expected):
    client = _ClientRaising(str(shop), exc)
    with pytest.raises(expected):
        _run({"shop": client}, "return ds_clients['shop'].execute_query('SELECT 1 AS a')")


# ---------------------------------------------------------------- Splunk

class _SplunkHttp:
    """The HTTP boundary (`pinned_request`): records search jobs, answers
    with a canned result."""

    def __init__(self):
        self.searches: list[str] = []

    def __call__(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/services/search/jobs"):
            self.searches.append(kwargs["data"]["search"])
            return _Resp({"results": [{"user": "sarah@example.com", "count": "3"}]})
        raise AssertionError(f"unexpected {method} {url}")


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def splunk(monkeypatch):
    http = _SplunkHttp()
    monkeypatch.setattr("app.data_sources.clients.splunk_client.pinned_request", http)
    return SplunkClient(host="splunk.example.com", api_token="t"), http


IDENTITY = [ParamSpec.model_validate({"name": "email", "type": "string", "source": "identity"})]


def test_splunk_envelope_is_captured_for_the_identity_check(mode, splunk):
    client, http = splunk
    scoped = 'search index=crm user="sarah@example.com" | stats count'
    _, _, captured = _run({"spl": client}, f"return ds_clients['spl'].execute_query({{'spl': {scoped!r}}})")
    assert http.searches == [scoped]
    assert [json.loads(q) for q in captured] == [{"spl": scoped}]
    assert verify_identity_binds_in_queries(captured, {"email": "sarah@example.com"}, IDENTITY) is None

    unscoped = "search index=crm | stats count"
    _, _, captured = _run({"spl": client}, f"return ds_clients['spl'].execute_query({{'spl': {unscoped!r}}})")
    assert verify_identity_binds_in_queries(captured, {"email": "sarah@example.com"}, IDENTITY) is not None


def test_splunk_write_hidden_in_a_json_envelope_is_refused(mode, splunk):
    client, http = splunk
    # The command name is JSON-escaped, so only the decoded SPL shows it.
    envelope = '{"spl": "search index=crm | \\\\u0064elete"}'
    with pytest.raises(UnsafeSQLError):
        _run({"spl": client}, f"return ds_clients['spl'].execute_query('{envelope}')")
    assert http.searches == []
