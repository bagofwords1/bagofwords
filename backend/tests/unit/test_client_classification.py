"""A connector the read-only guard neither checks nor exempts takes no free text.

Contract: every client class production can build is classified `guarded`
(the wrapper checks its statements), `exempt` (its query surface cannot write
or run free-form statements, or it checks every statement where it sends it;
the reason names the file:line it rests on) or `unknown`. The wrapper refuses
a statement of any type, passed by position or by name, to an `unknown`
connector, so a connector added later without a decision cannot receive free
text unchecked. A class exempt only through a parent's declaration needs a
decision of its own (`_INHERITS_EXEMPTION`). Keyword-only calls and objects
that are not connectors keep today's behaviour.
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
from app.ai.code_execution.query_params import ParamError
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


class _SqlNamedConnector(DiscoveryClient):
    """An undeclared connector whose statement parameter has its own name
    and a second parameter that can be passed by position."""

    def __init__(self):
        self.received: list = []

    def execute_query(self, sql=None, extra=None):
        self.received.append((sql, extra))
        return pd.DataFrame([{"rows": 1}])


# Every way generated code can hand an undeclared connector a statement:
# whatever its type, by position or by the parameter's own name. `{first}`
# is the connector's first parameter.
_STATEMENT_SHAPES = {
    "str": "execute_query('anything the source runs')",
    "list": "execute_query(['anything the source runs'])",
    "tuple": "execute_query(('anything the source runs',))",
    "dict": "execute_query({{'q': 'anything the source runs'}})",
    "bytes": "execute_query(b'anything the source runs')",
    "number": "execute_query(1)",
    "first parameter by name": "execute_query({first}='anything the source runs')",
    "later positional": "execute_query(None, 'anything the source runs')",
    "alias": "query({first}='anything the source runs')",
}


@pytest.mark.parametrize("connector", [_UndeclaredConnector, _SqlNamedConnector])
@pytest.mark.parametrize("shape", list(_STATEMENT_SHAPES.values()), ids=list(_STATEMENT_SHAPES))
def test_a_statement_of_any_shape_to_an_undeclared_connector_is_refused(mode, connector, shape):
    client = connector()
    first = next(iter(inspect.signature(client.execute_query).parameters))
    with pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
        _run({"c": client}, "return ds_clients['c']." + shape.format(first=first))
    assert client.received == []


@pytest.mark.parametrize("call", [
    "execute_query(extra='anything the source runs')",
    # The params of a keyword call are not rendered (there is no positional
    # statement); the statement under the client's own name is still refused.
    "execute_query(sql='a = :v', params={'v': 'x'})",
])
def test_a_statement_by_another_parameter_name_to_an_undeclared_connector_is_refused(mode, call):
    client = _SqlNamedConnector()
    with pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
        _run({"c": client}, "return ds_clients['c']." + call)
    assert client.received == []


def test_a_call_with_no_statement_to_an_undeclared_connector_is_forwarded(mode):
    client = _SqlNamedConnector()
    df, _, _ = _run({"c": client}, "return ds_clients['c'].execute_query()")
    assert df["rows"].tolist() == [1]
    assert client.received == [(None, None)]


def test_a_keyword_only_call_to_an_undeclared_connector_is_forwarded(mode):
    client = _UndeclaredConnector()
    df, _, _ = _run({"c": client}, "return ds_clients['c'].execute_query(table_name='t', limit=5)")
    assert df["rows"].tolist() == [1]
    assert client.received == [(None, {"table_name": "t", "limit": 5})]


def test_a_keyword_only_call_with_params_is_refused(mode):
    # Nothing renders params into a keyword call: it would run with its
    # placeholders as written, so it is refused and nothing is sent.
    client = _UndeclaredConnector()
    with pytest.raises(ParamError, match="rendered only into a query string"):
        _run({"c": client}, "return ds_clients['c'].execute_query(table_name='t', params={'v': 'x'})")
    assert client.received == []


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
    "PostgresqlClient", "OracledbClient", "BigqueryClient", "NetsuiteClient", "MysqlClient",
    "MariadbClient", "ClickhouseClient", "PinotClient", "DruidClient", "AwsCostClient", "VerticaClient",
    "AwsRedshiftClient", "SybaseClient",
}

# Classes that take free text with no guard and no read-only proof in their
# own code: refused any text statement until a decision is made for each.
# CQL (Cassandra) and PartiQL (DynamoDB) can write, SQL++ (Couchbase) is
# read-only only by a server option; each client checks its text with a
# pattern that is not a reading of its engine's grammar
# (_document_schema.ensure_single_select).
_AWAITING_DECISION: set[str] = {
    "CassandraClient", "CouchbaseClient", "DynamodbClient",
    # New on main (search text and message ids), not reviewed yet.
    "SlackMessagesClient", "ExchangeOnpremClient",
}


@pytest.fixture(scope="module")
def classes():
    return enumerated_client_classes()


def test_the_naming_fallback_classes_are_enumerated(classes):
    names = {c.__name__ for c in classes}
    assert _NAMING_FALLBACK <= names, _NAMING_FALLBACK - names
    from app.schemas.data_source_registry import REGISTRY

    for key in ("postgresql", "oracledb", "bigquery", "netsuite", "mysql", "mariadb",
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


@pytest.mark.parametrize("module, name", [
    ("cassandra_client", "CassandraClient"),
    ("couchbase_client", "CouchbaseClient"),
    ("dynamodb_client", "DynamodbClient"),
])
def test_a_class_awaiting_a_decision_is_refused_its_text_and_sent_nothing(mode, module, name):
    import importlib

    assert name in _AWAITING_DECISION
    cls = getattr(importlib.import_module(f"app.data_sources.clients.{module}"), name)
    received: list = []
    client = object.__new__(cls)
    client.execute_query = lambda *a, **k: received.append((a, k))  # the driver boundary
    with pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
        _run({"c": client}, "return ds_clients['c'].execute_query('SELECT * FROM t')")
    assert received == []


# A class that is exempt only through a parent's declaration. Its own code
# may change what the parent's execute_query sends (a helper it overrides),
# so each one is a decision, keyed by (module, qualname): the parent whose
# declaration it inherits, every name defined by the classes of its MRO that
# are not in that parent's MRO, constructors and attribute hooks included
# (`_overrides`: a new override fails until it is decided
# again), and what those overrides do, with the file:line that shows it.
_C = "app.data_sources.clients"
_GIT_REPO_OVERRIDES = frozenset({
    "__init__", "_checkout", "_excluded", "_is_junk", "_iter_files", "_repo_url", "_resolve", "_root", "_root_policy_exempt",
    "capabilities", "cheap_live_listing", "default_host", "default_username", "grep_files", "provider",
    "read_file", "refresh", "test_connection",
})
_GIT_REPO = ("NetworkDirClient", _GIT_REPO_OVERRIDES,
             "through GitRepoClient: __init__ builds a NetworkDirClient with no root and writable=False "
             "(git_repo_client.py:72), read_file is NetworkDirClient's with its result scrubbed "
             "(git_repo_client.py:184), _resolve only narrows the path inside the clone (git_repo_client.py:171), "
             "_root clones the configured repository (git_repo_client.py:138)")
_NO_EXECUTE_QUERY = "defines no execute_query; its tools run through call_tool, not the query path"
_REMOTE_SERVER_OVERRIDES = frozenset({
    "__init__", "_auth_workspace", "_inspect", "_inspect_agentless", "_pool_fingerprint", "_shell", "_ssh", "_ssh_session",
    "_validate", "acall_authorized", "acall_human_approved", "call_tool", "config_class", "credential_classes",
    "list_tools", "server_url", "test_connection",
})
_OBJECT_STORE_TRANSPORT = frozenset({
    "__init__", "_client", "_download", "_head", "_is_not_found", "_iter_list", "_open_body", "_probe",
    "bucket_noun", "metadata_key", "scheme",
})
_INHERITS_EXEMPTION: dict[tuple[str, str], tuple[str, frozenset[str], str]] = {
    (f"{_C}.s3_client", "S3Client"): (
        "ObjectStoreClient", _OBJECT_STORE_TRANSPORT,
        "overrides construction and the storage transport only: __init__ stores the bucket settings "
        "(s3_client.py:35), objects are read with get_object (s3_client.py:148, s3_client.py:180) and "
        "listed with list_objects_v2 (s3_client.py:196); ObjectStoreClient.execute_query reads one object"),
    (f"{_C}.gcs_client", "GCSClient"): (
        "ObjectStoreClient", _OBJECT_STORE_TRANSPORT | {"_bucket", "_get_blob"},
        "overrides construction and the storage transport only: __init__ stores the bucket settings "
        "(gcs_client.py:32), objects are read with download_as_bytes and open('rb') (gcs_client.py:144, "
        "gcs_client.py:165) and listed with list_blobs (gcs_client.py:114); "
        "ObjectStoreClient.execute_query reads one object"),
    (f"{_C}.azure_blob_client", "AzureBlobClient"): (
        "ObjectStoreClient", _OBJECT_STORE_TRANSPORT | {"_resolved_account_url", "_scope_uri", "_web_url", "container"},
        "overrides construction and the storage transport only: __init__ stores the container settings "
        "(azure_blob_client.py:32), blobs are read with download_blob and get_blob_properties "
        "(azure_blob_client.py:170, azure_blob_client.py:155) and listed with list_blobs "
        "(azure_blob_client.py:140); ObjectStoreClient.execute_query reads one object"),
    (f"{_C}.sap_bw_xmla_client", "SapBwXmlaClient"): (
        "XmlaClient",
        frozenset({"DEFAULT_XMLA_PATH", "EMPTY_NOTE", "META_KEY", "PATH_HINT", "PRODUCT_NAME", "QUERY_REQUIRED_MSG",
                   "__init__", "_build_endpoint", "description", "system_prompt"}),
        "overrides __init__, which builds the endpoint and hands it to XmlaClient's "
        "(sap_bw_xmla_client.py:46), the endpoint URL "
        "(sap_bw_xmla_client.py:84) and prompt text (sap_bw_xmla_client.py:110) only"),
    (f"{_C}.infor_olap_client", "InforOlapClient"): (
        "XmlaClient",
        frozenset({"EMPTY_NOTE", "META_KEY", "PATH_HINT", "PRODUCT_NAME", "QUERY_REQUIRED_MSG", "__init__",
                   "_connect_gateway", "_discover_datasources", "_ensure_gateway_token", "_pick_datasource",
                   "_property_list_xml", "_resolve_worker_url", "_rewrite_host", "_soap_call", "connect",
                   "description", "system_prompt", "test_connection"}),
        "overrides construction and the transport only: __init__ runs XmlaClient's and stores "
        "the gateway settings (infor_olap_client.py:51), connect (infor_olap_client.py:95), _soap_call "
        "adding a gateway token to the body it is given (infor_olap_client.py:153), "
        "request context (infor_olap_client.py:217); XmlaClient.execute_query checks "
        "the statement before any of them runs"),
    (f"{_C}.git_repo_client", "GitHubRepoClient"): _GIT_REPO,
    (f"{_C}.git_repo_client", "GitLabRepoClient"): _GIT_REPO,
    (f"{_C}.git_repo_client", "BitbucketRepoClient"): _GIT_REPO,
    (f"{_C}.graph_drive_client", "SharepointClient"): (
        "GraphDriveClient",
        frozenset({"__init__", "_iter_listing_files", "alist_files_bounded", "list_files_bounded"}),
        "__init__ presets the mode (graph_drive_client.py:1563) and the listing reads folders by GET "
        "(graph_drive_client.py:1567); read_file is GraphDriveClient's"),
    (f"{_C}.graph_drive_client", "OnedriveClient"): (
        "GraphDriveClient", frozenset({"__init__"}),
        "__init__ presets the mode and recursion only (graph_drive_client.py:1638)"),
    (f"{_C}.graph_onenote_client", "OnenoteClient"): (
        "GraphDriveClient",
        frozenset({"__init__", "_base", "_by_id", "_enforce_page_scope", "_get_all", "_group_chain",
                   "_has_user_token", "_page_html", "_page_media", "_prior_meta", "_public", "_resource_bytes",
                   "_section_groups", "_section_pages", "_sections", "_walk", "capabilities",
                   "catalog_identity_available", "description", "file_version", "get_schema", "get_schemas",
                   "grep_files", "is_document_based", "list_files", "page_text", "read_file", "read_raw_bytes",
                   "search_files", "test_connection"}),
        "through GraphOneNoteClient: __init__ presets the mode and index tier and picks "
        "the notebook source (graph_onenote_client.py:143), read_file reads one page by GET "
        "(graph_onenote_client.py:520, graph_onenote_client.py:428), get_schemas "
        "lists pages (graph_onenote_client.py:717)"),
    (f"{_C}.graph_mail_client", "GraphMailClient"): (
        "GraphDriveClient",
        frozenset({"__init__", "_addr", "_collect", "_folder_path_by_id", "_messages_base", "_msg_to_item",
                   "capabilities", "get_schemas", "list_files", "list_mail_folders", "list_messages", "read_file",
                   "read_raw_bytes", "resolve_mail_folder", "search_files", "search_messages", "test_connection"}),
        "__init__ presets the mode (graph_mail_client.py:72), "
        "read_file reads one message by GET (graph_mail_client.py:344), "
        "get_schemas returns no tables (graph_mail_client.py:373)"),
    (f"{_C}.graph_teams_client", "GraphTeamsClient"): (
        "GraphDriveClient",
        frozenset({"__init__", "_annotate_chat", "_annotate_thread", "_channel_base", "_channel_threads", "_chat_row",
                   "_context_line", "_conv_name", "_conversation_by_id", "_conversation_rows", "_find_root",
                   "_folder", "_hit_row", "_is_user_message", "_read_channel_thread", "_read_chat_window",
                   "_read_recent", "_row", "_search_page", "_search_walk", "_sender", "_text", "_whoami",
                   "capabilities", "description", "get_schemas", "list_conversations", "list_files",
                   "list_messages", "read_file", "read_raw_bytes", "resolve_conversation", "search_files",
                   "search_messages", "test_connection"}),
        "__init__ presets the mode and empty caches (graph_teams_client.py:147), "
        "read_file reads one thread or chat window by GET "
        "(graph_teams_client.py:798, graph_teams_client.py:714), get_schemas "
        "returns no tables (graph_teams_client.py:824)"),
    (f"{_C}.mcp_client", "McpClient"): (
        "ToolProviderClient",
        frozenset({"_MAX_RESOURCES", "__init__", "_acall_tool", "_alist_tools", "_atest_connection", "_block_part",
                   "_build_headers", "_call_once", "_call_session", "_connect", "_detect_content_type",
                   "_extract_binaries", "_extract_error_message", "_extract_result_data", "_merge_structured",
                   "_parse_text", "_unwrap_exception", "acall_tool", "alist_resource_templates", "alist_resources",
                   "alist_tools", "aread_resource", "atest_connection", "call_tool", "list_tools",
                   "test_connection"}),
        f"{_NO_EXECUTE_QUERY} (mcp_client.py:254); __init__ stores the connection settings (mcp_client.py:261)"),
    (f"{_C}.custom_api_client", "CustomApiClient"): (
        "ToolProviderClient",
        frozenset({"_BEARER_AUTH_TYPES", "_BODYLESS_METHODS", "_DEFAULT_TIMEOUT_S", "_MAGIC", "_MAX_TIMEOUT_S",
                   "_PER_USER_OAUTH_TYPES", "_WRITE_METHODS", "__init__", "_build_headers", "_build_result", "_csrf_rejected",
                   "_detect_content_type", "_endpoint_default_policy", "_extract_error_message",
                   "_fetch_csrf_token", "_find_endpoint", "_is_pinned", "_is_texty", "_param_json_schema",
                   "_prepare_request", "_sniff_mime", "_timeout_for", "call_tool", "list_tools", "server_url",
                   "test_connection"}),
        f"{_NO_EXECUTE_QUERY} (custom_api_client.py:45); __init__ stores the endpoint settings "
        "(custom_api_client.py:69)"),
    (f"{_C}.remote_server_client", "RemoteServerClient"): (
        "ToolProviderClient", _REMOTE_SERVER_OVERRIDES,
        f"{_NO_EXECUTE_QUERY} (remote_server_client.py:37); __init__ validates the connection settings "
        "(remote_server_client.py:46)"),
    (f"{_C}.remote_server_client", "RemoteSMBClient"): (
        "ToolProviderClient", _REMOTE_SERVER_OVERRIDES,
        f"{_NO_EXECUTE_QUERY} (remote_server_client.py:452); __init__ presets NTLM auth and runs "
        "RemoteServerClient's (remote_server_client.py:456, remote_server_client.py:46)"),
}


def _declared_owner(cls):
    """The class in `cls`'s MRO whose EXEMPT_CLIENTS declaration it relies on."""
    return next((base for base in cls.__mro__ if (base.__module__, base.__qualname__) in EXEMPT_CLIENTS), None)


