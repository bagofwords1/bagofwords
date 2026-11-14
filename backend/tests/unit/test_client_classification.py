"""A connector the read-only guard neither checks nor exempts takes no free text.

Contract: every client class production can build is classified `guarded`
(the wrapper checks its statements), `exempt` (its query surface cannot write
or run free-form statements, or it checks every statement where it sends it;
the reason names the file:line it rests on) or `unknown`. The wrapper refuses
a text statement to an `unknown` connector, so a connector added later
without a decision cannot receive free text unchecked. Keyword-only calls and
objects that are not connectors keep today's behaviour.
"""
from __future__ import annotations

import inspect
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd
import pytest

from app.ai.code_execution import client_profiles
from app.ai.code_execution.client_profiles import EXEMPT_CLIENTS, classify
from app.ai.code_execution.code_execution import StreamingCodeExecutor, UnsafeSQLError
from app.data_sources.clients.bow_client import BowClient
from app.data_sources.clients.discovery import DiscoveryClient
from tests.unit._client_enumeration import enumerated_client_classes

_LINUX = sys.platform.startswith("linux")
_UNIT_TESTS = Path(__file__).parent


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


def _run(clients, body: str):
    code = "def generate_df(ds_clients, excel_files):\n" + "".join(
        f"    {line}\n" for line in body.strip().splitlines()
    )
    return StreamingCodeExecutor().execute_code(code=code, ds_clients=clients, excel_files=[])


# ------------------------------------------------------------ the wrapper

class _UndeclaredConnector(DiscoveryClient):
    """A connector added without a guard profile or an exemption; records
    what reaches it (the driver boundary)."""

    def __init__(self):
        self.received: list = []

    def execute_query(self, query=None, **kwargs):
        self.received.append((query, kwargs))
        return pd.DataFrame([{"rows": 1}])


def test_an_undeclared_connector_is_unknown():
    assert classify(_UndeclaredConnector()) == "unknown"


def test_text_to_an_undeclared_connector_is_refused_and_nothing_is_sent(mode):
    client = _UndeclaredConnector()
    with pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
        _run({"c": client}, "return ds_clients['c'].execute_query('anything the source runs')")
    assert client.received == []


def test_text_built_at_runtime_to_an_undeclared_connector_is_refused(mode):
    client = _UndeclaredConnector()
    with pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
        _run({"c": client}, "return ds_clients['c'].query(bytes.fromhex('6869').decode())")
    assert client.received == []


def test_a_keyword_only_call_to_an_undeclared_connector_is_forwarded(mode):
    client = _UndeclaredConnector()
    df, _, _ = _run({"c": client}, "return ds_clients['c'].execute_query(table_name='t', limit=5)")
    assert df["rows"].tolist() == [1]
    assert client.received == [(None, {"table_name": "t", "limit": 5})]


def test_a_keyword_only_call_with_params_is_forwarded_without_them(mode):
    client = _UndeclaredConnector()
    df, _, _ = _run({"c": client}, "return ds_clients['c'].execute_query(table_name='t', params={'v': 'x'})")
    assert df["rows"].tolist() == [1]
    assert client.received == [(None, {"table_name": "t"})]


class _PlainObject:
    """Not a connector (a test or helper object): passed through as today."""

    def __init__(self):
        self.received: list = []

    def execute_query(self, sql):
        self.received.append(sql)
        return pd.DataFrame([{"rows": 1}])


def test_an_object_that_is_not_a_connector_receives_its_text_as_today(mode):
    client = _PlainObject()
    df, _, _ = _run({"c": client}, "return ds_clients['c'].execute_query('anything')")
    assert df["rows"].tolist() == [1]
    assert client.received == ["anything"]


# ------------------------------------------------------------ classify

def test_a_renderer_row_alone_neither_guards_nor_exempts(monkeypatch):
    from app.data_sources.clients.csv_client import CSVClient

    client = object.__new__(CSVClient)
    assert client_profiles.client_entry(client).renderer == "duckdb"
    assert classify(client) == "exempt"
    key = (CSVClient.__module__, CSVClient.__qualname__)
    monkeypatch.setattr(client_profiles, "EXEMPT_CLIENTS", {k: v for k, v in EXEMPT_CLIENTS.items() if k != key})
    assert classify(client) == "unknown"


def test_a_subclass_with_its_own_execute_query_must_be_declared_itself():
    from app.data_sources.clients.network_dir_client import NetworkDirClient

    class _Inherits(NetworkDirClient):
        pass

    class _Overrides(NetworkDirClient):
        def execute_query(self, query=None, **kwargs):
            raise AssertionError("never called")

    assert classify(object.__new__(_Inherits)) == "exempt"
    assert classify(object.__new__(_Overrides)) == "unknown"


