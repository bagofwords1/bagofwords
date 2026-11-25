"""Connection fields reach the database driver as the fields they are.

The contract: whatever an admin types into host / database / user / password
arrives at the driver as that one value. A field can never add driver
options, move the connection to another host, or read a local file. Checked
at the driver boundary: the DBAPI ``connect`` is the only thing stubbed.
"""
from __future__ import annotations

import pytest

from app.data_sources import engine_pool


class _Dialed(Exception):
    """Raised by the stub driver once it has recorded the call."""


def _capture(monkeypatch, module, attr="connect"):
    seen = {}

    def fake_connect(*args, **kwargs):
        seen.setdefault("args", args)
        seen.setdefault("kwargs", kwargs)
        raise _Dialed()

    monkeypatch.setattr(module, attr, fake_connect)
    return seen


def _dial(client):
    """Open a connection through the client's own path; the stub stops it."""
    with engine_pool.ephemeral():
        with pytest.raises(Exception):
            with client.connect():
                pass


# Values that must stay inert inside one field.
HOSTILE = [
    "a/b@other.example:9999/c",
    "p@ss:w/rd#x=1&y=2 %41",
    "shop#x&local_infile=1",
]


@pytest.mark.parametrize("module_name, cls, driver", [
    ("mysql_client", "MysqlClient", "pymysql"),
    ("mariadb_client", "MariadbClient", "pymysql"),
])
@pytest.mark.parametrize("field, value", [(f, v) for f in ("database", "user", "password") for v in HOSTILE]
                         + [("user", "u?local_infile=1"), ("password", "p?read_default_file=/etc/hostname")])
def test_mysql_family_fields_stay_fields_and_local_files_are_never_served(
    monkeypatch, module_name, cls, driver, field, value
):
    import importlib

    seen = _capture(monkeypatch, importlib.import_module(driver))
    values = dict(host="db.example.test", port=3306, database="shop", user="reader", password="pw")
    values[field] = value
    client = getattr(importlib.import_module(f"app.data_sources.clients.{module_name}"), cls)(**values)
    _dial(client)

    kw = seen["kwargs"]
    assert kw["host"] == "db.example.test" and kw["port"] == 3306
    assert kw["database"] == values["database"]
    assert kw["user"] == values["user"]
    assert kw["password"] == values["password"]
    assert kw["local_infile"] is False
    # Nothing beyond the connection's own fields was derived from them.
    assert set(kw) <= {"host", "port", "database", "user", "password", "local_infile", "client_flag"}


@pytest.mark.parametrize("field, value", [(f, v) for f in ("database", "user", "password") for v in HOSTILE]
                         + [("user", "u?host=10.1.2.3"), ("password", "p?passfile=/etc/hostname")])
def test_postgres_fields_stay_fields(monkeypatch, field, value):
    import psycopg2

    from app.data_sources.clients.postgresql_client import PostgresqlClient

    seen = _capture(monkeypatch, psycopg2)
    values = dict(host="db.example.test", port=5432, database="shop", user="reader", password="pw")
    values[field] = value
    _dial(PostgresqlClient(**values))

    kw = seen["kwargs"]
    assert kw["host"] == "db.example.test" and kw["port"] == 5432
    assert kw["dbname"] == values["database"]
    assert kw["user"] == values["user"]
    assert kw["password"] == values["password"]
    assert set(kw) <= {"host", "port", "dbname", "user", "password", "sslmode"}


def test_trino_fields_stay_fields(monkeypatch):
    import trino.dbapi

    from app.data_sources.clients.trino_client import TrinoClient

    seen = _capture(monkeypatch, trino.dbapi)
    _dial(TrinoClient(host="trino.example.test", port=8443, catalog="lake?source=x&cert=/a&key=/b",
                      schema="s/t", user="svc@corp", password="p@ss/w:rd", protocol="https"))
    kw = seen["kwargs"]
    assert kw["host"] == "trino.example.test" and kw["port"] == 8443
    assert kw["catalog"] == "lake?source=x&cert=/a&key=/b" and kw["schema"] == "s/t"
    assert kw["user"] == "svc@corp"
    assert type(kw["auth"]).__name__ == "BasicAuthentication"