# Names a class body or ABCMeta sets that change no behaviour of an instance.
_INERT_NAMES = frozenset({
    "__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__annotations__", "__firstlineno__",
    "__static_attributes__", "__abstractmethods__", "__orig_bases__", "__parameters__", "_abc_impl",
})


def _overrides(cls) -> frozenset[str]:
    """Every name defined by a class in `cls`'s MRO that is not in its
    declared owner's MRO (mixins listed after the owner included), but the
    inert ones: `__init__`, `__getattr__` and `__getattribute__` count."""
    owner_mro = set(_declared_owner(cls).__mro__)
    return frozenset(name for base in cls.__mro__ if base not in owner_mro
                     for name in vars(base) if name not in _INERT_NAMES)


def _undecided_inheritors(classes, decided) -> set[tuple[str, str]]:
    """The exempt classes with no declaration of their own and no decision,
    or whose overrides are not the ones decided."""
    return {(cls.__module__, cls.__qualname__) for cls in classes
            if classify(object.__new__(cls)) == "exempt"
            and (cls.__module__, cls.__qualname__) not in EXEMPT_CLIENTS
            and ((cls.__module__, cls.__qualname__) not in decided
                 or decided[(cls.__module__, cls.__qualname__)][1] != _overrides(cls))}


def test_every_class_exempt_through_a_parent_is_decided(classes):
    assert _undecided_inheritors(classes + [BowClient], _INHERITS_EXEMPTION) == set()
    by_key = {(cls.__module__, cls.__qualname__): cls for cls in classes}
    for key, (parent, _, grounding) in _INHERITS_EXEMPTION.items():
        name = key[1]
        cls = by_key.get(key)
        assert cls is not None, f"declared for no class production builds: {key}"
        assert key not in EXEMPT_CLIENTS, f"{name} has its own declaration"
        owner = _declared_owner(cls)
        assert owner is not None and owner.__name__ == parent, (name, parent, owner)
        # Grounded in the subclass's own source: where its overrides are.
        source = Path(inspect.getsourcefile(cls))
        lines = [int(line) for file, line in re.findall(r"\b(\w+\.py):(\d+)\b", grounding) if file == source.name]
        assert lines, f"{name}: the grounding must cite {source.name}"
        assert all(1 <= line <= len(source.read_text().splitlines()) for line in lines), (name, lines)