def test_a_subclass_of_a_guarded_client_is_guarded():
    from app.data_sources.clients.sqlite_client import SqliteClient

    class _Sub(SqliteClient):
        def execute_query(self, sql):
            raise AssertionError("never called")

    assert classify(object.__new__(_Sub)) == "guarded"


# ------------------------------------------------------------ every class production builds

# Registry entries without a client_path resolve through the naming fallback.
_NAMING_FALLBACK = {
    "PostgresqlClient", "OracledbClient", "BigqueryClient", "NetsuiteClient", "MysqlClient", "AwsAthenaClient",
    "MariadbClient", "ClickhouseClient", "PinotClient", "DruidClient", "AwsCostClient", "VerticaClient",
    "AwsRedshiftClient", "SybaseClient",
}

# Classes that take free text with no guard and no read-only proof in their
# own code: refused any text statement until a decision is made for each.
_AWAITING_DECISION = {
    "InforEpmClient",  # caller MDX to an Infor process, infor_epm_client.py:457
    "OracleBIClient",  # caller Logical SQL, oracle_bi_client.py:270
    "SisenseClient",  # caller SQL to the ElastiCube SQL endpoint, sisense_client.py:469
    "TimbrA2aClient",  # caller prompt to a remote agent, timbr_a2a_client.py:105
    "OpenSearchClient",  # caller SQL to the SQL plugin, opensearch_client.py:421
}


@pytest.fixture(scope="module")
def classes():
    return enumerated_client_classes()


def test_the_naming_fallback_classes_are_enumerated(classes):
    names = {c.__name__ for c in classes}
    assert _NAMING_FALLBACK <= names, _NAMING_FALLBACK - names
    from app.schemas.data_source_registry import REGISTRY

    for key in ("postgresql", "oracledb", "bigquery", "netsuite", "mysql", "aws_athena", "mariadb",
                "clickhouse", "pinot", "druid", "aws_cost", "vertica", "aws_redshift", "sybase"):
        assert REGISTRY[key].client_path is None, key


def test_an_import_failure_fails_the_enumeration():
    from app.schemas import data_source_registry

    real = data_source_registry.resolve_client_class

    def failing(key):
        if key == "netsuite":
            raise ImportError("driver missing")
        return real(key)

    with patch.object(data_source_registry, "resolve_client_class", failing):
        with pytest.raises(ImportError, match="driver missing"):
            enumerated_client_classes()


def test_every_class_is_guarded_or_exempt_except_those_awaiting_a_decision(classes):
    verdicts = {cls.__name__: classify(object.__new__(cls)) for cls in classes + [BowClient]}
    assert len(verdicts) == len(classes) + 1
    assert set(verdicts.values()) <= {"guarded", "exempt", "unknown"}
    assert {name for name, v in verdicts.items() if v == "unknown"} == _AWAITING_DECISION


@pytest.mark.parametrize("name", sorted(_AWAITING_DECISION))
def test_a_class_awaiting_a_decision_is_refused_text_and_sent_nothing(mode, classes, name):
    cls = next(c for c in classes if c.__name__ == name)
    client = object.__new__(cls)
    with patch.object(cls, "execute_query", side_effect=AssertionError("sent")) as sent:
        with pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
            _run({"c": client}, "return ds_clients['c'].execute_query('SELECT 1')")
    assert not sent.called


def test_the_accelerated_client_is_exempt_as_checked_at_its_dispatch_point(classes):
    from app.data_sources.fast.fast_client import FastQueryClient

    assert FastQueryClient in classes
    assert classify(object.__new__(FastQueryClient)) == "exempt"
    assert EXEMPT_CLIENTS[("app.data_sources.fast.fast_client", "FastQueryClient")].startswith(
        "checked at the client's dispatch point")


_CATEGORIES = (
    "checked at the client's dispatch point",
    "read-only query language API",
    "structured spec mapped to read endpoints",
    "keyword-only API",
    "no execute_query text",
)


