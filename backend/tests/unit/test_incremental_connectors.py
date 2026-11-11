"""Caching and incremental refresh for connectors that are not plain SQL.

Every connector that can run a query can be cached, and every cached table can
refresh incrementally. What differs is only how a refresh window reaches the
source: written into SOQL's WHERE clause (Salesforce), as a FILTER around a DAX
table expression (Power BI), in HogQL (PostHog) — or, for a connector with no
way to express one, not at all: its query runs whole and the window is taken
from the result.

The Salesforce and generic-connector tests run the real service against a fake
org / fake client; the query languages themselves are exercised as text.
"""
import json
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest
import pytest_asyncio

from app.data_sources.fast import artifacts, incremental
from app.data_sources.fast.incremental import IncrementalConfigError, Window
from app.data_sources.fast.powerbi_source import parse_dax, window_dax
from app.data_sources.fast.salesforce_source import SalesforceSource, bounded_soql, window_soql
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.organization import Organization
from app.services import custom_table_runs
from app.services.custom_query_service import CustomQueryService

# -- SOQL ---------------------------------------------------------------------

AT = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("soql,expected", [
    ("SELECT Id FROM Account",
     "SELECT Id FROM Account WHERE SystemModstamp >= 2026-11-01T08:30:00Z"),
    ("SELECT Id FROM Account ORDER BY Name LIMIT 50;",
     "SELECT Id FROM Account WHERE SystemModstamp >= 2026-11-01T08:30:00Z ORDER BY Name LIMIT 50"),
    ("SELECT Id FROM Account WHERE Industry = 'Tech' OR Rating = 'Hot'",
     "SELECT Id FROM Account WHERE (Industry = 'Tech' OR Rating = 'Hot') AND SystemModstamp >= 2026-11-01T08:30:00Z"),
    # Keywords inside a string, and a child subquery with its own WHERE, are not the query's own clauses.
    ("SELECT Id, (SELECT Id FROM Contacts WHERE Email != null ORDER BY Name) FROM Account WHERE Name = 'ORDER BY me' GROUP BY Id",
     "SELECT Id, (SELECT Id FROM Contacts WHERE Email != null ORDER BY Name) FROM Account "
     "WHERE (Name = 'ORDER BY me') AND SystemModstamp >= 2026-11-01T08:30:00Z GROUP BY Id"),
])
def test_a_window_is_written_into_the_querys_own_where_clause(soql, expected):
    assert window_soql(soql, "SystemModstamp", None, Window(lo=AT)) == expected


def test_soql_windows_are_half_open_and_can_carry_rows_without_a_cursor():
    out = window_soql("SELECT Id FROM Case", "ClosedDate", None,
                      Window(lo=date(2026, 1, 1), hi=date(2026, 2, 1)))
    assert out == "SELECT Id FROM Case WHERE ClosedDate >= 2026-01-01 AND ClosedDate < 2026-02-01"
    tail = window_soql("SELECT Id FROM Case", "ClosedDate", None, Window(lo=date(2026, 1, 1), include_nulls=True))
    assert tail == "SELECT Id FROM Case WHERE ((ClosedDate >= 2026-01-01) OR ClosedDate = null)"
    # Nothing to restrict: the query is sent as written.
    assert window_soql("SELECT Id FROM Case LIMIT 5", "ClosedDate", None, Window()) == "SELECT Id FROM Case LIMIT 5"


def test_soql_dates_are_bare_and_utc_and_naive_values_are_taken_as_utc():
    eastern = datetime(2026, 11, 1, 3, 30, tzinfo=timezone(timedelta(hours=-5)))
    for value in (AT, eastern, datetime(2026, 11, 1, 8, 30)):
        assert "SystemModstamp >= 2026-11-01T08:30:00Z" in window_soql(
            "SELECT Id FROM Account", "SystemModstamp", None, Window(lo=value))
    assert window_soql("SELECT Id FROM A", "Seq__c", None, Window(lo=41)).endswith("Seq__c >= 41")


def test_placeholders_in_soql_are_filled_even_when_the_window_is_open():
    soql = "SELECT Id FROM Account WHERE LastModifiedDate >= {{since}} AND LastModifiedDate < {{until}}"
    out = window_soql(soql, "LastModifiedDate", "timestamp", Window(lo=AT))
    assert out.startswith("SELECT Id FROM Account WHERE LastModifiedDate >= 2026-11-01T08:30:00Z AND")
    assert "{{" not in window_soql(soql, "LastModifiedDate", "timestamp", Window())