def test_a_decided_class_that_gains_an_override_needs_a_new_decision():
    # A subclass standing in for GitHubRepoClient after an edit that adds a
    # helper: same module and name, one more override than was decided.
    from app.data_sources.clients.git_repo_client import GitHubRepoClient

    edited = type("GitHubRepoClient", (GitHubRepoClient,), {
        "__module__": GitHubRepoClient.__module__,
        "_list_refs": lambda self: [],
    })
    assert _undecided_inheritors([edited], _INHERITS_EXEMPTION) == {(GitHubRepoClient.__module__, "GitHubRepoClient")}
    unchanged = type("GitHubRepoClient", (GitHubRepoClient,), {"__module__": GitHubRepoClient.__module__})
    assert _undecided_inheritors([unchanged], _INHERITS_EXEMPTION) == set()


def _edited_git_hub(bases=(), **attrs):
    """A class standing in for GitHubRepoClient after an edit: same module
    and name, the given extra bases after it and the given definitions."""
    from app.data_sources.clients.git_repo_client import GitHubRepoClient

    return type("GitHubRepoClient", (GitHubRepoClient, *bases), {"__module__": GitHubRepoClient.__module__, **attrs})


_GIT_HUB_KEY = (f"{_C}.git_repo_client", "GitHubRepoClient")


