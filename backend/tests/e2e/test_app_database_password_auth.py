"""The application database is reached with password authentication.

The suite's own Postgres server uses trust auth (see tests/conftest.py), so
nothing else in the suite proves that the production connection code still
authenticates. These tests start a second, small server with the image's
default password auth (SCRAM-SHA-256) and connect to it the way a deployment
does: the URL comes from the `database` section of the BOW config, resolved
by the production builders (not the TESTING branches), for the sync psycopg2
engine, the async asyncpg engine and the alembic migration runner.

Postgres jobs only: on SQLite there is no server to authenticate against.
"""
import asyncio
import contextlib
import os

import pytest
from asyncpg.exceptions import InvalidPasswordError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.settings import database as app_database
from app.settings.bow_config import Database
from app.settings.config import settings

PASSWORD = "app-db-password-1"


@pytest.fixture(scope="module")
def password_server(db_backend):
    """A Postgres server that requires a password, shared by this module.

    TEST_PASSWORD_DATABASE_URL points at one provisioned outside the suite (a
    scratch database whose server checks passwords), the counterpart of a
    pre-set TEST_DATABASE_URL. Otherwise a container is started. A run with
    no Docker at all (a pre-set TEST_DATABASE_URL needs none) skips; on CI a
    container that fails to start where Docker is present is an error, so a
    broken setup can't pass as skipped.
    """
    if db_backend != "postgres":
        pytest.skip("password auth is a Postgres server property")
    provided = os.environ.get("TEST_PASSWORD_DATABASE_URL")
    if provided:
        yield make_url(provided).set(drivername="postgresql")
        return
    try:
        from testcontainers.postgres import PostgresContainer

        container = (
            PostgresContainer("postgres:15", username="bow", password=PASSWORD, dbname="bow")
            .with_bind_ports(5432, ("127.0.0.1", None))
        )
        container.start()
    except Exception as exc:  # no Docker daemon on this machine
        docker_present = bool(os.environ.get("DOCKER_HOST")) or os.path.exists("/var/run/docker.sock")
        if os.environ.get("CI") and docker_present:
            raise
        pytest.skip(f"cannot start a Postgres container (set TEST_PASSWORD_DATABASE_URL): {exc}")
    try:
        url = make_url(container.get_connection_url()).set(drivername="postgresql")
        yield url
    finally:
        container.stop()


@contextlib.contextmanager
def _production_config(monkeypatch, url):
    """Point the BOW config's database at `url` and leave the TESTING branches
    for the duration of the block (the builders read both when called)."""
    monkeypatch.setattr(settings.bow_config, "database", Database(url=url.render_as_string(hide_password=False)))
    monkeypatch.setattr(settings, "TESTING", False)
    try:
        yield
    finally:
        monkeypatch.setattr(settings, "TESTING", True)


def _sync_select_user(monkeypatch, url):
    with _production_config(monkeypatch, url):
        engine = app_database.create_database_engine()
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT current_user")).scalar()
    finally:
        engine.dispose()


def _production_async_engine(monkeypatch):
    """The app's own async engine (create_async_database_engine, the one
    app.dependencies uses), built fresh for this config. The process-wide
    singleton is cleared for the call and monkeypatch restores it after."""
    monkeypatch.setattr(app_database, "_async_engine_singleton", None)
    return app_database.create_async_database_engine()


def _async_select_user(monkeypatch, url):
    with _production_config(monkeypatch, url):
        engine = _production_async_engine(monkeypatch)

    async def _run():
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text("SELECT current_user"))).scalar()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@pytest.mark.parametrize("connect", [_sync_select_user, _async_select_user], ids=["sync", "async"])
def test_production_engine_connects_with_the_configured_password(monkeypatch, password_server, connect):
    assert connect(monkeypatch, password_server) == password_server.username


@pytest.mark.parametrize("connect", [_sync_select_user, _async_select_user], ids=["sync", "async"])
def test_production_engine_is_refused_a_wrong_password(monkeypatch, password_server, connect):
    """Also what keeps the tests above honest: on a server that let anyone in,
    this one fails."""
    wrong = password_server.set(password=PASSWORD + "-wrong")
    # psycopg2's refusal comes wrapped by SQLAlchemy; asyncpg's connect error
    # comes through as raised.
    with pytest.raises((DBAPIError, InvalidPasswordError)) as refused:
        connect(monkeypatch, wrong)
    assert _refused_for_password(refused.value)


def _refused_for_password(exc):
    """SQLSTATE 28P01 (invalid_password): refused by the server's password
    check, not by some other connection failure."""
    orig = getattr(exc, "orig", exc)
    if getattr(orig, "sqlstate", None) == "28P01":
        return True
    # psycopg2 reports connection-time failures without a SQLSTATE. The text
    # is the server's own (Postgres' auth.c), not ours.
    return "password authentication failed" in str(orig)


def test_migrations_run_through_the_production_config(monkeypatch, password_server):
    """The migration runner (alembic/env.py) resolves the URL from the BOW
    config outside TESTING, connects with the password and migrates an empty
    database to head, after which the production async engine reads it."""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    with _production_config(monkeypatch, password_server):
        command.upgrade(cfg, "head")
        engine = _production_async_engine(monkeypatch)
    head = ScriptDirectory.from_config(cfg).get_current_head()

    async def _read():
        try:
            async with engine.connect() as conn:
                version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
                orgs = (await conn.execute(text("SELECT count(*) FROM organizations"))).scalar()
                return version, orgs
        finally:
            await engine.dispose()

    version, orgs = asyncio.run(_read())
    assert version == head
    assert orgs == 0
