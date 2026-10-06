"""'Check connection' against Oracle must end with an answer, never hang.

Reported: an Oracle 10.2 server behind the bundled 19c client accepted the
login (a session appeared on the database) and the add-connection modal sat on
"Connecting…" indefinitely — the probe and the schema-access read both ran with
no time limit. These pin the contract: each half of the check either returns
or fails with an actionable message within its bound, the abandoned source
statement is cancelled, and clients that don't opt in behave exactly as before.

Also covers ORACLE_CLIENT_LIB_DIR, the opt-in for an older Instant Client.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

import app.data_sources.clients.oracledb_client as oc
from app.data_sources import query_cancellation as qc
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService


def _client(**overrides):
    params = dict(host="127.0.0.1", port=1521, service_name="svc", user="u", password="p")
    params.update(overrides)
    return oc.OracledbClient(**params)


@pytest.fixture()
def silent_listener():
    """A TCP listener that accepts connections and never sends a byte —
    from the client's side, a login the server never finishes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    accepted = []
    stop = threading.Event()

    def _accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                accepted.append(srv.accept()[0])
            except OSError:
                continue

    threading.Thread(target=_accept, daemon=True).start()
    yield srv.getsockname()[1]
    stop.set()
    for s in accepted:
        s.close()
    srv.close()


# ---------------------------------------------------------------------------
# Connect half: the login probe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bound_s", [1, 2])
async def test_probe_against_a_server_that_never_answers_fails_within_its_bound(
    silent_listener, monkeypatch, bound_s
):
    monkeypatch.setattr(oc, "CONNECT_TIMEOUT_S", bound_s, raising=False)
    started = time.monotonic()
    status = await _client(port=silent_listener).atest_connection()
    elapsed = time.monotonic() - started

    assert status["success"] is False
    assert status["message"]
    assert "ORACLE_CLIENT_LIB_DIR" in status["message"]  # names the way out
    assert elapsed < bound_s + 5


@pytest.mark.asyncio
async def test_probe_result_passes_through_unchanged_when_it_finishes(monkeypatch):
    monkeypatch.setattr(oc, "CONNECT_TIMEOUT_S", 5, raising=False)
    expected = {"success": True, "message": "Successfully connected to Oracle"}
    monkeypatch.setattr(oc.OracledbClient, "test_connection", lambda self: expected)
    assert await _client().atest_connection() == expected


def test_thin_mode_unsupported_server_error_explains_the_fix(monkeypatch):
    def refuse(self):
        raise RuntimeError("DPY-3010: connections to this database server version are not supported")
    monkeypatch.setattr(oc.OracledbClient, "connect", refuse)
    status = _client().test_connection()
    assert status["success"] is False
    assert "DPY-3010" in status["message"]
    assert "ORACLE_CLIENT_LIB_DIR" in status["message"]


# ---------------------------------------------------------------------------
# Schema half: the catalog read behind "Check connection"
# ---------------------------------------------------------------------------

class _CatalogClient:
    """Stands in for a SQL client at the driver boundary."""

    def __init__(self, tables, block: threading.Event | None = None, timeout_s=None):
        self._tables = tables
        self._block = block
        if timeout_s is not None:
            self.validation_timeout_s = timeout_s
            self.validation_timeout_message = "slow catalog after {seconds}s"

    def get_schemas(self):
        if self._block is not None:
            self._block.wait(30)
        return self._tables

    async def aget_schemas(self):
        return self.get_schemas()


@pytest.fixture(params=[ConnectionService, DataSourceService], ids=["connection", "data_source"])
def validator(request):
    return request.param()


@pytest.mark.asyncio
async def test_catalog_read_that_never_returns_fails_with_the_clients_message(validator):
    release = threading.Event()
    try:
        status = await validator._avalidate_schema_access(
            _CatalogClient(["t"], block=release, timeout_s=1)
        )
    finally:
        release.set()
    assert status["success"] is False
    assert status["message"] == "slow catalog after 1s"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s", [None, 5])