def test_a_decided_class_that_gains_an_attribute_hook_needs_a_new_decision():
    edited = _edited_git_hub(__getattribute__=lambda self, name: object.__getattribute__(self, name))
    assert _undecided_inheritors([edited], _INHERITS_EXEMPTION) == {_GIT_HUB_KEY}
    edited = _edited_git_hub(__getattr__=lambda self, name: None)
    assert _undecided_inheritors([edited], _INHERITS_EXEMPTION) == {_GIT_HUB_KEY}


def test_a_decided_class_that_gains_a_constructor_needs_a_new_decision():
    # Every decided class already defines __init__, so the decision here is
    # one made for a subclass that overrode nothing.
    from app.data_sources.clients.network_dir_client import NetworkDirClient

    class _Preset(NetworkDirClient):
        pass

    key = (_Preset.__module__, _Preset.__qualname__)
    decided = {key: ("NetworkDirClient", frozenset(), "overrides nothing")}
    assert _undecided_inheritors([_Preset], decided) == set()
    _Preset.__init__ = lambda self, *args, **kwargs: None
    assert _undecided_inheritors([_Preset], decided) == {key}


def test_a_decided_class_that_gains_a_mixin_after_its_owner_needs_a_new_decision():
    class _Mixin:
        def _list_refs(self):
            return []

    edited = _edited_git_hub(bases=(_Mixin,))
    assert edited.__mro__.index(_Mixin) > edited.__mro__.index(_declared_owner(edited))
    assert _undecided_inheritors([edited], _INHERITS_EXEMPTION) == {_GIT_HUB_KEY}