def test_every_exemption_names_its_category_and_a_file_line_of_the_clients_own_code(classes):
    by_key = {}
    for cls in classes + [BowClient]:
        for base in cls.__mro__:
            by_key.setdefault((base.__module__, base.__qualname__), []).append(base)
    for key, reason in EXEMPT_CLIENTS.items():
        assert key in by_key, f"declared for no class production builds: {key}"
        assert reason.strip() and reason.startswith(_CATEGORIES), (key, reason)
        cited = re.findall(r"\b(\w+\.py):(\d+)\b", reason)
        assert cited, (key, reason)
        sources = {Path(inspect.getsourcefile(c)) for c in by_key[key][0].__mro__ if c is not object}
        for name, line in cited:
            source = next((s for s in sources if s.name == name), None)
            assert source is not None, f"{key}: {name} is not the source of the class or its bases"
            assert 1 <= int(line) <= len(source.read_text().splitlines()), (key, name, line)


# Each exemption resting on a check at the dispatch point has a test that
# refuses a write through that client.
_DISPATCH_REFUSAL_TESTS = {
    "CSVClient": ("test_duckdb_file_lockdown.py", "test_each_connection_refuses_a_copy_onto_its_own_file"),
    "QVDClient": ("test_duckdb_file_lockdown.py", "test_each_connection_refuses_a_copy_onto_its_own_file"),
    "PBIXClient": ("test_duckdb_file_lockdown.py", "test_each_connection_refuses_a_copy_onto_its_own_file"),
    "PowerBIReportServerClient": ("test_duckdb_file_lockdown.py",
                                  "test_each_connection_refuses_a_copy_onto_its_own_file"),
    "FastQueryClient": ("test_client_classification.py",
                        "test_the_accelerated_client_refuses_writes_and_other_files_on_its_locked_engine"),
    "MongodbClient": ("test_non_sql_client_rules.py", "test_mongo_refuses_pipeline_outside_the_read_stages"),
    "AzureDataExplorerClient": ("test_non_sql_client_rules.py", "test_adx_refuses_management_commands"),
    "AwsCostClient": ("test_non_sql_client_rules.py", "test_aws_cost_refuses_operations_outside_reads"),
    "ZabbixClient": ("test_zabbix_client.py", "test_non_read_method_refused"),
    "XmlaClient": ("test_sap_bw_xmla_client.py", "test_non_read_statement_refused"),
    "AnalysisServicesClient": ("test_non_sql_client_rules.py", "test_xmla_refuses_statements_outside_the_read_verbs"),
    "PowerBIClient": ("test_powerbi_client.py", "test_non_guid_id_refused_in_execute_query"),
}


def test_every_dispatch_point_exemption_has_a_refusal_test():
    dispatch = {name for (_, name), reason in EXEMPT_CLIENTS.items()
                if reason.startswith("checked at the client's dispatch point")}
    assert dispatch == set(_DISPATCH_REFUSAL_TESTS)
    for name, (file, test) in _DISPATCH_REFUSAL_TESTS.items():
        assert re.search(rf"^\s*def {test}\(", (_UNIT_TESTS / file).read_text(), re.M), (name, file, test)


# ------------------------------------------------------------ the accelerated client's locked engine

@pytest.fixture
def custom_table(tmp_path):
    from app.data_sources.clients.sqlite_client import SqliteClient
    from app.data_sources.fast import artifacts, extractor
    from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

    source = tmp_path / "source.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE notes (id INTEGER, v TEXT)")
    con.executemany("INSERT INTO notes VALUES (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()
    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"):
        result = extractor.extract_to_artifact(SqliteClient(database=str(source)), "SELECT * FROM notes", "notes", "conn")
        yield FastQueryClient([FastRelation(
            name="notes", artifact_path=result.artifact_path, artifact_key=result.artifact_key,
            columns=[{"name": "id"}, {"name": "v"}],
        )])


def test_the_accelerated_client_refuses_writes_and_other_files_on_its_locked_engine(custom_table, tmp_path):
    assert custom_table.execute_query("SELECT id FROM notes ORDER BY id")["id"].tolist() == [1, 2]

    copied = tmp_path / "copied.csv"
    with pytest.raises(Exception, match="(?i)external access|disabled|permission"):
        custom_table.execute_query(f"COPY (SELECT * FROM notes) TO '{copied}'")
    assert not copied.exists()

    other = tmp_path / "other.duckdb"
    duckdb.connect(str(other)).close()
    with pytest.raises(Exception, match="(?i)external access|disabled|permission"):
        custom_table.execute_query(f"ATTACH '{other}' AS other")

    artifact = Path(custom_table.relations[0].artifact_path)
    before = artifact.read_bytes()
    with pytest.raises(Exception, match="(?i)external access|disabled|permission"):
        custom_table.execute_query(f"COPY (SELECT 1 AS id, 'x' AS v) TO '{artifact}'")
    assert artifact.read_bytes() == before
    assert custom_table.execute_query("SELECT count(*) AS n FROM notes")["n"].tolist() == [2]