@pytest.mark.parametrize("column", ["Name; DELETE", "a b", "x' OR '1'='1", "", "COUNT(Id)"])
def test_only_a_field_name_can_be_a_soql_cursor(column):
    with pytest.raises(IncrementalConfigError):
        window_soql("SELECT Id FROM Account", column, None, Window(lo=1))


def test_a_text_cursor_that_could_end_a_soql_literal_is_refused():
    with pytest.raises(IncrementalConfigError):
        window_soql("SELECT Id FROM Account", "Code__c", None, Window(lo="x' OR Name != '"))


@pytest.mark.parametrize("soql,limit,expected", [
    ("SELECT Id FROM Account", 100, "SELECT Id FROM Account LIMIT 100"),
    ("SELECT Id FROM Account LIMIT 5", 100, "SELECT Id FROM Account LIMIT 5"),
    ("SELECT Id FROM Account LIMIT 5000;", 100, "SELECT Id FROM Account LIMIT 100"),
    ("SELECT Id, (SELECT Id FROM Contacts LIMIT 3) FROM Account", 100,
     "SELECT Id, (SELECT Id FROM Contacts LIMIT 3) FROM Account LIMIT 100"),
])
def test_a_soql_preview_is_bounded_at_the_source(soql, limit, expected):
    assert bounded_soql(soql, limit) == expected


class FakeOrg:
    """A Salesforce org: holds records, answers SOQL with the conditions the
    window writer produces, and pages like the real query API."""

    PAGE = 2000

    def __init__(self):
        self.records, self.queries, self._cursors = [], [], {}

    def add(self, count, **over):
        start = len(self.records)
        for i in range(start, start + count):
            stamp = over.get("stamp", datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i))
            self.records.append({
                "attributes": {"type": "Opportunity"}, "Id": f"006{i:012d}",
                "Amount": over.get("amount", i * 1.5),
                "StageName": over.get("stage", "Open"),
                # A lookup that is null on some rows: flattened to Account.Name.
                "Account": None if i % 7 == 0 else {"attributes": {"type": "Account"}, "Name": f"Acct {i % 50}"},
                "SystemModstamp": None if stamp is None else stamp.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            })

    def touch(self, index, when, stage="Closed Won"):
        self.records[index]["StageName"] = stage
        self.records[index]["SystemModstamp"] = when.strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    @staticmethod
    def _stamp(text):
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

    def _match(self, soql):
        lo = re.search(r"SystemModstamp >= (\S+?Z)", soql)
        hi = re.search(r"SystemModstamp < (\S+?Z)", soql)
        nulls = "SystemModstamp = null" in soql
        limit = re.search(r"LIMIT (\d+)\s*$", soql)
        out = []
        for r in self.records:
            stamp = r["SystemModstamp"]
            if stamp is None:
                if nulls or not (lo or hi):
                    out.append(r)
                continue
            at = self._stamp(stamp)
            if lo and at < self._stamp(lo.group(1)):
                continue
            if hi and at >= self._stamp(hi.group(1)):
                continue
            out.append(r)
        return out[: int(limit.group(1))] if limit else out

    def _page(self, key, offset):
        rows = self._cursors[key]
        done = offset + self.PAGE >= len(rows)
        page = {"totalSize": len(rows), "done": done, "records": rows[offset:offset + self.PAGE]}
        if not done:
            page["nextRecordsUrl"] = f"/query/{key}-{offset + self.PAGE}"
        return page

    def query(self, soql):
        self.queries.append(soql)
        key = uuid.uuid4().hex[:8]
        self._cursors[key] = self._match(soql)
        return self._page(key, 0)

    def query_more(self, url, identifier_is_url=False):
        key, offset = url.rsplit("/", 1)[1].split("-")
        return self._page(key, int(offset))


def _source(org):
    client = type("Client", (), {})()

    @contextmanager
    def connect():
        yield org

    client.connect = connect
    return SalesforceSource(client)


