"""One Athena connection can index many Glue databases without changing single-database ones.

Contract: a plain `database` keeps bare table names, no table metadata and the
database as the query context. A list/glob spec indexes `<db>.<table>` for each
matching Glue database, never has a default database (an unqualified table
reference is refused before Athena sees it), reports databases it could not read
through `index_stats()`, and hands only databases that are *gone* (not merely
unreadable) to pruning. Only boto3's Glue client and awswrangler are faked.
"""

import inspect

import awswrangler as real_wr
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.data_sources.clients import aws_athena_client as athena_module
from app.data_sources.clients.aws_athena_client import AwsAthenaClient
from app.data_sources.clients.progress import IndexingCancelled
from app.utils import safe_regex


def _glue_error(code):
    return ClientError({"Error": {"Code": code, "Message": f"{code} happened"}}, "GetTables")


def _glue_table(name):
    return {
        "Name": name,
        "StorageDescriptor": {"Columns": [{"Name": "id", "Type": "int"}]},
        "PartitionKeys": [{"Name": "dt", "Type": "string"}],
    }


class _Paginator:
    def __init__(self, pages, error_on_page=None):
        self._pages = pages
        self._error_on_page = error_on_page

    def paginate(self, **kwargs):
        for i, page in enumerate(self._pages):
            if self._error_on_page == i:
                raise _glue_error("ThrottlingException")
            yield page


class FakeGlue:
    """`databases` maps a name to table names, or to an exception raised while reading it."""

    def __init__(self, databases, fail_database_page=None, forbid_database_listing=False):
        self.databases = databases
        self.fail_database_page = fail_database_page
        self.forbid_database_listing = forbid_database_listing

    def get_paginator(self, op):
        if op == "get_databases":
            if self.forbid_database_listing:
                raise AssertionError("get_databases must not be called")
            names = list(self.databases)
            pages = [{"DatabaseList": [{"Name": n} for n in names[i:i + 2]]} for i in range(0, len(names), 2)]
            return _Paginator(pages or [{"DatabaseList": []}], self.fail_database_page)
        assert op == "get_tables"
        glue = self

        class _Tables:
            def paginate(self, DatabaseName):
                value = glue.databases[DatabaseName]
                if isinstance(value, Exception):
                    raise value
                for i in range(0, max(len(value), 1), 2):
                    yield {"TableList": [_glue_table(n) for n in value[i:i + 2]]}

        return _Tables()


class FakeWrangler:
    def __init__(self):
        self.calls = []

    class _Athena:
        def __init__(self, outer):
            self.outer = outer

        def read_sql_query(self, **kwargs):
            inspect.signature(real_wr.athena.read_sql_query).bind(**kwargs)
            self.outer.calls.append(kwargs)
            import pandas as pd
            return pd.DataFrame({"n": [1]})

    @property
    def athena(self):
        return FakeWrangler._Athena(self)


@pytest.fixture
def wrangler(monkeypatch):
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    fake = FakeWrangler()
    monkeypatch.setattr(athena_module, "wr", fake)
    return fake


def make_client(database, glue, **kwargs):
    client = AwsAthenaClient(region="eu-west-1", database=database, access_key="k", secret_key="s", **kwargs)
    client.glue_client = glue
    return client


TENANTS = {
    "acme_db": ["assets", "users"],
    "globex_db": ["assets", "alerts", "day_01", "day_07_" + "a" * 32, "daily_x", "day"],
    "billing_db": ["costs"],
    "analytics_history": ["general"],
}


def names(tables):
    return sorted(t.name for t in tables)



def test_stored_single_database_config_indexes_bare_names_without_metadata(wrangler):
    client = make_client("sales_x", FakeGlue({"sales_x": ["orders", "refunds", "day_01"]}))
    tables = client.get_tables()
    assert names(tables) == ["day_01", "orders", "refunds"]
    assert all(t.metadata_json is None for t in tables)
    assert [c.name for c in tables[0].columns] == ["id", "dt"]
    assert client.unindexed_table_prefixes() == []
    assert client.index_stats() == {}


def test_single_mode_sends_its_database_as_query_context(wrangler):
    client = make_client("sales_x", FakeGlue({"sales_x": ["orders"]}))
    client.execute_query("SELECT * FROM orders")
    assert wrangler.calls[0]["database"] == "sales_x"


def test_single_mode_with_exclude_pattern_drops_matches_and_count_agrees(wrangler):
    glue = FakeGlue({"sales_x": ["orders", "day_01", "day_22", "daily_x"]})
    client = make_client("sales_x", glue, table_exclude_pattern=r"^day_\d{2}")
    tables = client.get_tables()
    assert names(tables) == ["daily_x", "orders"]
    assert client.count_tables() == len(tables)