def test_oracle_host_and_service_name_cannot_carry_descriptor_syntax(monkeypatch):
    import oracledb

    from app.data_sources.clients.oracledb_client import OracledbClient

    seen = _capture(monkeypatch, oracledb)
    _dial(OracledbClient(host="ora.example.test", port=1521, service_name="orcl.example.test",
                         user="reader", password="p@ss/w:rd?x=1"))
    assert seen["kwargs"]["user"] == "reader" and seen["kwargs"]["password"] == "p@ss/w:rd?x=1"
    assert "HOST=ora.example.test" in seen["kwargs"]["dsn"]
    assert "SERVICE_NAME=orcl.example.test" in seen["kwargs"]["dsn"]

    seen.clear()
    for bad in (dict(service_name="orcl)(HOST=other.example.test"), dict(host="ora)(PORT=1")):
        values = dict(host="ora.example.test", port=1521, service_name="orcl", user="reader", password="pw")
        values.update(bad)
        _dial(OracledbClient(**values))
        assert not seen, "the driver must not be reached with a descriptor built from that value"
        with pytest.raises(ValueError):
            OracledbClient(**values, use_tcps=True)._connect_args()


@pytest.mark.parametrize("module_name, cls, extra", [
    ("mysql_client", "MysqlClient", {}),
    ("mariadb_client", "MariadbClient", {}),
    ("postgresql_client", "PostgresqlClient", {}),
])
@pytest.mark.parametrize("host", ["db.example.test/x?opt=1#", "db.example.test@other.example.test", "a b"])  # noqa: E501
def test_host_with_url_syntax_is_refused_before_the_driver(monkeypatch, module_name, cls, extra, host):
    import importlib

    import psycopg2
    import pymysql

    seen = _capture(monkeypatch, pymysql)
    seen2 = _capture(monkeypatch, psycopg2)
    client = getattr(importlib.import_module(f"app.data_sources.clients.{module_name}"), cls)(
        host=host, port=1234, database="shop", user="reader", password="pw", **extra)
    _dial(client)
    assert not seen and not seen2


# Options written after the database name: saved connections rely on them.

def _pg_kwargs(monkeypatch, database):
    import psycopg2

    from app.data_sources.clients.postgresql_client import PostgresqlClient

    seen = _capture(monkeypatch, psycopg2)
    _dial(PostgresqlClient(host="db.example.test", port=5432, database=database, user="reader", password="pw"))
    return seen["kwargs"]


def _mysql_kwargs(monkeypatch, database, cls="MysqlClient", module="mysql_client"):
    import importlib

    import pymysql

    seen = _capture(monkeypatch, pymysql)
    client = getattr(importlib.import_module(f"app.data_sources.clients.{module}"), cls)(
        host="db.example.test", port=3306, database=database, user="reader", password="pw")
    _dial(client)
    return seen["kwargs"]


@pytest.mark.parametrize("database, expected", [
    ("mydb?options=-csearch_path%3Dsales", {"options": "-csearch_path=sales"}),
    ("mydb?application_name=bow&connect_timeout=5", {"application_name": "bow", "connect_timeout": "5"}),
    ("mydb?target_session_attrs=read-write&client_encoding=utf8",
     {"target_session_attrs": "read-write", "client_encoding": "utf8"}),
])
def test_postgres_options_after_the_database_name_keep_working(monkeypatch, database, expected):
    kw = _pg_kwargs(monkeypatch, database)
    assert kw["dbname"] == "mydb" and kw["host"] == "db.example.test" and kw["port"] == 5432
    for key, value in expected.items():
        assert kw[key] == value