def test_a_new_subclass_of_an_exempt_class_needs_a_decision():
    from app.data_sources.clients.network_dir_client import NetworkDirClient

    class _NewSubclass(NetworkDirClient):
        def read_file(self, *args, **kwargs):
            raise AssertionError("never called")

    assert _undecided_inheritors([_NewSubclass], _INHERITS_EXEMPTION) == {
        (_NewSubclass.__module__, _NewSubclass.__qualname__)}


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
    "remote agent with SQL validation enforced",
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


# What the cited lines of the DuckDB exemptions hold, in the order cited.
_CITED_CALLS = {
    "CSVClient": ("check_duckdb_file_query(",),
    "QVDClient": ("check_duckdb_file_query(",),
    "PBIXClient": ("check_duckdb_file_query(",),
    "PowerBIReportServerClient": ("check_duckdb_file_query(",),
    "FastQueryClient": ("lock_query_session(con)", "assert_no_blocked_introspection(sql)", "extract_statements(sql)",
                        "assert_no_blocked_calls(sql)"),
    "NetworkDirClient": ("def read_file(", "reads_as_statements(query)", "query_tables(self, query)"),
}


def test_the_duckdb_exemptions_cite_the_lines_that_lock_and_check(classes):
    by_name = {cls.__qualname__: cls for cls in classes}
    for (_module, name), reason in EXEMPT_CLIENTS.items():
        if name not in _CITED_CALLS:
            continue
        source = Path(inspect.getsourcefile(by_name[name]))
        lines = source.read_text().splitlines()
        cited = [lines[int(line) - 1] for file, line in re.findall(r"\b(\w+\.py):(\d+)\b", reason)
                 if file == source.name]
        assert len(cited) == len(_CITED_CALLS[name]), (name, reason)
        for text, call in zip(cited, _CITED_CALLS[name]):
            assert call in text, (name, call, text)


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
    "InforEpmClient": ("test_non_sql_client_rules.py", "test_infor_epm_refuses_statements_outside_one_read"),
    "OracleBIClient": ("test_non_sql_client_rules.py", "test_oracle_bi_refuses_statements_outside_one_select"),
    "OpenSearchClient": ("test_non_sql_client_rules.py",
                         "test_opensearch_refuses_sql_outside_one_select_and_unknown_envelope_keys"),
    "SisenseClient": ("test_non_sql_client_rules.py", "test_sisense_refuses_sql_outside_one_select"),
    "NetworkDirClient": ("test_client_classification.py",
                         "test_the_network_directory_refuses_sql_outside_one_select_over_its_selected_tables"),
}