async def test_catalog_read_that_finishes_is_counted_with_or_without_a_bound(validator, timeout_s):
    status = await validator._avalidate_schema_access(
        _CatalogClient(["a", "b", "c"], timeout_s=timeout_s)
    )
    assert status["success"] is True
    assert status["table_count"] == 3


def test_oracle_client_declares_a_catalog_bound():
    client = _client()
    assert client.validation_timeout_s and client.validation_timeout_s > 0
    assert "{seconds}" in client.validation_timeout_message


# ---------------------------------------------------------------------------
# run_bounded: the abandoned source statement is cancelled
# ---------------------------------------------------------------------------

class _FakeOracleConn:
    """Just enough of a SQLAlchemy Connection for query_cancellation."""

    def __init__(self):
        self.cancelled = threading.Event()
        dialect = type("D", (), {"name": "oracle"})()
        self.engine = type("E", (), {"dialect": dialect})()
        raw = type("Raw", (), {"cancel": lambda _self: self.cancelled.set()})()
        self.connection = type("Fairy", (), {"dbapi_connection": raw})()


@pytest.mark.asyncio
async def test_timed_out_call_has_its_source_statement_cancelled():
    owner = object()
    conn = _FakeOracleConn()

    def stuck_query():
        with qc.track(owner, conn):
            conn.cancelled.wait(30)  # an OCI break is what ends a stuck execute()

    with pytest.raises(qc.SourceCallTimeout):
        await qc.run_bounded(owner, stuck_query, 1)
    assert conn.cancelled.is_set()


@pytest.mark.asyncio
async def test_errors_raised_by_the_call_itself_propagate_unchanged():
    def boom():
        raise TimeoutError("driver socket timeout")

    with pytest.raises(TimeoutError) as info:
        await qc.run_bounded(object(), boom, 5)
    assert not isinstance(info.value, qc.SourceCallTimeout)


# ---------------------------------------------------------------------------
# ORACLE_CLIENT_LIB_DIR: opt-in Instant Client, default unchanged
# ---------------------------------------------------------------------------

def _record_init(monkeypatch, fail_lib_dir=False):
    calls = []

    def init(**kwargs):
        calls.append(kwargs)
        if fail_lib_dir and "lib_dir" in kwargs:
            raise Exception("DPI-1047: Cannot locate a 64-bit Oracle Client library")

    monkeypatch.setattr(oc.oracledb, "init_oracle_client", init)
    return calls


def test_without_lib_dir_the_default_libraries_are_loaded(monkeypatch):
    monkeypatch.delenv("ORACLE_CLIENT_LIB_DIR", raising=False)
    calls = _record_init(monkeypatch)
    assert oc.init_thick_mode_if_available() is True
    assert calls == [{}]


@pytest.mark.parametrize("lib_dir", ["/opt/oracle/instantclient_11_2", "/mnt/ic"])
def test_lib_dir_selects_that_client(monkeypatch, lib_dir):
    monkeypatch.setenv("ORACLE_CLIENT_LIB_DIR", lib_dir)
    calls = _record_init(monkeypatch)
    assert oc.init_thick_mode_if_available() is True
    assert calls == [{"lib_dir": lib_dir}]


def test_unloadable_lib_dir_falls_back_to_the_default_libraries(monkeypatch):
    monkeypatch.setenv("ORACLE_CLIENT_LIB_DIR", "/nope")
    calls = _record_init(monkeypatch, fail_lib_dir=True)
    assert oc.init_thick_mode_if_available() is True
    assert calls[-1] == {}


def test_thin_mode_opt_out_still_wins_over_lib_dir(monkeypatch):
    monkeypatch.setenv("ORACLE_CLIENT_LIB_DIR", "/opt/oracle/instantclient_11_2")
    monkeypatch.setenv("ORACLE_THICK_MODE", "0")
    calls = _record_init(monkeypatch)
    assert oc.init_thick_mode_if_available() is False
    assert calls == []


@pytest.mark.asyncio
async def test_bounded_call_sees_the_callers_context_like_to_thread():
    import contextvars
    var = contextvars.ContextVar("probe", default="unset")
    var.set("caller-value")
    assert await qc.run_bounded(object(), var.get, 5) == "caller-value"
