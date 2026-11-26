"""`PostgresqlClient.execute_query` reads in one message, then rolls back.

Contract, on an engine built for it (`engine_pool.pg_one_message`):

* the connection is checked to be idle, psycopg2's local `autocommit` flag
  is turned on, and one message is sent: `BEGIN READ ONLY;` (plus
  `SET LOCAL search_path TO …;` when schemas are pinned), a line break, then
  the checked query exactly as given;
* on a server reporting `standard_conforming_strings` off, `BEGIN READ ONLY;
  SET LOCAL standard_conforming_strings = on` goes first as its own
  message, and the query's message then has no BEGIN;
* after the read the connection must still be in the transaction, or the
  rows are not returned; the after-read literal check runs before the
  ROLLBACK;
* a connection idle after a message was sent (the query ended BOW's
  transaction, or the server refused the whole message) is dropped, on
  success and on error alike; one idle because nothing was sent is pooled;
* an error inside the prefix's `SET LOCAL search_path` is the schema
  setting error `connect()` gives;
* on every exit: ROLLBACK unless already idle, while autocommit is still
  on (a failed ROLLBACK is swallowed and the connection dropped); then idle
  or dropped; then the flag back off, unless the connection was dropped;
* an error names the checked query, and its `LINE n` counts from the
  query's first line;
* the configured schema items are checked before anything is sent.

An engine built with the switch off keeps the `connect(read_only=True)` read.

The pool, the psycopg2 connection and the result reader are faked at the
driver boundary: what the connection is sent, with which flag, and in what
order.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pandas as pd
import pytest
import sqlalchemy

from app.ai.code_execution.sql_guard import ReadOnlyViolation
from app.data_sources import query_cancellation
from app.data_sources.clients import postgresql_client
from app.data_sources.clients.postgresql_client import PostgresqlClient

IDLE, INTRANS, INERROR = 0, 2, 3
SCS = "standard_conforming_strings"
BS = chr(92)
SQL = "SELECT 1 AS a"


class FakeRaw:
    """A psycopg2 connection: its flag, its server-reported status and
    settings, and a log of what happened to it, in order."""

    def __init__(self, scs="on", status=IDLE, server_version=150004, version_text="15.4"):
        self.log = []
        self._autocommit = False
        self.status = status
        self.closed = 0
        self.params = {SCS: scs, "server_version": version_text}
        self.server_version = server_version
        self.scs_after_switch = "on"
        self.rollback_error = None
        self.rollback_ends_it = True
        self.invalidate_error = None
        self._readonly = None

    @property
    def readonly(self):
        return self._readonly

    @readonly.setter
    def readonly(self, value):
        self.log.append(("readonly", value, self._autocommit))
        self._readonly = value

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        self.log.append(("autocommit", value))
        self._autocommit = value

    def get_transaction_status(self):
        return self.status

    def get_parameter_status(self, name):
        return self.params.get(name)

    def cancel(self):
        self.log.append(("cancel",))


class FakeConn:
    """A SQLAlchemy Connection over `raw`."""

    def __init__(self, raw):
        self.raw = raw
        self.connection = SimpleNamespace(dbapi_connection=raw)
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.info = {}
        self.invalidated = False

    def exec_driver_sql(self, sql, parameters=None, execution_options=None):
        raw = self.raw
        raw.log.append(("send", sql, raw.autocommit))
        if sql == "ROLLBACK":
            if raw.rollback_error is not None:
                raise raw.rollback_error
            if raw.rollback_ends_it:
                raw.status = IDLE
            return
        if sql.startswith("BEGIN"):
            raw.status = INTRANS
        if f"SET LOCAL {SCS} = on" in sql:
            raw.params[SCS] = raw.scs_after_switch

    def execution_options(self, **options):
        return self

    def invalidate(self, exception=None):
        self.raw.log.append(("invalidate",))
        if self.raw.invalidate_error is not None:
            raise self.raw.invalidate_error
        self.invalidated = True
        self.raw.closed = 2

    def rollback(self):
        self.raw.log.append(("rollback",))

    def close(self):
        self.raw.log.append(("close",))


class FakeEngine:
    bow_pg_one_message = True

    def __init__(self, raw):
        self.raw = raw

    def connect(self):
        self.raw.log.append(("checkout",))
        return FakeConn(self.raw)


class OldEngine(FakeEngine):
    bow_pg_one_message = False


def _reader(result=None, error=None, then=None, error_status=INERROR):
    """The pandas reader at the driver: logs the message it was handed and
    what the flag was; a message holding BEGIN opens the transaction. An
    error leaves the connection at `error_status`: failed, or idle (the
    server refused the whole message, or the query had ended the
    transaction before it failed)."""

    def read(message, conn):
        raw = conn.connection.dbapi_connection
        raw.log.append(("read", message, raw.autocommit))
        if message.startswith("BEGIN"):
            raw.status = INTRANS
        if then is not None:
            then(raw, conn)
        if error is not None:
            raw.status = error_status
            raise error
        return pd.DataFrame({"a": [1]}) if result is None else result

    return read


@pytest.fixture
def rig(monkeypatch):
    def make(raw=None, engine=FakeEngine, schema=None, **reader):
        raw = raw or FakeRaw()
        monkeypatch.setattr(postgresql_client, "get_engine", lambda uri, **kwargs: engine(raw))
        monkeypatch.setattr(postgresql_client, "read_sql_exact", _reader(**reader))
        client = PostgresqlClient(host="db.invalid", port=5432, database="d", user="u", password="p", schema=schema)
        return client, raw

    return make


def _db_error(message: str, position="12"):
    class PgError(Exception):
        pass

    orig = PgError(message)
    orig.diag = SimpleNamespace(statement_position=position)
    return sqlalchemy.exc.ProgrammingError("the message as sent", None, orig)


# ------------------------------------------------------------------ the message and the order of things

def test_a_read_is_one_message_and_a_rollback_sent_while_autocommit_is_on(rig):
    client, raw = rig()
    df = client.execute_query(SQL)
    assert df.to_dict("records") == [{"a": 1}]
    assert raw.log == [
        ("checkout",),
        ("autocommit", True),
        ("read", "BEGIN READ ONLY;\n" + SQL, True),
        ("send", "ROLLBACK", True),
        ("autocommit", False),
        ("close",),
    ]


def test_pinned_schemas_go_into_the_same_message_as_written(rig):
    client, raw = rig(schema='rvw_a, "Sales", ' + "'x'")
    client.execute_query(SQL)
    reads = [entry for entry in raw.log if entry[0] == "read"]
    assert reads == [("read", "BEGIN READ ONLY; SET LOCAL search_path TO rvw_a, \"Sales\", 'x';\n" + SQL, True)]


def test_a_failed_read_is_rolled_back_before_the_flag_is_restored_and_its_error_is_raised(rig):
    """An error that leaves the transaction failed: rolled back and pooled."""
    error = _db_error("division by zero")
    client, raw = rig(error=error)
    with pytest.raises(sqlalchemy.exc.ProgrammingError) as raised:
        client.execute_query(SQL)
    assert raised.value is error
    assert raw.log[-3:] == [("send", "ROLLBACK", True), ("autocommit", False), ("close",)]
    assert ("invalidate",) not in raw.log


@pytest.mark.parametrize("error", [
    _db_error("division by zero", position=None),
    sqlalchemy.exc.ResourceClosedError("This result object does not return rows."),
], ids=["error-after-commit", "no-rows-after-commit"])
def test_a_read_that_fails_with_the_connection_idle_drops_it_without_restoring_the_flag(rig, error):
    """Idle after a send: the query ended BOW's transaction before it
    failed, or the server refused the whole message. Dropped, not pooled."""
    client, raw = rig(error=error, error_status=IDLE)
    with pytest.raises(type(error)):
        client.execute_query("SELECT 1 AS a; COMMIT; SELECT 1/0 AS b")
    assert ("send", "ROLLBACK", True) not in raw.log
    assert raw.log[-2:] == [("invalidate",), ("close",)]
    assert ("autocommit", False) not in raw.log


def test_a_syntax_error_in_the_second_message_after_the_switch_stays_pooled(rig):
    """With standard_conforming_strings off, BEGIN ran in its own message:
    the server's refusal of the query leaves the transaction failed."""
    client, raw = rig(raw=FakeRaw(scs="off"), error=_db_error('syntax error at or near "SELEC"', position="1"))
    with pytest.raises(sqlalchemy.exc.ProgrammingError):
        client.execute_query("SELEC 1")
    assert raw.log[-3:] == [("send", "ROLLBACK", True), ("autocommit", False), ("close",)]
    assert ("invalidate",) not in raw.log


