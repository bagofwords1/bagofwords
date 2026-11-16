"""Before a Postgres read, the session is made to read literals the way the
guard did; after it, a session that stopped doing so is not reused.

Contract (`prepare_pg_session(conn, sql)` / `verify_pg_session_after(conn)`):

* the server's reported `standard_conforming_strings` decides: `on` needs
  nothing; `off` is switched on with its own SET and read again; when it is
  still not `on` (or the server does not report it), a query whose literals
  hold a backslash is refused and any other query runs;
* a server older than 9.1, or one that reports itself as Redshift, gets the
  Redshift rule: no backslash in a literal and no E-string;
* after the query, a session whose setting is no longer `on` is invalidated,
  so the pool never hands it out again.

The DB-API connection is faked at the driver boundary: the two things it
reports (parameter status and server version) and the statements it is sent.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.ai.code_execution.sql_guard import ReadOnlyViolation
from app.data_sources.clients import _pg_session
from app.data_sources.clients._pg_session import prepare_pg_session, verify_pg_session_after

BS = chr(92)
SCS = "standard_conforming_strings"
SWITCH = "SET standard_conforming_strings = on"
BACKSLASH_READ = "SELECT 'DOMAIN" + BS + "user' AS who"
E_STRING_READ = "SELECT E'it" + BS + "'s' AS v"
# An E-string whose value holds a backslash.
E_BACKSLASH_READ = "SELECT E'a" + BS + BS + "b' AS v"
PLAIN_READ = "SELECT 'it''s' AS v"


class FakeDbapi:
    """What psycopg2 reports for a session."""

    def __init__(self, scs, server_version=150004, version_text="15.4"):
        self.status = {SCS: scs, "server_version": version_text}
        self.server_version = server_version

    def get_parameter_status(self, name):
        return self.status.get(name)


class FakeConn:
    """A SQLAlchemy Connection over `dbapi`: records statements; `becomes`
    is what the server reports after the switch."""

    def __init__(self, dbapi, becomes="on"):
        self.connection = SimpleNamespace(dbapi_connection=dbapi)
        self.info = {}
        self.sent = []
        self.invalidated = False
        self._becomes = becomes

    def exec_driver_sql(self, sql, parameters=None, execution_options=None):
        self.sent.append(sql)
        if sql == SWITCH:
            self.connection.dbapi_connection.status[SCS] = self._becomes

    def invalidate(self):
        self.invalidated = True


@pytest.mark.parametrize("sql", [BACKSLASH_READ, E_STRING_READ, PLAIN_READ])
def test_a_server_reading_standard_strings_needs_nothing(sql):
    conn = FakeConn(FakeDbapi("on"))
    prepare_pg_session(conn, sql)
    assert conn.sent == []


def test_a_server_with_standard_strings_off_is_switched_on_with_its_own_statement():
    conn = FakeConn(FakeDbapi("off"))
    prepare_pg_session(conn, BACKSLASH_READ)
    assert conn.sent == [SWITCH]


@pytest.mark.parametrize("scs, becomes", [("off", "off"), ("off", None), (None, None)])
@pytest.mark.parametrize("sql", [BACKSLASH_READ, E_BACKSLASH_READ])
def test_a_backslash_literal_is_refused_when_standard_strings_cannot_be_confirmed(scs, becomes, sql):
    conn = FakeConn(FakeDbapi(scs), becomes=becomes)
    with pytest.raises(ReadOnlyViolation, match="standard_conforming_strings"):
        prepare_pg_session(conn, sql)


@pytest.mark.parametrize("scs, becomes", [("off", "off"), (None, None)])
def test_a_query_without_backslash_literals_runs_when_standard_strings_cannot_be_confirmed(scs, becomes):
    conn = FakeConn(FakeDbapi(scs), becomes=becomes)
    prepare_pg_session(conn, PLAIN_READ)
    # An E-string escape whose value holds no backslash is sent as a plain
    # literal without one.
    prepare_pg_session(conn, E_STRING_READ)
    prepare_pg_session(conn, "SELECT 1 AS " + '"a' + BS + 'b"')


@pytest.mark.parametrize("dbapi", [
    FakeDbapi("on", server_version=80002, version_text="8.0.2"),
    FakeDbapi("on", server_version=90000, version_text="9.0.0"),
    FakeDbapi("on", server_version=150004, version_text="8.0.2 Redshift 1.0.1"),
])
@pytest.mark.parametrize("sql", [BACKSLASH_READ, E_STRING_READ])
def test_an_old_or_redshift_like_server_gets_the_redshift_rule(dbapi, sql):
    with pytest.raises(ReadOnlyViolation):
        prepare_pg_session(FakeConn(dbapi), sql)


def test_an_old_server_still_runs_a_read_without_backslashes():
    conn = FakeConn(FakeDbapi("on", server_version=90000, version_text="9.0.0"))
    prepare_pg_session(conn, PLAIN_READ)


def test_a_session_whose_standard_strings_changed_during_the_query_is_invalidated(caplog):
    logging.getLogger("app.data_sources.clients._pg_session").disabled = False
    conn = FakeConn(FakeDbapi("on"))
    prepare_pg_session(conn, BACKSLASH_READ)
    conn.connection.dbapi_connection.status[SCS] = "off"
    with caplog.at_level(logging.WARNING, logger="app.data_sources.clients._pg_session"):
        verify_pg_session_after(conn)
    assert conn.invalidated is True
    assert SCS in caplog.text


def test_a_session_already_discarded_is_left_alone():
    conn = FakeConn(FakeDbapi("on"))
    prepare_pg_session(conn, BACKSLASH_READ)
    conn.invalidated = True
    conn.connection = None  # an invalidated connection has no session to read
    verify_pg_session_after(conn)


@pytest.mark.parametrize("scs", ["on", "off"])
def test_a_session_still_reading_standard_strings_is_kept(scs):
    conn = FakeConn(FakeDbapi(scs))
    prepare_pg_session(conn, BACKSLASH_READ)
    verify_pg_session_after(conn)
    assert conn.invalidated is False


# --------------------------------------------------------------- the split for a caller that sends its own SET
#
# `pg_session_needs_switch(conn, sql)` decides without a round trip: True
# when the server reports the setting off, so the caller sends the switch
# itself, inside its own transaction, then calls `confirm_pg_session(conn,
# sql)`, which re-reads the reported setting and applies the refusal.

@pytest.mark.parametrize("sql", [BACKSLASH_READ, E_STRING_READ, PLAIN_READ])
def test_a_server_reading_standard_strings_needs_no_switch_and_nothing_is_sent(sql):
    conn = FakeConn(FakeDbapi("on"))
    assert _pg_session.pg_session_needs_switch(conn, sql) is False
    assert conn.sent == []
    verify_pg_session_after(conn)
    assert conn.invalidated is False


def test_a_server_reporting_standard_strings_off_needs_the_switch_and_the_decision_sends_nothing():
    conn = FakeConn(FakeDbapi("off"))
    assert _pg_session.pg_session_needs_switch(conn, BACKSLASH_READ) is True
    assert conn.sent == []


def test_after_the_callers_switch_the_session_is_confirmed_and_checked_after_the_read():
    dbapi = FakeDbapi("off")
    conn = FakeConn(dbapi)
    assert _pg_session.pg_session_needs_switch(conn, BACKSLASH_READ) is True
    dbapi.status[SCS] = "on"  # the caller's SET LOCAL ... = on
    _pg_session.confirm_pg_session(conn, BACKSLASH_READ)
    dbapi.status[SCS] = "off"  # the read turned it off again
    verify_pg_session_after(conn)
    assert conn.invalidated is True


@pytest.mark.parametrize("becomes", ["off", None])
@pytest.mark.parametrize("sql", [BACKSLASH_READ, E_BACKSLASH_READ])
def test_after_a_switch_that_did_not_take_a_backslash_literal_is_refused(becomes, sql):
    dbapi = FakeDbapi("off")
    conn = FakeConn(dbapi)
    assert _pg_session.pg_session_needs_switch(conn, sql) is True
    dbapi.status[SCS] = becomes
    with pytest.raises(ReadOnlyViolation, match="standard_conforming_strings"):
        _pg_session.confirm_pg_session(conn, sql)


def test_a_server_that_does_not_report_the_setting_refuses_a_backslash_literal_without_a_switch():
    conn = FakeConn(FakeDbapi(None))
    with pytest.raises(ReadOnlyViolation, match="standard_conforming_strings"):
        _pg_session.pg_session_needs_switch(conn, BACKSLASH_READ)
    assert _pg_session.pg_session_needs_switch(conn, PLAIN_READ) is False
    assert conn.sent == []


@pytest.mark.parametrize("dbapi", [
    FakeDbapi("off", server_version=90000, version_text="9.0.0"),
    FakeDbapi("on", server_version=150004, version_text="8.0.2 Redshift 1.0.1"),
])
def test_an_old_or_redshift_like_server_gets_the_redshift_rule_and_never_the_switch(dbapi):
    with pytest.raises(ReadOnlyViolation):
        _pg_session.pg_session_needs_switch(FakeConn(dbapi), BACKSLASH_READ)
    conn = FakeConn(dbapi)
    assert _pg_session.pg_session_needs_switch(conn, PLAIN_READ) is False
    assert conn.sent == []