def test_connection_test_binds_the_real_wrangler_signature_in_both_modes(wrangler):
    assert make_client("acme_db, globex_db", FakeGlue(TENANTS)).test_connection()["success"] is True
    assert make_client("acme_db", FakeGlue(TENANTS)).test_connection()["success"] is True
    assert [c["database"] for c in wrangler.calls] == [None, "acme_db"]


def test_single_mode_does_not_refuse_unqualified_references(wrangler):
    client = make_client("sales_x", FakeGlue({"sales_x": ["orders"]}))
    client.execute_query("SELECT * FROM orders")
    assert len(wrangler.calls) == 1



@pytest.mark.parametrize("spec,expected", [
    ("*_db, !billing_db", {"acme_db", "globex_db"}),
    ("*_db, analytics_history", {"acme_db", "globex_db", "billing_db", "analytics_history"}),
    ("a?me_db, analytics_*, !analytics_x", {"acme_db", "analytics_history"}),
])
def test_spec_resolves_the_expected_database_set(wrangler, spec, expected):
    client = make_client(spec, FakeGlue(TENANTS))
    assert {t.metadata_json["database"] for t in client.get_tables()} == expected


def test_database_matching_two_entries_is_indexed_once(wrangler):
    client = make_client("*_db, acme_db", FakeGlue(TENANTS))
    tables = client.get_tables()
    assert names(tables).count("acme_db.assets") == 1


def test_explicit_name_is_used_lowercased(wrangler):
    client = make_client("ACME_DB, globex_db", FakeGlue(TENANTS))
    assert {t.metadata_json["database"] for t in client.get_tables()} == {"acme_db", "globex_db"}


def test_explicit_list_never_lists_glue_databases(wrangler):
    glue = FakeGlue(TENANTS, forbid_database_listing=True)
    client = make_client("acme_db, globex_db", glue)
    assert len(client.get_tables()) > 0
    assert client.count_tables() > 0


def test_same_table_in_two_databases_is_two_tables(wrangler):
    tables = make_client("acme_db, globex_db", FakeGlue(TENANTS)).get_tables()
    assert {"acme_db.assets", "globex_db.assets"} <= set(names(tables))
    assert all(t.name == f"{t.metadata_json['database']}.{t.name.split('.', 1)[1]}" for t in tables)


def test_database_name_with_a_dot_is_skipped_and_reported(wrangler):
    glue = FakeGlue({"acme_db": ["assets"], "odd.db": ["x"]})
    client = make_client("acme_db, odd.db", glue)
    assert names(client.get_tables()) == ["acme_db.assets"]
    reported = [d["name"] for d in client.index_stats()["unreadable_datasets"]]
    assert reported == ["odd.db"]
    assert client.unindexed_table_prefixes() == []



def test_exclude_regex_drops_day_tables_and_keeps_lookalikes(wrangler):
    client = make_client("*_db", FakeGlue(TENANTS), table_exclude_pattern=r"^day_\d{2}")
    got = set(names(client.get_tables()))
    assert "globex_db.day_01" not in got
    assert not any(n.startswith("globex_db.day_07_") for n in got)
    assert {"globex_db.daily_x", "globex_db.day"} <= got
    assert client.count_tables() == len(got)


def test_dead_exclude_pattern_fails_the_crawl_instead_of_indexing_everything(wrangler, monkeypatch):
    monkeypatch.setattr(safe_regex, "SEARCH_TIMEOUT_S", 0.001)
    glue = FakeGlue({"acme_db": ["a" * 60 + "!", "b" * 60 + "!"]})
    client = make_client("acme_db", glue, table_exclude_pattern=r"^(a|aa)+$")
    assert client.get_tables() == []
    with pytest.raises(Exception):
        client.count_tables()
    assert client.test_connection()["success"] is False



def test_multi_mode_sends_no_context_database(wrangler):
    client = make_client("acme_db, globex_db", FakeGlue(TENANTS))
    client.execute_query("SELECT * FROM globex_db.assets")
    assert wrangler.calls[0]["database"] is None


def test_multi_mode_refuses_unqualified_table_even_when_every_namesake_is_active(wrangler):
    client = make_client("acme_db, globex_db", FakeGlue(TENANTS))
    with pytest.raises(Exception):
        client.execute_query("SELECT * FROM assets")
    assert wrangler.calls == []


def test_multi_mode_allows_cte_shadowing_a_table_name(wrangler):
    client = make_client("acme_db, globex_db", FakeGlue(TENANTS))
    client.execute_query("WITH users AS (SELECT 1 AS id) SELECT * FROM users")
    assert len(wrangler.calls) == 1