def test_salesforce_rows_stream_in_pages_past_the_agents_row_cap():
    from app.data_sources.clients.salesforce_client import MAX_ROWS
    org = FakeOrg()
    org.add(MAX_ROWS + 2500)
    batches = list(_source(org).stream_batches(
        "SELECT Id, Amount, StageName, Account.Name, SystemModstamp FROM Opportunity", 3000))
    assert sum(b.num_rows for b in batches) == MAX_ROWS + 2500, "the cap is the agent's, not the cache's"
    assert max(b.num_rows for b in batches) <= 3000, "memory stays at one batch"
    assert batches[0].schema.names == ["Id", "Amount", "StageName", "Account.Name", "SystemModstamp"]
    ids = [i for b in batches for i in b.column("Id").to_pylist()]
    assert len(set(ids)) == len(ids)


def test_an_empty_salesforce_result_still_has_the_querys_columns():
    (batch,) = _source(FakeOrg()).stream_batches("SELECT Id, Account.Name FROM Opportunity", 100)
    assert batch.num_rows == 0 and batch.schema.names == ["Id", "Account.Name"]


# -- the real service on a fake org ------------------------------------------------

pytestmark_db = [pytest.mark.db, pytest.mark.asyncio]


class Lab:
    def __init__(self, root, connection_id):
        self.root, self.connection_id = root, connection_id
        self.service = CustomQueryService()
        self.table_id = None

    async def table(self, sql, mode, **config):
        settings = self.service.refresh_settings(mode, config, sql)
        async with async_session_maker() as db:
            row = ConnectionTable(connection_id=self.connection_id, name="copy", kind=KIND_BOW,
                                  definition_sql=sql, columns=[], pks=[], fks=[],
                                  refresh_mode=settings.mode, refresh_config=settings.to_dict())
            db.add(row)
            await db.commit()
            self.table_id = row.id

    async def refresh(self, **kwargs):
        async with async_session_maker() as db:
            connection = await db.get(Connection, self.connection_id)
            table = await db.get(ConnectionTable, self.table_id)
            await self.service.refresh(db, connection, table, **kwargs)
            await db.refresh(table)
            return table

    async def copy(self, sql):
        async with async_session_maker() as db:
            row = await db.get(ConnectionTable, self.table_id)
        con = artifacts.connect_encrypted(row.artifact_path, artifacts.decrypt_key(row.artifact_key_enc),
                                          read_only=True)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    async def last_run(self):
        async with async_session_maker() as db:
            listing = await custom_table_runs.list_runs(db, self.table_id, limit=1)
            return await custom_table_runs.get_run(db, self.table_id, listing["runs"][0]["id"])


async def _lab(tmp_path, conn_type):
    async with async_session_maker() as db:
        org = Organization(name=f"Connectors {uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        connection = Connection(name=conn_type, type=conn_type, config={}, organization_id=org.id)
        db.add(connection)
        await db.commit()
        return Lab(tmp_path / "fast", connection.id)


@pytest_asyncio.fixture
async def salesforce(tmp_path):
    from app.data_sources.clients.salesforce_client import SalesforceClient
    org = FakeOrg()

    @contextmanager
    def connect(self):
        yield org

    async def client(self, db, connection, current_user=None, **_):
        return SalesforceClient(access_token="t", instance_url="https://x.my.salesforce.com")

    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"), \
            patch.object(SalesforceClient, "connect", connect), \
            patch("app.services.connection_service.ConnectionService.construct_client", client):
        lab = await _lab(tmp_path, "salesforce")
        lab.org = org
        yield lab


SOQL = "SELECT Id, Amount, StageName, Account.Name, SystemModstamp FROM Opportunity"


@pytest.mark.db
@pytest.mark.asyncio
async def test_salesforce_is_cached_whole_and_then_refreshed_from_its_checkpoint(salesforce):
    org = salesforce.org
    org.add(12_500)
    org.add(3, stamp=None)                     # records with no SystemModstamp at all
    await salesforce.table(SOQL, "merge", cursor_column="SystemModstamp", unique_key=["Id"],
                           lookback_seconds=3600)
    table = await salesforce.refresh()
    assert table.no_rows == 12_503 and table.last_refresh_status == "ok"
    first = await salesforce.last_run()
    assert first["rendered_query"] == SOQL, "a first load sends the admin's query as written"

    newest = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=12_499)
    org.add(4, stamp=newest + timedelta(minutes=5))
    org.touch(10, newest + timedelta(minutes=6))
    org.touch(12_501, newest + timedelta(minutes=7), stage="Late stamp")    # gets its first stamp
    asked_before = len(org.queries)
    table = await salesforce.refresh()
    run = await salesforce.last_run()
    assert run["mode"] == "incremental" and (run["rows_inserted"], run["rows_updated"]) == (4, 2)
    assert table.no_rows == 12_507
    sent = org.queries[asked_before:][-1]
    assert sent == run["rendered_query"]
    assert re.fullmatch(re.escape(SOQL) + r" WHERE \(\(SystemModstamp >= 2026-\S+Z\) OR SystemModstamp = null\)",
                        sent), sent
    assert run["rows_read"] < 100, "an hour of lookback, not twelve thousand records"
    stages = dict(await salesforce.copy(
        'SELECT "Id", "StageName" FROM copy WHERE "StageName" <> \'Open\''))
    assert stages == {org.records[10]["Id"]: "Closed Won", org.records[12_501]["Id"]: "Late stamp"}
    # The flattened lookup column and its nulls came through.
    assert (await salesforce.copy('SELECT count(*) FROM copy WHERE "Account.Name" IS NULL'))[0][0] > 1000


