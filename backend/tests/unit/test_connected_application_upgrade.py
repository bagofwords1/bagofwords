"""Upgrade the shipped schema, not a downgrade of the implementation under test.

The frozen hostapp01 fixture is byte-for-byte from v0.0.601 (95da1b81d).
Legacy rows require direct inserts: today's API cannot write that schema.
"""
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic import command
from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, scratch_database

pytestmark = pytest.mark.db
RELEASE_HEAD = "mrgheads11"
BACKEND = Path(__file__).parents[2]


@contextmanager
def enforce_sqlite_foreign_keys():
    def enable(connection, _record):
        if isinstance(connection, sqlite3.Connection):
            connection.execute("PRAGMA foreign_keys=ON")
    sa.event.listen(sa.engine.Engine, "connect", enable)
    try:
        yield
    finally:
        sa.event.remove(sa.engine.Engine, "connect", enable)


def rows(conn, name):
    table = sa.Table(name, sa.MetaData(), autoload_with=conn)
    return {r["id"]: dict(r) for r in conn.execute(sa.select(table)).mappings()}


def seed_release(conn):
    def insert(table_name, **values):
        table = sa.Table(table_name, sa.MetaData(), autoload_with=conn)
        values.setdefault("id", str(uuid.uuid4()))
        conn.execute(table.insert().values(**values))
        return values["id"]

    user = insert("users", name="Migration viewer", email=f"{uuid.uuid4()}@example.com",
                  hashed_password="not-a-login", is_active=True, is_superuser=False, is_verified=True)
    org = insert("organizations", name="Existing organization")
    artifact = insert("artifacts", organization_id=org, created_by=user, mode="page", title="Shared assistant")
    now = datetime(2026, 10, 30, 12, 0)
    timestamps = {"created_at": now, "updated_at": now, "deleted_at": None}
    for deleted in (None, now):
        insert("oauth_mcp_clients", organization_id=org, name="Customer portal",
               client_id=f"client_{uuid.uuid4().hex}", client_secret_hash=uuid.uuid4().hex * 2,
               redirect_uris='["https://portal.example.com/callback"]', scopes="app mcp",
               trusted=True, entra_exchange={"provider": "entra", "external_client_id": "existing-client"},
               **{**timestamps, "deleted_at": deleted})
    for mode, enabled, deleted in (("assertion", True, None), ("sso", False, None), ("idp_token", True, now)):
        host = insert("host_applications", organization_id=org, name="Customer portal",
                      public_id=f"host_{uuid.uuid4().hex[:20]}", enabled=enabled,
                      allowed_origins=["https://portal.example.com"], identity_mode=mode,
                      identity_source="member_attribute:employee_id", create_users=False,
                      issuer="existing-erp", public_key="existing-public-key", key_fingerprint="key-fingerprint",
                      audience="existing-audience", epoch=7, revision=4, created_by_user_id=user,
                      **{**timestamps, "deleted_at": deleted})
        insert("host_assertion_replays", host_application_id=host, jti=uuid.uuid4().hex,
               expires_at=now + timedelta(minutes=1), **timestamps)
        insert("artifact_host_applications", artifact_id=artifact, host_application_id=host,
               created_by_user_id=user, **timestamps)
        insert("oauth_mcp_access_tokens", client_id="embed:" + host, user_id=user, organization_id=org,
               token_hash=uuid.uuid4().hex * 2, scope="embed", expires_at=now + timedelta(hours=1),
               host_application_id=host, exchange_context={"epoch": 7}, **timestamps)
    client = next(iter(rows(conn, "oauth_mcp_clients").values()))
    insert("oauth_mcp_access_tokens", client_id=client["client_id"], user_id=user, organization_id=org,
           token_hash=uuid.uuid4().hex * 2, scope="app mcp", expires_at=now + timedelta(hours=1),
           refresh_token_hash=uuid.uuid4().hex * 2, refresh_expires_at=now + timedelta(days=30), **timestamps)
    insert("oauth_mcp_authorization_codes", client_id=client["client_id"], user_id=user, organization_id=org,
           code=uuid.uuid4().hex, redirect_uri="https://portal.example.com/callback", scope="app",
           code_challenge="existing-pkce-challenge", expires_at=now + timedelta(minutes=5), **timestamps)