@pytest.mark.parametrize("sql", [
    "SELECT FROM FROM (",
    "SHOW TABLES IN globex_db",
    "DESCRIBE globex_db.assets",
])
def test_multi_mode_never_sends_unverifiable_sql_to_athena(wrangler, sql):
    client = make_client("acme_db, globex_db", FakeGlue(TENANTS))
    with pytest.raises(Exception):
        client.execute_query(sql)
    assert wrangler.calls == []


PRUNABLE = [_glue_error("EntityNotFoundException")]
UNREAD = [
    _glue_error("ThrottlingException"),
    _glue_error("ExpiredTokenException"),
    _glue_error("AccessDeniedException"),
    EndpointConnectionError(endpoint_url="https://glue.example"),
    _glue_error("ValidationException"),
    RuntimeError("something unexpected"),
]


@pytest.mark.parametrize("error", PRUNABLE)
def test_database_that_is_gone_is_reported_but_not_protected_from_pruning(wrangler, error):
    glue = FakeGlue({"acme_db": ["assets"], "gone_db": error})
    client = make_client("acme_db, gone_db", glue)
    assert names(client.get_tables()) == ["acme_db.assets"]
    assert client.unindexed_table_prefixes() == []
    stats = client.index_stats()
    assert [d["name"] for d in stats["unreadable_datasets"]] == ["gone_db"]
    assert stats["unreadable_dataset_count"] == 1


@pytest.mark.parametrize("error", UNREAD, ids=lambda e: type(e).__name__ + getattr(e, "response", {}).get("Error", {}).get("Code", ""))
def test_database_that_failed_to_read_is_protected_and_reported(wrangler, error):
    glue = FakeGlue({"acme_db": ["assets"], "flaky_db": error})
    client = make_client("acme_db, flaky_db", glue)
    assert names(client.get_tables()) == ["acme_db.assets"]
    assert client.unindexed_table_prefixes() == ["flaky_db"]
    entry = client.index_stats()["unreadable_datasets"][0]
    assert entry["name"] == "flaky_db" and entry["reason"]


def test_failed_database_listing_page_fails_the_crawl_and_prunes_nothing(wrangler):
    glue = FakeGlue(TENANTS, fail_database_page=1)
    client = make_client("*_db", glue)
    assert client.get_tables() == []
    assert client.unindexed_table_prefixes() == []


def test_database_unread_once_is_no_longer_reported_after_it_reads_again(wrangler):
    glue = FakeGlue({"acme_db": ["assets"], "flaky_db": _glue_error("ThrottlingException")})
    client = make_client("acme_db, flaky_db", glue)
    client.get_tables()
    assert client.unindexed_table_prefixes() == ["flaky_db"]
    glue.databases["flaky_db"] = ["events"]
    tables = client.get_tables()
    assert "flaky_db.events" in names(tables)
    assert client.unindexed_table_prefixes() == []
    assert client.index_stats() == {}


def test_one_failing_database_in_count_is_reported_and_does_not_raise(wrangler):
    glue = FakeGlue({"acme_db": ["assets", "users"], "flaky_db": _glue_error("AccessDeniedException")})
    client = make_client("acme_db, flaky_db", glue)
    assert client.count_tables() == 2
    assert [d["name"] for d in client.index_stats()["unreadable_datasets"]] == ["flaky_db"]


def test_count_raises_when_no_database_is_readable(wrangler):
    glue = FakeGlue({"a_db": _glue_error("AccessDeniedException"), "b_db": _glue_error("AccessDeniedException")})
    with pytest.raises(Exception):
        make_client("a_db, b_db", glue).count_tables()


def test_cancellation_inside_a_database_propagates(wrangler):
    glue = FakeGlue({"acme_db": ["assets"], "slow_db": IndexingCancelled()})
    with pytest.raises(IndexingCancelled):
        make_client("acme_db, slow_db", glue).get_tables()


def test_count_tables_equals_the_crawl(wrangler):
    client = make_client("*_db, !billing_db", FakeGlue(TENANTS), table_exclude_pattern=r"^day_\d{2}")
    assert client.count_tables() == len(client.get_tables())



def test_connection_test_fails_when_the_spec_matches_no_database(wrangler):
    client = make_client("zzz_*", FakeGlue(TENANTS))
    assert client.test_connection()["success"] is False


def test_connection_test_succeeds_and_names_a_missing_database(wrangler):
    glue = FakeGlue({"acme_db": ["assets"], "ghost_db": _glue_error("EntityNotFoundException")})
    result = make_client("acme_db, ghost_db", glue).test_connection()
    assert result["success"] is True
    assert "ghost_db" in result["message"]