def test_a_read_only_flag_left_set_is_cleared_before_autocommit_goes_on(rig):
    raw = FakeRaw()
    raw._readonly = False
    client, raw = rig(raw=raw)
    client.execute_query(SQL)
    assert raw.log[:3] == [("checkout",), ("readonly", None, False), ("autocommit", True)]


@pytest.mark.parametrize("fails", [False, True], ids=["read-ok", "read-failed"])
def test_a_rollback_that_fails_drops_the_connection_and_never_masks_the_outcome(rig, fails):
    raw = FakeRaw()
    raw.rollback_error = sqlalchemy.exc.OperationalError("ROLLBACK", None, Exception("server closed the connection"))
    error = _db_error("division by zero")
    client, raw = rig(raw=raw, error=error if fails else None)
    if fails:
        with pytest.raises(sqlalchemy.exc.ProgrammingError) as raised:
            client.execute_query(SQL)
        assert raised.value is error
    else:
        assert client.execute_query(SQL).to_dict("records") == [{"a": 1}]
    assert raw.log[-3:] == [("send", "ROLLBACK", True), ("invalidate",), ("close",)]
    assert ("autocommit", False) not in raw.log


def test_a_connection_still_in_a_transaction_after_the_rollback_is_dropped(rig):
    raw = FakeRaw()
    raw.rollback_ends_it = False
    client, raw = rig(raw=raw)
    client.execute_query(SQL)
    assert raw.log[-3:] == [("send", "ROLLBACK", True), ("invalidate",), ("close",)]
    assert ("autocommit", False) not in raw.log


