"""Check SQL's authenticated identity before yielding any user-query connection."""
from types import SimpleNamespace
import pytest
import sqlalchemy
from app.data_sources.clients.mssql_client import MSSQLClient
from app.data_sources import engine_pool, kerberos
from tests.unit.test_mssql_kerberos import _install_fake_gssapi
from urllib.parse import unquote_plus


@pytest.mark.parametrize("kerberos", [False, True])
def test_sql_tls_verifies_server_by_default(kerberos):
    uri = unquote_plus(MSSQLClient("sql.example.test", 1433, "synthetic",
        use_kerberos=kerberos).sql_server_uri)
    assert "TrustServerCertificate=no;" in uri
    assert "Encrypt=yes;" in uri


@pytest.mark.parametrize("field", ["host", "database", "user", "password"])
def test_odbc_values_cannot_inject_authentication_options(field):
    values = dict(host="sql.example.test", port=1433, database="synthetic",
                  user="reader", password="secret")
    values[field] = "value};Trusted_Connection=yes;UID=other"
    uri = unquote_plus(MSSQLClient(**values).sql_server_uri)
    suffix = ",1433" if field == "host" else ""
    assert "{value}};Trusted_Connection=yes;UID=other" + suffix + "}" in uri


def test_extra_odbc_keyword_cannot_inject_another_keyword():
    with pytest.raises(ValueError):
        _ = MSSQLClient("sql.example.test", 1433, "synthetic",
            additional_params={"ApplicationIntent=ReadOnly;UID": "other"}).sql_server_uri


def test_extra_odbc_value_is_escaped():
    uri = unquote_plus(MSSQLClient("sql.example.test", 1433, "synthetic",
        additional_params={"ApplicationIntent": "ReadOnly;UID=other"}).sql_server_uri)
    assert "ApplicationIntent={ReadOnly;UID=other};" in uri


@pytest.mark.parametrize("sid,scheme,allowed", [
    (b"expected-sid", "KERBEROS", True),
    (b"service-sid", "KERBEROS", False),
    (b"expected-sid", "NTLM", False),
    (None, "KERBEROS", False),
])
def test_only_matching_sql_identity_reaches_user_query(monkeypatch, tmp_path, sid, scheme, allowed):
    _install_fake_gssapi(monkeypatch, [], [])
    monkeypatch.setattr(kerberos, "_manager", kerberos.KerberosTicketManager(ccache_dir=str(tmp_path)))
    connections = []
    class DriverConnection:
        def __init__(self):
            self.info = {}
            self.invalidated = False
            self.closed = False
            connections.append(self)
        def execute(self, statement):
            return SimpleNamespace(one=lambda: (sid, scheme), scalar=lambda: 1)
        def invalidate(self):
            self.invalidated = True
        def close(self):
            self.closed = True
    class DriverEngine:
        def __init__(self, *a, **kw): pass
        def connect(self): return DriverConnection()
        def dispose(self): pass
    monkeypatch.setattr(sqlalchemy, "create_engine", DriverEngine)
    engine_pool.dispose_all()
    client = MSSQLClient("sql.example.test", 1433, "synthetic", use_kerberos=True,
        kerberos_impersonate="person@EXAMPLE.TEST", kerberos_expected_sid=b"expected-sid".hex())
    try:
        if allowed:
            with client.connect():
                pass
        else:
            with pytest.raises(RuntimeError):
                with client.connect():
                    pytest.fail("Untrusted SQL identity reached user query")
            assert connections[0].invalidated
        assert connections[0].closed
    finally:
        engine_pool.dispose_all()