@pytest.mark.parametrize("option", [
    "host=10.1.2.3", "hostaddr=10.1.2.3", "port=64329", "passfile=/etc/hostname", "service=othersvc9",
    "servicefile=/etc/hostname", "sslcrl=/etc/hostname", "krbsrvname=zzsvc9", "user=postgres9", "dbname=otherdb9",
    "sslrootcert=/etc/shadow", "sslkey=/etc/ssl/private/x.key",
])
def test_postgres_options_that_redirect_or_read_files_are_dropped(monkeypatch, caplog, option):
    with caplog.at_level("WARNING"):
        kw = _pg_kwargs(monkeypatch, f"mydb?application_name=bow&{option}&connect_timeout=5")
    assert kw["dbname"] == "mydb" and kw["host"] == "db.example.test" and kw["port"] == 5432
    assert kw["user"] == "reader" and kw["password"] == "pw"
    assert kw["application_name"] == "bow" and kw["connect_timeout"] == "5"
    key = option.split("=")[0]
    assert set(kw) <= {"host", "port", "dbname", "user", "password", "sslmode", "application_name", "connect_timeout"}
    assert any(repr(key) in r.getMessage() for r in caplog.records)
    assert not any(option.split("=")[1] in r.getMessage() for r in caplog.records)


def test_postgres_option_values_cannot_add_more_options(monkeypatch):
    kw = _pg_kwargs(monkeypatch, "mydb?application_name=a%26host%3D10.1.2.3%3Fport%3D1")
    assert kw["application_name"] == "a&host=10.1.2.3?port=1"
    assert kw["host"] == "db.example.test" and kw["port"] == 5432


@pytest.mark.parametrize("cls, module", [("MysqlClient", "mysql_client"), ("MariadbClient", "mariadb_client")])
def test_mysql_options_after_the_database_name_keep_working(monkeypatch, cls, module):
    kw = _mysql_kwargs(monkeypatch, "shop?charset=utf8mb4&connect_timeout=7", cls, module)
    assert kw["database"] == "shop" and kw["charset"] == "utf8mb4" and kw["connect_timeout"] == 7
    assert kw["local_infile"] is False


@pytest.mark.parametrize("option", [
    "local_infile=1", "read_default_file=/etc/hostname", "read_default_group=client", "init_command=SET+x%3D1",
    "unix_socket=/tmp/s.sock", "conv=x", "cursorclass=x", "plugin_dir=/tmp", "host=10.1.2.3", "port=1",
    "ssl_ca=/etc/shadow",
])
@pytest.mark.parametrize("cls, module", [("MysqlClient", "mysql_client"), ("MariadbClient", "mariadb_client")])
def test_mysql_options_that_redirect_or_touch_files_are_dropped(monkeypatch, option, cls, module):
    kw = _mysql_kwargs(monkeypatch, f"shop?charset=utf8mb4&{option}", cls, module)
    assert kw["database"] == "shop" and kw["charset"] == "utf8mb4"
    assert kw["host"] == "db.example.test" and kw["port"] == 3306
    assert kw["local_infile"] is False
    assert set(kw) <= {"host", "port", "database", "user", "password", "local_infile", "client_flag", "charset"}


def test_mongodb_fields_cannot_add_uri_options():
    from pymongo.uri_parser import parse_uri

    from app.data_sources.clients.mongodb_client import MongodbClient

    client = MongodbClient(host="mongo.example.test", port=27017, database="shop",
                           user="reader?tls=false", password="p@ss/w:rd&x=1", auth_source="admin&directConnection=true")
    parsed = parse_uri(client._build_uri(), warn=False)
    assert parsed["nodelist"] == [("mongo.example.test", 27017)]
    assert parsed["database"] == "shop"
    assert parsed["username"] == "reader?tls=false" and parsed["password"] == "p@ss/w:rd&x=1"
    assert {k.lower() for k in parsed["options"]} == {"authsource"}
    assert parsed["options"]["authSource"] == "admin&directConnection=true"

    # A replica-set seed list keeps working.
    seeds = MongodbClient(host="m1.example.test:27017,m2.example.test", port=27018, database="shop")
    assert parse_uri(seeds._build_uri(), warn=False)["nodelist"] == [
        ("m1.example.test", 27017), ("m2.example.test", 27018)]

    with pytest.raises(ValueError):
        MongodbClient(host="mongo.example.test@other.example.test", port=27017, database="shop")._build_uri()