def test_single_mode_connection_test_reports_a_missing_database(wrangler):
    glue = FakeGlue({"ghost_db": _glue_error("EntityNotFoundException")})
    result = make_client("ghost_db", glue).test_connection()
    assert result["success"] is False
    assert "ghost_db" in result["message"]



def test_multi_description_names_attached_databases_and_teaches_qualified_names(wrangler):
    client = make_client("*_db", FakeGlue(TENANTS))
    client.attach_table_metadata([
        {"name": "acme_db.assets", "metadata_json": {"database": "acme_db"}},
        {"name": "globex_db.assets", "metadata_json": {"database": "globex_db"}},
    ])
    text = client.description
    assert "acme_db" in text and "globex_db" in text
    assert "FROM assets" not in text and "FROM my_table" not in text
    assert "SHOW TABLES" not in text and "DESCRIBE " not in text.replace("SHOW and DESCRIBE", "")


def test_single_description_has_no_show_or_describe_example(wrangler):
    text = make_client("sales_x", FakeGlue({"sales_x": []})).description
    assert "sales_x" in text
    assert "SHOW TABLES" not in text and 'execute_query("DESCRIBE' not in text


def test_attach_table_metadata_ignores_a_database_outside_the_spec(wrangler):
    client = make_client("*_db, !billing_db", FakeGlue(TENANTS))
    client.attach_table_metadata([
        {"name": "billing_db.costs", "metadata_json": {"database": "billing_db"}},
        {"name": "other.x", "metadata_json": {"database": "other"}},
        {"name": "acme_db.assets", "metadata_json": {"database": "acme_db"}},
    ])
    text = client.description
    assert "acme_db" in text and "billing_db" not in text.split("Every table")[0] and "other" not in text.split("Every table")[0]


SCOPE_NAMES = dict(active=["globex_db.asset", "asset"], inactive=["acme_db.asset"])


def scoped(client):
    from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR, TableScope
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(**SCOPE_NAMES))
    return client


@pytest.mark.parametrize("sql", [
    "SELECT * FROM asset",
    "SELECT * FROM acme_db.asset",
    "SELECT * FROM billing_db.asset",
    "SELECT * FROM othercat.globex_db.asset",
    "SELECT * FROM globex_db.asset a JOIN billing_db.asset b ON a.id = b.id",
])
def test_multi_mode_scope_does_not_let_a_legacy_one_part_row_unlock_other_databases(wrangler, sql):
    from app.ai.code_execution.table_scope import TableScopeViolation, enforce_table_scope
    client = scoped(make_client("acme_db, globex_db, billing_db", FakeGlue(TENANTS)))
    with pytest.raises(TableScopeViolation):
        enforce_table_scope(client, sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM globex_db.asset",
    "SELECT * FROM awsdatacatalog.globex_db.asset",
    "SELECT * FROM GLOBEX_DB.ASSET",
])
def test_multi_mode_scope_allows_active_database_qualified_tables(wrangler, sql):
    from app.ai.code_execution.table_scope import enforce_table_scope
    enforce_table_scope(scoped(make_client("acme_db, globex_db", FakeGlue(TENANTS))), sql)


def test_single_mode_scope_keeps_suffix_matching(wrangler):
    from app.ai.code_execution.table_scope import enforce_table_scope
    from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR, TableScope
    client = make_client("globex_db", FakeGlue(TENANTS))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["globex_db.asset", "asset"], inactive=[]))
    enforce_table_scope(client, "SELECT * FROM asset")
    enforce_table_scope(client, "SELECT * FROM globex_db.asset")


@pytest.mark.parametrize("pattern", ["a" * 201, "x{101}", "x{1,500}", "(ab){1000}"])
def test_exclude_pattern_that_is_too_costly_is_refused_by_the_client(wrangler, pattern):
    with pytest.raises(ValueError):
        make_client("acme_db", FakeGlue(TENANTS), table_exclude_pattern=pattern)


def test_failure_reason_carries_no_aws_message_text(wrangler):
    error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:iam::123456789012:role/secret"}}, "GetTables")
    client = make_client("acme_db, flaky_db", FakeGlue({"acme_db": ["assets"], "flaky_db": error}))
    client.get_tables()
    assert "123456789012" not in str(client.index_stats())


def test_dotted_entries_are_not_described_as_permission_problems(wrangler):
    client = make_client("acme_db, odd.db", FakeGlue({"acme_db": ["assets"], "odd.db": ["x"]}))
    message = client.test_connection()["message"]
    assert "odd.db" in message and "permission" not in message.lower()