def test_the_remote_agent_is_exempt_only_with_sql_validation_enforced(classes):
    from app.data_sources.clients.timbr_a2a_client import TimbrA2aClient

    assert TimbrA2aClient in classes
    assert classify(object.__new__(TimbrA2aClient)) == "exempt"
    reason = EXEMPT_CLIENTS[(TimbrA2aClient.__module__, TimbrA2aClient.__qualname__)]
    assert reason.startswith("remote agent with SQL validation enforced") and "should-validate-sql" in reason
    assert {name for (_, name), r in EXEMPT_CLIENTS.items()
            if r.startswith("remote agent with SQL validation enforced")} == {"TimbrA2aClient"}
    tests = (_UNIT_TESTS / "test_non_sql_client_rules.py").read_text()
    assert re.search(r"^def test_timbr_a2a_refuses_a_prompt_without_sql_validation\(", tests, re.M)


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

    # Statements other than a SELECT are refused before they run; a SELECT
    # reading a file outside the session meets the locked engine.
    refused = "(?i)external access|disabled|permission|one read-only SELECT"
    copied = tmp_path / "copied.csv"
    with pytest.raises(Exception, match=refused):
        custom_table.execute_query(f"COPY (SELECT * FROM notes) TO '{copied}'")
    assert not copied.exists()

    other = tmp_path / "other.duckdb"
    duckdb.connect(str(other)).close()
    with pytest.raises(Exception, match=refused):
        custom_table.execute_query(f"ATTACH '{other}' AS other")

    outside = tmp_path / "outside.csv"
    outside.write_text("id\n7\n")
    with pytest.raises(Exception, match="(?i)external access|disabled|permission"):
        custom_table.execute_query(f"SELECT * FROM read_csv('{outside}')")

    artifact = Path(custom_table.relations[0].artifact_path)
    before = artifact.read_bytes()
    with pytest.raises(Exception, match=refused):
        custom_table.execute_query(f"COPY (SELECT 1 AS id, 'x' AS v) TO '{artifact}'")
    assert artifact.read_bytes() == before
    assert custom_table.execute_query("SELECT count(*) AS n FROM notes")["n"].tolist() == [2]