def _idle_after_the_send(raw, conn):
    raw.status = IDLE


def _rollback_fails(raw, conn):
    raw.rollback_error = sqlalchemy.exc.OperationalError("ROLLBACK", None, Exception("server closed the connection"))


def _rollback_leaves_it_open(raw, conn):
    raw.rollback_ends_it = False


@pytest.mark.parametrize("then", [_idle_after_the_send, _rollback_fails, _rollback_leaves_it_open],
                         ids=["idle-after-send", "rollback-failed", "open-after-rollback"])
def test_a_drop_that_fails_leaves_autocommit_on_for_the_reset_backstop(rig, then):
    """A connection meant to be dropped keeps the flag on even when the
    drop itself fails, so the pool's reset listener drops it at checkin."""
    raw = FakeRaw()
    raw.invalidate_error = RuntimeError("invalidate failed")
    client, raw = rig(raw=raw, then=then)
    try:
        client.execute_query(SQL)
    except RuntimeError as e:
        assert "transaction" in str(e)
    assert ("invalidate",) in raw.log
    assert ("autocommit", False) not in raw.log
    assert raw.autocommit is True
    assert raw.log[-1] == ("close",)


def test_a_read_that_ended_its_own_transaction_returns_no_rows_and_drops_the_connection(rig):
    def committed(raw, conn):
        raw.status = IDLE

    client, raw = rig(then=committed)
    with pytest.raises(RuntimeError, match="transaction"):
        client.execute_query(SQL)
    assert ("send", "ROLLBACK", True) not in raw.log
    assert raw.log[-2:] == [("invalidate",), ("close",)]


def test_a_connection_that_is_not_idle_at_checkout_is_dropped_before_anything_is_sent(rig):
    client, raw = rig(raw=FakeRaw(status=INTRANS))
    with pytest.raises(RuntimeError):
        client.execute_query(SQL)
    assert [e for e in raw.log if e[0] in ("autocommit", "read", "send")] == []
    assert ("invalidate",) in raw.log and raw.log[-1] == ("close",)


