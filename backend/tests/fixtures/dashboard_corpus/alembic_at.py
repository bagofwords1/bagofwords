"""Alembic helpers for the dashboard corpus.

`MAIN_BASE_REVISION` is the head of main when this branch was cut. The corpus
is written at that revision -- the schema customers have today -- and then
upgraded through this branch's migrations, so every corpus test also runs
our downgrade and upgrade. Until this branch adds a migration the base IS
the head and both steps are no-ops.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from alembic import command
from tests.fixtures.dashboard_corpus.rows import sync_test_url

MAIN_BASE_REVISION = "submsg02"

# The last schema-only revision of this branch: the corpus stops here unless
# a test asks for the data migration (phase 9), so the tests written for
# phases 1-8 keep the data as customers have it before the upgrade.
PRE_MIGRATION_REVISION = "dashfc07"

# The last revision of release 0.0.560 (commit 052d73926). Customers skip
# releases; the oldest-upgrade test starts here.
OLD_RELEASE_REVISION = "codeperm01"


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def current_revisions(url: str) -> tuple[str, ...]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return tuple(MigrationContext.configure(conn).get_current_heads())
    finally:
        engine.dispose()


@contextmanager
def alembic_pointed_at(url: str) -> Iterator[Config]:
    """A Config whose migrations run against `url`.

    alembic/env.py resolves the URL from settings.TEST_DATABASE_URL under
    TESTING and ignores the config's own url, so point that setting (and the
    env var) at `url` for the duration and restore it afterwards -- the same
    dance as the conftest template build.
    """
    from app.settings.config import settings

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    saved_setting = settings.TEST_DATABASE_URL
    saved_env = os.environ.get("TEST_DATABASE_URL")
    settings.TEST_DATABASE_URL = url
    os.environ["TEST_DATABASE_URL"] = url
    try:
        yield cfg
    finally:
        settings.TEST_DATABASE_URL = saved_setting
        if saved_env is not None:
            os.environ["TEST_DATABASE_URL"] = saved_env
        else:
            os.environ.pop("TEST_DATABASE_URL", None)


@contextmanager
def scratch_database(tmp_path) -> Iterator[str]:
    """An empty database next to the test database, on the same backend:
    a SQLite file, or a fresh database on the test Postgres server."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    test_url = sync_test_url()
    if test_url.startswith("sqlite"):
        yield f"sqlite:///{tmp_path}/scratch.db"
        return
    url = make_url(test_url)
    name = f"{url.database}_scratch"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def downgrade_test_db_to_base() -> None:
    """Take the test database back to main's schema (no-op while base == head)."""
    url = sync_test_url()
    if current_revisions(url) == (MAIN_BASE_REVISION,):
        return
    remove_schema_only_revisions(url)
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, MAIN_BASE_REVISION)
    assert current_revisions(url) == (MAIN_BASE_REVISION,)


# Schema-only revisions that come after the data migration (dashfc08). The
# code at head reads their tables and columns, so a corpus stopped before the
# data migration gets them too: their upgrade() runs on its own, unstamped,
# and is idempotent, so a later `upgrade head` finds it done.
SCHEMA_ONLY_AFTER_DATA_MIGRATION = ("collab01", "refhist01")


def apply_schema_only_revisions(url: str) -> None:
    from alembic.operations import Operations

    script = script_directory()
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                for revision in SCHEMA_ONLY_AFTER_DATA_MIGRATION:
                    script.get_revision(revision).module.upgrade()
    finally:
        engine.dispose()


def remove_schema_only_revisions(url: str) -> None:
    """Undo apply_schema_only_revisions before a downgrade below the data
    migration (their downgrade is idempotent too)."""
    from alembic.operations import Operations

    script = script_directory()
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            if set(context.get_current_heads()) & set(SCHEMA_ONLY_AFTER_DATA_MIGRATION):
                return  # stamped: alembic's own downgrade undoes them
            with Operations.context(context):
                for revision in reversed(SCHEMA_ONLY_AFTER_DATA_MIGRATION):
                    script.get_revision(revision).module.downgrade()
    finally:
        engine.dispose()


def upgrade_test_db_to_head(target: str = "head") -> None:
    url = sync_test_url()
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, target)
    if target != "head" and target not in SCHEMA_ONLY_AFTER_DATA_MIGRATION:
        apply_schema_only_revisions(url)
