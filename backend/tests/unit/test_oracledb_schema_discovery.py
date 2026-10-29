"""Oracle schema discovery must read the dictionary views one at a time.

On Oracle 10g/11g a LEFT JOIN across ALL_TAB_COLUMNS, ALL_COL_COMMENTS and
ALL_TAB_COMMENTS ran for minutes (137k columns, 4k tables) while each view on
its own answers in seconds. These tests pin the public contract of
get_schemas() — tables, column order, comments, owner filtering — against a
fake driver that serves each dictionary view separately, and pin the cheap
count_tables() used by the connection test.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

import app.data_sources.clients.oracledb_client as oc

# ---------------------------------------------------------------------------
# Fake Oracle dictionary: a boundary mock standing in for python-oracledb
# ---------------------------------------------------------------------------

# owner -> {table: {"cols": [(name, dtype, col_comment)], "comment": str|None, "view": bool}}
DICTIONARY = {
    "REPOS2000": {
        "ORDERS": {
            "cols": [("ID", "NUMBER", "Order id"), ("AMOUNT", "NUMBER", None), ("NOTE", "VARCHAR2", "")],
            "comment": "Sales orders",
            "view": False,
        },
        "CUSTOMERS": {
            "cols": [("ID", "NUMBER", None), ("NAME", "VARCHAR2", "Display name")],
            "comment": None,
            "view": False,
        },
        "V_ORDERS": {"cols": [("ID", "NUMBER", None)], "comment": "Orders view", "view": True},
    },
    "BRIO": {
        "REPORTS": {"cols": [("RID", "NUMBER", None)], "comment": "BI reports", "view": False},
    },
    "SYS": {
        "DUAL": {"cols": [("DUMMY", "VARCHAR2", None)], "comment": None, "view": False},
    },
}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class FakeDictionaryConnection:
    """Answers each dictionary view from DICTIONARY, honouring the owner binds.

    Refuses any statement that references more than one dictionary view: a
    join is exactly the shape that hangs on old servers, so it is a failure
    here rather than something the fake silently serves.
    """

    VIEWS = ("all_col_comments", "all_tab_comments", "all_tab_columns", "all_tables", "all_views")

    def __init__(self):
        self.statements = []

    def _owners(self, params):
        return set((params or {}).values())

    def execute(self, clause, params=None):
        sql = str(clause).lower()
        self.statements.append(sql)
        views = [v for v in self.VIEWS if v in sql]
        if sql.strip().startswith("alter session"):
            return _Result([])
        owners = self._owners(params)
        if "all_col_comments" in sql and len(views) == 1:
            rows = [
                (o, t, c, cm)
                for o in owners for t, meta in DICTIONARY.get(o, {}).items()
                for c, _dt, cm in meta["cols"] if cm is not None
            ]
            return _Result(rows)
        if "all_tab_comments" in sql and len(views) == 1:
            rows = [
                (o, t, meta["comment"])
                for o in owners for t, meta in DICTIONARY.get(o, {}).items()
                if meta["comment"] is not None
            ]
            return _Result(rows)
        if "all_tab_columns" in sql and len(views) == 1:
            rows = [
                (o, t, c, dt)
                for o in sorted(owners) for t, meta in sorted(DICTIONARY.get(o, {}).items())
                for c, dt, _cm in meta["cols"]
            ]
            return _Result(rows)
        if set(views) == {"all_tables", "all_views"}:
            # count_tables(): one scalar built from the two catalog views
            n = sum(1 for o in owners for _t in DICTIONARY.get(o, {}))
            return _Result([(n,)])
        raise AssertionError(f"unexpected dictionary statement (joins hang on 10g): {sql[:120]}")


@pytest.fixture()
def fake_conn(monkeypatch):
    conn = FakeDictionaryConnection()

    @contextmanager
    def _connect(self):
        yield conn

    monkeypatch.setattr(oc.OracledbClient, "connect", _connect)
    # FK reflection goes through SQLAlchemy's inspector against the real driver;
    # it is a separate boundary and measured fast on 10g, so stub it here.
    monkeypatch.setattr(oc, "attach_foreign_keys", lambda *a, **k: 0)
    return conn


def _client(**overrides):
    params = {"host": "dbhost", "port": 1521, "service_name": "dwh", "user": "repos2000", "password": "x"}
    params.update(overrides)
    return oc.OracledbClient(**params)


def _by_name(tables):
    return {t.name: t for t in tables}


# ---------------------------------------------------------------------------
# get_schemas(): same result the old join produced
# ---------------------------------------------------------------------------

def test_tables_and_columns_keep_dictionary_order(fake_conn):
    tables = _by_name(_client().get_schemas())
    assert set(tables) == {"REPOS2000.ORDERS", "REPOS2000.CUSTOMERS", "REPOS2000.V_ORDERS"}
    assert [c.name for c in tables["REPOS2000.ORDERS"].columns] == ["ID", "AMOUNT", "NOTE"]
    assert [c.dtype for c in tables["REPOS2000.ORDERS"].columns] == ["NUMBER", "NUMBER", "VARCHAR2"]
    assert all(t.metadata_json == {"schema": "REPOS2000"} for t in tables.values())


def test_comments_attach_where_present_and_are_none_otherwise(fake_conn):
    tables = _by_name(_client().get_schemas())
    orders = tables["REPOS2000.ORDERS"]
    assert orders.description == "Sales orders"
    assert {c.name: c.description for c in orders.columns} == {
        "ID": "Order id", "AMOUNT": None, "NOTE": None,  # empty comment reads as none
    }
    assert tables["REPOS2000.CUSTOMERS"].description is None
    assert tables["REPOS2000.CUSTOMERS"].columns[1].description == "Display name"


def test_default_owner_is_login_user_uppercased(fake_conn):
    tables = _client(user="repos2000").get_schemas()
    assert {t.metadata_json["schema"] for t in tables} == {"REPOS2000"}


def test_configured_schemas_filter_every_dictionary_read(fake_conn):
    tables = _by_name(_client(user="someone_else", schema="repos2000, brio").get_schemas())
    assert set(tables) == {
        "REPOS2000.ORDERS", "REPOS2000.CUSTOMERS", "REPOS2000.V_ORDERS", "BRIO.REPORTS",
    }
    assert tables["BRIO.REPORTS"].description == "BI reports"
    # SYS is visible in the dictionary but was not asked for
    assert not any(n.startswith("SYS.") for n in tables)


def test_discovery_never_joins_dictionary_views(fake_conn):
    _client().get_schemas()
    for sql in fake_conn.statements:
        assert sum(v in sql for v in FakeDictionaryConnection.VIEWS) <= 1


# ---------------------------------------------------------------------------
# count_tables(): the connection test's cheap path
# ---------------------------------------------------------------------------

def test_count_tables_matches_what_discovery_returns(fake_conn):
    client = _client()
    assert client.count_tables() == len(client.get_schemas()) == 3


def test_count_tables_honours_configured_schemas(fake_conn):
    client = _client(user="someone_else", schema="repos2000,brio")
    assert client.count_tables() == len(client.get_schemas()) == 4


def test_count_tables_does_not_read_columns(fake_conn):
    _client().count_tables()
    assert not any("all_tab_columns" in sql for sql in fake_conn.statements)