def test_the_after_read_check_runs_before_the_rollback(rig):
    def turned_off(raw, conn):
        raw.params[SCS] = "off"

    client, raw = rig(then=turned_off)
    assert client.execute_query(SQL).to_dict("records") == [{"a": 1}]
    # The session no longer reads literals as written: dropped, not rolled back and pooled.
    assert raw.log[-2:] == [("invalidate",), ("close",)]
    assert ("send", "ROLLBACK", True) not in raw.log


def test_the_read_is_registered_for_cancellation(rig):
    seen = []

    def cancel_now(raw, conn):
        seen.append(query_cancellation.cancel_thread(client, threading.get_ident()))

    client, raw = rig(then=cancel_now)
    client.execute_query(SQL)
    assert seen == ["cancelled (postgresql cancel)"]
    assert ("cancel",) in raw.log
    assert query_cancellation.cancel_thread(client, threading.get_ident()) == "not_running"


# ------------------------------------------------------------------ standard_conforming_strings off

@pytest.mark.parametrize("schema, message", [
    (None, SQL),
    ("rvw_a", "SET LOCAL search_path TO rvw_a;\n" + SQL),
])
def test_on_a_server_with_standard_strings_off_the_switch_is_its_own_message_first(rig, schema, message):
    client, raw = rig(raw=FakeRaw(scs="off"), schema=schema)
    client.execute_query(SQL)
    assert [e for e in raw.log if e[0] in ("send", "read")] == [
        ("send", f"BEGIN READ ONLY; SET LOCAL {SCS} = on", True),
        ("read", message, True),
        ("send", "ROLLBACK", True),
    ]


def test_a_switch_that_did_not_take_refuses_a_backslash_literal_and_rolls_back(rig):
    raw = FakeRaw(scs="off")
    raw.scs_after_switch = "off"
    client, raw = rig(raw=raw)
    with pytest.raises(ReadOnlyViolation, match=SCS):
        client.execute_query("SELECT 'a" + BS + "b' AS v")
    assert [e for e in raw.log if e[0] == "read"] == []
    assert raw.log[-3:] == [("send", "ROLLBACK", True), ("autocommit", False), ("close",)]


def test_an_old_server_refuses_a_backslash_literal_before_anything_is_sent(rig):
    client, raw = rig(raw=FakeRaw(server_version=90000, version_text="9.0.0"))
    with pytest.raises(ReadOnlyViolation):
        client.execute_query("SELECT 'a" + BS + "b' AS v")
    assert [e for e in raw.log if e[0] in ("send", "read")] == []
    # Nothing was sent: the idle connection goes back to the pool.
    assert raw.log[-2:] == [("autocommit", False), ("close",)]
    assert ("invalidate",) not in raw.log


# ------------------------------------------------------------------ errors name the checked query

def test_an_error_names_the_checked_query_and_counts_lines_from_it(rig):
    sql = "SELECT 1 AS a,\n  nope AS b"
    error = _db_error('column "nope" does not exist\nLINE 3:   nope AS b\n          ^\n')
    client, _raw = rig(error=error)
    with pytest.raises(sqlalchemy.exc.ProgrammingError) as raised:
        client.execute_query(sql)
    e = raised.value
    assert e.statement == sql
    assert str(e.orig) == 'column "nope" does not exist\nLINE 2:   nope AS b\n          ^\n'
    assert "LINE 2:   nope AS b\n          ^" in e.args[0]
    assert "BEGIN READ ONLY" not in str(e)


def test_a_renumbered_line_keeps_its_caret_under_the_same_character(rig):
    sql = "\n" * 9 + "nope"
    error = _db_error('syntax error at or near "nope"\nLINE 10: nope\n         ^\n')
    client, _raw = rig(error=error)
    with pytest.raises(sqlalchemy.exc.ProgrammingError) as raised:
        client.execute_query(sql)
    assert str(raised.value.orig) == 'syntax error at or near "nope"\nLINE 9: nope\n        ^\n'