def test_multi_description_does_not_list_raw_spec_entries_when_nothing_is_attached(wrangler):
    text = make_client("*_db, !billing_db", FakeGlue(TENANTS)).description
    assert "*_db" not in text


def test_exclude_pattern_timeout_reaches_the_refresh_as_its_reason(wrangler, monkeypatch):
    from app.data_sources.clients.discovery import IncompleteDiscoveryError, require_complete_discovery
    monkeypatch.setattr(safe_regex, "SEARCH_TIMEOUT_S", 0.001)
    glue = FakeGlue({"acme_db": ["a" * 60 + "!"], "globex_db": ["b" * 60 + "!"]})
    for spec in ("acme_db", "acme_db, globex_db"):
        client = make_client(spec, glue, table_exclude_pattern=r"^(a|aa)+$")
        with pytest.raises(IncompleteDiscoveryError) as caught:
            require_complete_discovery(client, client.get_tables())
        assert "table_exclude_pattern" in str(caught.value)


def test_failed_database_listing_keeps_the_previous_catalog(wrangler):
    from app.data_sources.clients.discovery import IncompleteDiscoveryError, require_complete_discovery
    client = make_client("*_db", FakeGlue(TENANTS, fail_database_page=1))
    with pytest.raises(IncompleteDiscoveryError):
        require_complete_discovery(client, client.get_tables())


@pytest.mark.parametrize("sql", [
    "SELECT * FROM billing_db.costs",
    "SELECT * FROM analytics_history.general",
    "SELECT * FROM othercat.acme_db.assets",
    "SELECT * FROM acme_db.assets a JOIN billing_db.costs c ON a.id = c.id",
    "WITH x AS (SELECT * FROM billing_db.costs) SELECT * FROM x",
])
def test_multi_mode_refuses_databases_outside_the_spec_without_a_table_scope(wrangler, sql):
    client = make_client("*_db, !billing_db", FakeGlue(TENANTS))
    with pytest.raises(ValueError):
        client.execute_query(sql)
    assert wrangler.calls == []


@pytest.mark.parametrize("sql", [
    "SELECT * FROM acme_db.assets",
    "SELECT * FROM AwsDataCatalog.globex_db.assets",
    'SELECT * FROM "acme_db"."assets" a JOIN globex_db.alerts b ON a.id = b.id',
])
def test_multi_mode_runs_tables_in_the_spec(wrangler, sql):
    make_client("*_db, !billing_db", FakeGlue(TENANTS)).execute_query(sql)
    assert len(wrangler.calls) == 1


def test_count_after_a_crawl_reflects_the_crawl_not_an_older_count(wrangler):
    glue = FakeGlue({"acme_db": ["assets"], "flaky_db": _glue_error("ThrottlingException")})
    client = make_client("acme_db, flaky_db", glue)
    assert client.count_tables() == 1
    assert client.unindexed_table_prefixes() == ["flaky_db"]

    glue.databases["flaky_db"] = ["events", "users"]
    tables = client.get_tables()
    assert client.count_tables() == len(tables) == 3
    assert client.index_stats() == {}
    assert client.unindexed_table_prefixes() == []


def test_indexing_progress_counts_only_tables_that_are_indexed(wrangler):
    client = make_client("*_db", FakeGlue(TENANTS), table_exclude_pattern=r"^day_\d{2}")
    events = []
    tables = client.get_schemas(progress_callback=lambda *event: events.append(event))
    labels = {item for phase, item, *_ in events if phase == "tables" and item}
    assert labels == {t.name for t in tables}
    assert not any("day_0" in label for label in labels)


@pytest.mark.parametrize("dotted", [3, 51, 400])
def test_skipped_dotted_tables_are_reported_without_listing_every_one(wrangler, dotted):
    from app.data_sources.clients.progress import unreadable_note
    glue = FakeGlue({"acme_db": ["assets"] + [f"t.{i}" for i in range(dotted)]})
    client = make_client("acme_db, globex_db", glue)
    glue.databases["globex_db"] = ["alerts"]
    assert names(client.get_tables()) == ["acme_db.assets", "globex_db.alerts"]
    entries = client.index_stats()["unreadable_datasets"]
    assert len(entries) <= athena_module.MAX_SKIPPED_TABLE_ENTRIES + 1
    listed = [e for e in entries if not e["name"].startswith("+")]
    rolled = [e for e in entries if e["name"].startswith("+")]
    assert len(listed) + sum(int(e["name"][1:].split()[0]) for e in rolled) == dotted
    assert "permission" not in unreadable_note(entries).lower()