def test_mongodb_options_after_the_database_or_host_keep_working():
    from pymongo.uri_parser import parse_uri

    from app.data_sources.clients.mongodb_client import MongodbClient

    client = MongodbClient(host="m1.example.test,m2.example.test/?readPreference=secondary", port=27017,
                           database="shop?replicaSet=rs0&authSource=users", user="reader", password="pw")
    parsed = parse_uri(client._build_uri(), warn=False)
    assert parsed["nodelist"] == [("m1.example.test", 27017), ("m2.example.test", 27017)]
    assert parsed["database"] == "shop" and client._parts()[1] == "shop"
    assert parsed["options"]["replicaSet"] == "rs0" and parsed["options"]["authSource"] == "users"
    assert parsed["options"]["readPreference"] == "secondary"

    # A file option that points at a protected location is dropped; the rest stays.
    risky = MongodbClient(host="mongo.example.test", port=27017,
                          database="shop?replicaSet=rs0&tlsCAFile=/etc/shadow&tlsCertificateKeyFile=/etc/ssl/private/k.pem")
    options = parse_uri(risky._build_uri(), warn=False, validate=False)["options"]
    assert {k.lower() for k in options} == {"replicaset"}


def test_spark_connect_host_cannot_add_connection_parameters():
    from app.data_sources.clients.spark_connect_client import SparkConnectClient

    assert SparkConnectClient(host="spark.example.test", port=15002, token="t;x").remote_url == (
        "sc://spark.example.test:15002/;token=t%3Bx")
    for host in ("spark.example.test/;token=other", "spark.example.test;use_ssl=false"):
        with pytest.raises(ValueError):
            _ = SparkConnectClient(host=host, port=15002).remote_url


def test_sybase_credentials_cannot_add_odbc_keywords():
    pytest.importorskip("pyodbc")
    from app.data_sources.clients.sybase_client import SybaseClient

    plain = SybaseClient(host="syb.example.test", port=2638, database="db", user="reader", password="p}w d")
    assert "UID=reader;PWD=p}w d;" in plain._connection_string()
    hostile = SybaseClient(host="syb.example.test", port=2638, database="db",
                           user="reader", password="pw;SERVERNAME=other")
    assert "PWD={pw;SERVERNAME=other};" in hostile._connection_string()


def test_special_characters_in_credentials_keep_separate_pool_identities():
    """Two passwords that differ only in URL-special characters are two identities."""
    from app.data_sources.clients.postgresql_client import PostgresqlClient
    from app.data_sources.engine_pool import _key

    a = PostgresqlClient(host="h", port=5432, database="d", user="svc", password="a@b")
    b = PostgresqlClient(host="h", port=5432, database="d", user="svc", password="a%40b")
    assert _key(a.pg_uri, None, None) != _key(b.pg_uri, None, None)


# ── credentials that were saved percent-encoded keep connecting ─────────────

def _login_server(monkeypatch, client_module, accepted):
    """Stands in for the pool + driver: accepts exactly one (user, password)."""
    attempts = []

    class _Conn:
        def execute(self, *a, **k):
            return None

        def close(self):
            pass

    class _Engine:
        def __init__(self, url):
            self.url = url

        def connect(self):
            attempts.append((self.url.username, self.url.password))
            if attempts[-1] != accepted:
                raise RuntimeError("password authentication failed")
            return _Conn()

    monkeypatch.setattr(client_module, "get_engine", lambda url, **kw: _Engine(url))
    return attempts


def _login(client):
    with engine_pool.ephemeral():
        try:
            with client.connect():
                pass
        except Exception as e:
            return str(e)