def test_an_error_inside_the_prefix_is_a_schema_setting_error(rig):
    """The server put it in `SET LOCAL search_path` (position 43 of the
    message): the error `connect()` gives for a schema it cannot apply,
    chained, without the schema names. Refused whole, so dropped."""
    error = _db_error('syntax error at or near "user"\nLINE 1: BEGIN READ ONLY; SET LOCAL search_path TO user;\n'
                      '                                                 ^\n', position="43")
    client, raw = rig(error=error, error_status=IDLE, schema="user")
    with pytest.raises(RuntimeError) as raised:
        client.execute_query(SQL)
    assert str(raised.value) == str(postgresql_client._schema_setting_error(error))
    assert str(raised.value).startswith("The connection's schema setting could not be applied on the server")
    assert raised.value.__cause__ is error
    assert "user" not in str(raised.value)
    assert raw.log[-2:] == [("invalidate",), ("close",)]


def test_an_error_just_past_the_prefix_is_the_querys_own(rig):
    prefix = "BEGIN READ ONLY; SET LOCAL search_path TO rvw_a;\n"
    error = _db_error('syntax error at or near "SELEC"\nLINE 2: SELEC 1\n        ^\n', position=str(len(prefix) + 1))
    client, _raw = rig(error=error, error_status=IDLE, schema="rvw_a")
    with pytest.raises(sqlalchemy.exc.ProgrammingError) as raised:
        client.execute_query("SELEC 1")
    assert raised.value is error and str(error.orig).startswith('syntax error at or near "SELEC"\nLINE 1: SELEC 1')


def test_an_error_without_a_position_keeps_its_text(rig):
    text = "canceling statement due to user request\nLINE 2 is not a position here\n"
    error = _db_error(text, position=None)
    client, _raw = rig(error=error)
    with pytest.raises(sqlalchemy.exc.ProgrammingError) as raised:
        client.execute_query(SQL)
    assert str(raised.value.orig) == text
    assert raised.value.statement == SQL


# ------------------------------------------------------------------ the switch and the schema rule

def test_an_engine_built_with_the_switch_off_keeps_the_read_only_connect_read(rig):
    client, raw = rig(engine=OldEngine)
    client.execute_query(SQL)
    assert [e for e in raw.log if e[0] == "read"] == [("read", SQL, False)]
    assert not [e for e in raw.log if e[0] == "autocommit"]
    assert raw.log[-2:] == [("rollback",), ("close",)]


@pytest.mark.parametrize("schema", ["rvw_a;", "rvw_a --x", "rvw" + BS + "a", "a /* b */", "a b", "\"a\"\"", "1a"])
def test_a_schema_item_the_rule_refuses_fails_before_a_connection_is_taken(rig, schema):
    client, raw = rig(schema=schema)
    with pytest.raises(RuntimeError, match="schema") as raised:
        client.execute_query(SQL)
    assert schema not in str(raised.value)
    assert raw.log == []


ACCEPTED_SCHEMAS = [
    "rvw_a", "RVW_A", "_x1$", '"Sales"', '"a:b"', '"a""b"', 'U&"Sales"', "'Sales'", "'it''s'", "123",
    '"$user", public', chr(0x05E9) + chr(0x05DC) + chr(0x05D5) + chr(0x05DD), "caf" + chr(0xE9),
    '"a%b"',
    # Non-ASCII characters are identifier characters to Postgres: Hebrew
    # geresh, gershayim and maqaf, currency signs, a combining mark.
    "a" + chr(0x05F3), chr(0x05E6) + chr(0x05F4) + chr(0x05DC), "a" + chr(0x05BE) + "b",
    "price" + chr(0x20AA), chr(0x20AC) + "x", "e" + chr(0x0301),
]


@pytest.mark.parametrize("schema", ACCEPTED_SCHEMAS)
def test_the_schema_rule_keeps_every_accepted_item_as_written(schema):
    items = [s.strip() for s in schema.split(",")]
    assert postgresql_client.checked_search_path(items) == ", ".join(items)