@pytest.mark.db
@pytest.mark.asyncio
async def test_salesforce_append_with_the_filter_placed_by_the_admin(salesforce):
    org = salesforce.org
    org.add(300)
    soql = SOQL + " WHERE SystemModstamp >= {{since}}"
    await salesforce.table(soql, "append", cursor_column="SystemModstamp", cursor_kind="timestamp",
                           lookback_seconds=600)
    assert (await salesforce.refresh()).no_rows == 300
    org.add(5, stamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=400))
    table = await salesforce.refresh()
    await salesforce.refresh()                 # the same window again changes nothing
    assert table.no_rows == 305 == (await salesforce.copy("SELECT count(*) FROM copy"))[0][0]
    assert "{{" not in (await salesforce.last_run())["rendered_query"]


# -- DAX ---------------------------------------------------------------------------

def test_a_window_is_a_filter_around_the_dax_table_expression():
    dax = ('DEFINE VAR __east = FILTER(Sales, Sales[Region] = "East")\n'
           'EVALUATE __east ORDER BY Sales[OrderDate]')
    out = window_dax(dax, "Sales[OrderDate]", None, Window(lo=datetime(2026, 3, 1, 12, 0, 5, 900_000)))
    q = parse_dax(out)
    assert q.define.startswith("DEFINE VAR __east")
    assert q.body == 'FILTER(__east, Sales[OrderDate] >= dt"2026-03-01T12:00:05")'
    assert q.order_by == "ORDER BY Sales[OrderDate]"


@pytest.mark.parametrize("column,ref", [("OrderDate", "[OrderDate]"), ("Sales[OrderDate]", "Sales[OrderDate]"),
                                        ("'Order Lines'[Qty]", "'Order Lines'[Qty]"), ("odd]name", "[odd]]name]")])
def test_a_dax_cursor_is_a_column_reference_however_the_admin_named_it(column, ref):
    out = window_dax("EVALUATE Sales", column, None, Window(lo=5, hi=9, include_nulls=True))
    assert out == f"EVALUATE FILTER(Sales, ({ref} >= 5 && {ref} < 9) || ISBLANK({ref}))"


def test_dax_placeholders_and_open_windows():
    dax = "EVALUATE FILTER(Sales, Sales[OrderDate] >= {{since}} && Sales[OrderDate] < {{until}})"
    out = window_dax(dax, "Sales[OrderDate]", "timestamp", Window(lo=date(2026, 1, 1)))
    assert 'Sales[OrderDate] >= dt"2026-01-01T00:00:00"' in out and "{{" not in out
    assert "{{" not in window_dax(dax, "Sales[OrderDate]", "timestamp", Window())
    assert window_dax("EVALUATE Sales", "Sales[OrderDate]", None, Window()) == "EVALUATE Sales"


def test_power_bi_and_salesforce_clients_write_their_own_windows():
    from app.data_sources.clients.powerbi_client import PowerBIClient
    from app.data_sources.clients.salesforce_client import SalesforceClient
    assert incremental.render(PowerBIClient, "EVALUATE Sales", "Qty", None, Window(lo=3)) == \
        "EVALUATE FILTER(Sales, [Qty] >= 3)"
    assert incremental.render(SalesforceClient, "SELECT Id FROM A", "Seq__c", None, Window(lo=3)) == \
        "SELECT Id FROM A WHERE Seq__c >= 3"
    for client in (PowerBIClient, SalesforceClient):
        assert incremental.filters_at_source(client)