def _pg(password, user="reader", database="shop"):
    from app.data_sources.clients.postgresql_client import PostgresqlClient

    return PostgresqlClient(host=f"db-{id(password)}.example.test", port=5432, database=database,
                            user=user, password=password)


@pytest.fixture(autouse=True)
def _forget_decoded_forms():
    from app.data_sources.clients import _sa_url

    _sa_url._decoded_ok.clear()
    yield
    _sa_url._decoded_ok.clear()


def test_literal_credentials_that_work_are_used_as_they_are(monkeypatch):
    from app.data_sources.clients import postgresql_client

    attempts = _login_server(monkeypatch, postgresql_client, ("reader", "p%40ss"))
    _login(_pg("p%40ss"))
    assert attempts == [("reader", "p%40ss")]


def test_percent_encoded_credentials_from_before_the_upgrade_still_connect(monkeypatch, caplog):
    from app.data_sources.clients import postgresql_client

    attempts = _login_server(monkeypatch, postgresql_client, ("svc@corp", "p@ss/w:rd"))
    client = _pg("p%40ss%2Fw%3Ard", user="svc%40corp")
    with caplog.at_level("WARNING"):
        _login(client)
        assert attempts == [("svc%40corp", "p%40ss%2Fw%3Ard"), ("svc@corp", "p@ss/w:rd")]
        # The working form is remembered: no failed login per query, one warning.
        same = type(client)(host=client.host, port=5432, database="shop", user="svc%40corp", password="p%40ss%2Fw%3Ard")
        _login(same)
        assert attempts[2:] == [("svc@corp", "p@ss/w:rd")]
    warnings = [r for r in caplog.records if "percent-encoded" in r.getMessage()]
    assert len(warnings) == 1 and "p@ss" not in warnings[0].getMessage()
    # The pool identity follows the form that connects.
    assert same.pg_uri == client.pg_uri and "svc%40corp:p%40ss%2Fw%3Ard@" in client.pg_uri


def test_a_failing_login_with_nothing_to_decode_reports_the_original_error(monkeypatch):
    from app.data_sources.clients import postgresql_client

    attempts = _login_server(monkeypatch, postgresql_client, ("reader", "right"))
    assert "password authentication failed" in _login(_pg("wrong"))
    assert attempts == [("reader", "wrong")]


def test_when_the_decoded_form_fails_too_the_original_error_is_reported(monkeypatch):
    from app.data_sources.clients import postgresql_client

    attempts = _login_server(monkeypatch, postgresql_client, ("reader", "right"))
    client = _pg("wr%6Fng")
    assert "password authentication failed" in _login(client)
    assert attempts == [("reader", "wr%6Fng"), ("reader", "wrong")]
    assert "wr%256Fng" in client.pg_uri  # nothing was remembered


def test_oracle_gets_the_same_fallback(monkeypatch):
    from app.data_sources.clients import oracledb_client
    from app.data_sources.clients.oracledb_client import OracledbClient

    attempts = _login_server(monkeypatch, oracledb_client, ("reader", "p@ss"))
    _login(OracledbClient(host="ora-fallback.example.test", port=1521, service_name="orcl", user="reader", password="p%40ss"))
    assert attempts == [("reader", "p%40ss"), ("reader", "p@ss")]


def test_oracle_service_name_may_carry_connect_data_options():
    from app.data_sources.clients.oracledb_client import OracledbClient

    args = OracledbClient(host="ora.example.test", port=2484, service_name="orcl)(SERVER=DEDICATED",
                          user="reader", password="pw", use_tcps=True)._connect_args()
    assert "(CONNECT_DATA=(SERVICE_NAME=orcl)(SERVER=DEDICATED))" in args["dsn"]
    with pytest.raises(ValueError):
        OracledbClient(host="ora.example.test", port=2484, service_name="orcl)(SECURITY=x",
                       user="reader", password="pw", use_tcps=True)._connect_args()