# ------------------------------------------------------------ the network directory's indexed file tables

def test_the_network_directory_refuses_sql_outside_one_select_over_its_selected_tables(tmp_path, monkeypatch):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation
    from app.data_sources.clients._network_dir_tables import attach_catalog
    from app.data_sources.clients.network_dir_client import NetworkDirClient
    from app.services import storage

    monkeypatch.setattr(storage, "UPLOADS_ROOT", tmp_path / "cache")
    root = tmp_path / "share"
    root.mkdir()
    (root / "notes.txt").write_text("incident timeline")
    client = NetworkDirClient(root_path=str(root), index_mode="content")
    assert client.index_file_tables

    # Nothing selected for the agent: no SQL runs at all.
    with pytest.raises(PermissionError):
        client.execute_query("SELECT 1 AS n")

    # Only the coverage table is selected; the packet table is not.
    attach_catalog(client, [("indexed_files", [], {})], [])
    assert client.execute_query("SELECT count(*) AS n FROM indexed_files")["n"].tolist() == [0]
    with pytest.raises(duckdb.CatalogException):
        client.execute_query("SELECT * FROM packets")
    for refused in (
        "SELECT * FROM read_csv_auto('notes.txt')",
        "SELECT 1 AS n; SELECT 2 AS n",
        "COPY (SELECT 1 AS n) TO 'out.csv'",
        "SELECT getenv('HOME') AS h",
    ):
        with pytest.raises(ReadOnlyViolation):
            client.execute_query(refused)
    assert not (root / "out.csv").exists()

    # A file named as a table: the text checks refuse it (the engine-tree
    # check reads it as a string in a relation position), and with them out
    # of the way the locked engine still stops it.
    (root / "data.csv").write_text("v\n1\n")
    path = str(root / "data.csv").replace("'", "''")
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(f"SELECT * FROM '{path}'")
    from app.ai.code_execution import sql_guard

    with monkeypatch.context() as unchecked:
        unchecked.setattr(sql_guard, "check_sql", lambda sql, dialect, **kwargs: None)
        with pytest.raises(duckdb.PermissionException):
            client.execute_query(f"SELECT * FROM '{path}'")

    # Settings stay out of reach; the documented discovery reads still work.
    for refused in ("SELECT name, setting FROM pg_settings","SELECT * FROM pg_catalog.pg_settings"):
        with pytest.raises(ValueError, match="introspection"):
            client.execute_query(refused)
    assert "file_id" in client.execute_query(
        "SELECT column_name FROM duckdb_columns() WHERE table_name = 'indexed_files'")["column_name"].tolist()
    assert "file_id" in client.execute_query("DESCRIBE indexed_files")["column_name"].tolist()


def test_the_network_directory_engine_does_not_read_python_objects(tmp_path, monkeypatch):
    from app.data_sources.clients import _network_dir_tables
    from app.data_sources.clients._network_dir_tables import attach_catalog
    from app.data_sources.clients.network_dir_client import NetworkDirClient
    from app.services import storage

    monkeypatch.setattr(storage, "UPLOADS_ROOT", tmp_path / "cache")
    root = tmp_path / "share"
    root.mkdir()
    client = NetworkDirClient(root_path=str(root), index_mode="content")
    attach_catalog(client, [("indexed_files", [], {})], [])
    # A frame the module running the query can see passes the text checks.
    monkeypatch.setattr(_network_dir_tables, "visible_frame", pd.DataFrame({"v": [1]}), raising=False)
    with pytest.raises(duckdb.CatalogException):
        client.execute_query("SELECT * FROM visible_frame")