# -- SQL-shaped languages that are not in the shared dialect table ----------------------

def test_posthog_and_sybase_declare_the_dialect_their_windows_are_written_in():
    from app.data_sources.clients.posthog_client import PostHogClient
    from app.data_sources.clients.sybase_client import SybaseClient
    lo = datetime(2026, 5, 1, 10, 0, 0, 250_000)
    hogql = incremental.render(PostHogClient, "SELECT timestamp, event FROM events", "timestamp", None,
                               Window(lo=lo, include_nulls=True))
    assert hogql == ("SELECT * FROM (SELECT timestamp, event FROM events) AS bow_sub "
                     "WHERE (`timestamp` >= toDateTime('2026-05-01 10:00:00')) OR `timestamp` IS NULL")
    sybase = incremental.render(SybaseClient, "SELECT * FROM dba.orders", "updated_at", None, Window(lo=lo))
    assert sybase == ("SELECT * FROM (SELECT * FROM dba.orders) bow_sub "
                      "WHERE \"updated_at\" >= CAST('2026-05-01 10:00:00.250000' AS TIMESTAMP)")
    assert incremental.filters_at_source(PostHogClient) and incremental.filters_at_source(SybaseClient)


# -- any other connector ----------------------------------------------------------------

class FakeAlerts:
    """A connector with a query language of its own and no way to ask for a
    range: it returns whatever is currently open, newest state for each."""

    def __init__(self):
        self.rows, self.queries = [], []

    def execute_query(self, query):
        self.queries.append(query)
        return pd.DataFrame(self.rows, columns=["id", "severity", "seen"])


@pytest_asyncio.fixture
async def alerts(tmp_path):
    api = FakeAlerts()

    async def client(self, db, connection, current_user=None, **_):
        return api

    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"), \
            patch("app.services.connection_service.ConnectionService.construct_client", client):
        lab = await _lab(tmp_path, "mongodb")
        lab.api = api
        yield lab


@pytest.mark.db
@pytest.mark.asyncio
async def test_a_connector_that_cannot_filter_is_still_cached_and_keeps_what_it_has_seen(alerts):
    api = alerts.api
    query = json.dumps({"find": "alerts", "filter": {"state": "open"}})
    assert not incremental.filters_at_source(api)
    api.rows = [(1, "low", 10), (2, "high", 11), (3, "low", 12)]
    await alerts.table(query, "merge", cursor_column="seen", unique_key=["id"], lookback_units=0,
                       full_refresh_every_days=1)
    table = await alerts.refresh()
    assert table.no_rows == 3
    first = await alerts.last_run()
    assert any("cannot filter at the source" in e["message"] for e in first["events"])

    # Alert 1 closed (gone from the API), 2 escalated, 4 is new, 3 unchanged.
    api.rows = [(2, "critical", 20), (3, "low", 12), (4, "high", 21)]
    table = await alerts.refresh()
    run = await alerts.last_run()
    assert api.queries[-1] == query, "the connector's own query, untouched"
    assert (run["mode"], run["rows_inserted"], run["rows_updated"]) == ("incremental", 1, 1)
    assert sorted(await alerts.copy("SELECT id, severity, seen FROM copy")) == [
        (1, "low", 10), (2, "critical", 20), (3, "low", 12), (4, "high", 21)]

    # A rebuild would keep only what the API returns today, so it is never
    # done on a schedule — only when the admin asks.
    async with async_session_maker() as db:
        row = await db.get(ConnectionTable, alerts.table_id)
        row.refresh_state = {**row.refresh_state, "last_full_refresh_at": "2020-01-01T00:00:00"}
        await db.commit()
    assert (await alerts.refresh()).no_rows == 4
    assert (await alerts.last_run())["mode"] == "incremental"
    assert (await alerts.refresh(full=True)).no_rows == 3


@pytest.mark.db
@pytest.mark.asyncio
async def test_placeholders_are_refused_where_the_source_cannot_be_filtered(alerts):
    from fastapi import HTTPException
    alerts.api.rows = [(1, "low", 10)]
    await alerts.table('{"find": "alerts", "since": {{since}}}', "append", cursor_column="seen",
                       cursor_kind="number")
    with pytest.raises(HTTPException) as error:
        await alerts.refresh()
    assert "cannot be filtered at the source" in str(error.value.detail)