@pytest.mark.parametrize("item", [
    "rvw_a;", "rvw_a --x", "a/*b*/", "rvw" + BS + "a", '"a' + BS + 'b"', "'a" + BS + "b'", '"a"b"', "'a'b'",
    '"a\nb"', "a b", "1a", "", '""', "-", "a.b", 'U&"a" UESCAPE \'!\'',
    "a%b", "$a", "1.5", "-1", '"a\tb"',
    # Whitespace, control and format characters outside ASCII: NBSP, an
    # ideographic space, a line separator, NEL, zero-width space and joiner,
    # a soft hyphen, a byte order mark.
    "a" + chr(0xA0) + "b", "a" + chr(0x3000), "a" + chr(0x2028), "a" + chr(0x85), "a" + chr(0x200B) + "b",
    "a" + chr(0x200D), chr(0xAD) + "a", chr(0xFEFF) + "a",
])
def test_the_schema_rule_refuses_anything_else_naming_the_setting_not_the_value(item):
    with pytest.raises(ValueError, match="schema") as raised:
        postgresql_client.checked_search_path(["public", item])
    if item:
        assert item not in str(raised.value)


# ------------------------------------------------------------------ the URL the read connects with

class LoginEngine(FakeEngine):
    """A server that accepts one (user, password); logs each login."""

    def __init__(self, raw, url, accepted):
        super().__init__(raw)
        self.url = sqlalchemy.engine.make_url(url)
        self.accepted = accepted

    def connect(self):
        self.raw.log.append(("login", self.url.username, self.url.password))
        if (self.url.username, self.url.password) != self.accepted:
            raise RuntimeError("password authentication failed")
        return super().connect()


@pytest.fixture
def forget_decoded_forms():
    from app.data_sources.clients import _sa_url

    _sa_url._decoded_ok.clear()
    yield
    _sa_url._decoded_ok.clear()


def test_credentials_saved_percent_encoded_still_connect_for_the_one_message_read(monkeypatch, forget_decoded_forms):
    raw = FakeRaw()
    monkeypatch.setattr(postgresql_client, "get_engine",
                        lambda url, **kwargs: LoginEngine(raw, url, ("svc@corp", "p@ss/w:rd")))
    monkeypatch.setattr(postgresql_client, "read_sql_exact", _reader())
    client = PostgresqlClient(host="db.invalid", port=5432, database="d", user="svc%40corp",
                              password="p%40ss%2Fw%3Ard")

    df = client.execute_query(SQL)

    assert df.to_dict("records") == [{"a": 1}]
    assert [e for e in raw.log if e[0] == "login"] == [
        ("login", "svc%40corp", "p%40ss%2Fw%3Ard"), ("login", "svc@corp", "p@ss/w:rd"),
    ]
    assert [e for e in raw.log if e[0] == "read"] == [("read", "BEGIN READ ONLY;\n" + SQL, True)]


@pytest.mark.parametrize("field, value", [
    ("database", "a/b@other.example:9999/c"), ("user", "u?host=10.1.2.3"),
    ("password", "p@ss:w/rd#x=1&y=2 %41"), ("password", "p?passfile=/etc/hostname"),
])
def test_the_one_message_read_hands_the_driver_the_fields_as_they_are(monkeypatch, field, value):
    import psycopg2

    from app.data_sources import engine_pool

    class Dialed(Exception):
        pass

    seen = {}

    def fake_connect(*args, **kwargs):
        seen.setdefault("kwargs", kwargs)
        raise Dialed()

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    values = dict(host="db.example.test", port=5432, database="shop", user="reader", password="pw")
    values[field] = value
    with engine_pool.ephemeral():
        with pytest.raises(Exception):
            PostgresqlClient(**values).execute_query(SQL)

    kw = seen["kwargs"]
    assert kw["host"] == "db.example.test" and kw["port"] == 5432
    assert kw["dbname"] == values["database"]
    assert kw["user"] == values["user"]
    assert kw["password"] == values["password"]
    assert set(kw) <= {"host", "port", "dbname", "user", "password", "sslmode"}