def test_released_database_upgrades_without_losing_application_identity(tmp_path):
    assert (BACKEND / "alembic/versions/hostapp01_host_applications.py").read_bytes() == (
        BACKEND / "tests/fixtures/released_migrations/v0_0_601_hostapp01.py").read_bytes()
    released = tmp_path / "released-alembic"
    shutil.copytree(BACKEND / "alembic", released, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(BACKEND / "tests/fixtures/released_migrations/v0_0_601_hostapp01.py",
                    released / "versions/hostapp01_host_applications.py")
    with scratch_database(tmp_path) as url:
        with alembic_pointed_at(url) as cfg:
            cfg.set_main_option("script_location", str(released))
            command.upgrade(cfg, RELEASE_HEAD)
        engine = sa.create_engine(url)
        try:
            with engine.begin() as conn:
                seed_release(conn)
                before = {name: rows(conn, name) for name in (
                    "oauth_mcp_clients", "host_applications", "host_assertion_replays", "artifact_host_applications",
                    "oauth_mcp_access_tokens", "oauth_mcp_authorization_codes")}
            with enforce_sqlite_foreign_keys(), alembic_pointed_at(url) as cfg:
                command.upgrade(cfg, "head")
            with engine.connect() as conn:
                assert sa.inspect(conn).has_table("connected_applications"), (
                    "A database already stamped with the released hostapp01 needs a NEW revision")
                parents = rows(conn, "connected_applications")
                for old, new in (("oauth_mcp_clients", "application_oauth_config"),
                                 ("host_applications", "application_embedding_config")):
                    configs = rows(conn, new)
                    assert set(configs) == set(before[old])
                    for id_, legacy in before[old].items():
                        merged = {**parents[id_], **configs[id_]}
                        assert {key: merged[key] for key in legacy} == legacy
                        assert parents[id_]["agent_ids"] is None  # preserve pre-existing access
                assert len(parents) == sum(len(before[n]) for n in ("oauth_mcp_clients", "host_applications"))
                for name in ("host_assertion_replays", "artifact_host_applications", "oauth_mcp_access_tokens", "oauth_mcp_authorization_codes"):
                    assert rows(conn, name) == before[name]
                for name in ("host_assertion_replays", "oauth_mcp_access_tokens", "artifact_host_applications"):
                    fks = sa.inspect(conn).get_foreign_keys(name)
                    assert any(fk["referred_table"] == "application_embedding_config" for fk in fks)
                assert not sa.inspect(conn).has_table("host_applications")
                assert not sa.inspect(conn).has_table("oauth_mcp_clients")
                if conn.dialect.name == "sqlite":
                    assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            # A rollback immediately after migration preserves the released
            # rows as well. Once new policies are used, rollback fails closed.
            with enforce_sqlite_foreign_keys(), alembic_pointed_at(url) as cfg:
                command.downgrade(cfg, "payload01")
            with engine.connect() as conn:
                for name, expected in before.items():
                    assert rows(conn, name) == expected
            with enforce_sqlite_foreign_keys(), alembic_pointed_at(url) as cfg:
                command.upgrade(cfg, "head")
            # Rollback must not erase a newly configured security boundary.
            with engine.begin() as conn:
                conn.execute(sa.text("UPDATE connected_applications SET agent_ids=:policy"), {"policy": "[]"})
            with pytest.raises(RuntimeError, match="Cannot safely downgrade"), alembic_pointed_at(url) as cfg:
                command.downgrade(cfg, "payload01")
            with engine.connect() as conn:
                assert sa.inspect(conn).has_table("application_embedding_config")
                assert rows(conn, "oauth_mcp_access_tokens") == before["oauth_mcp_access_tokens"]
        finally:
            engine.dispose()
