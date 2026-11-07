"""Build the corpus once per worker and hand every test its own copy.

A full build costs seconds of API calls, seeding and migrations, and nearly
all of the dashboards tests read the same starting data. So the first test of
a worker that asks for `corpus` (or `migrated_corpus`) really builds it, which
keeps the downgrade, the seed and the upgrade under test on every run; the
database is then saved, and each test -- that first one included -- starts
from a restored copy.

What a copy has to bring back, besides the database:

- The `Corpus` and `Cast` objects. Each test gets its own, unpickled, so a
  test that edits them cannot reach the next one.
- The seeder's clock and row names. `Rows` keeps using them after the build:
  rows a test adds through `corpus_rows` get fixed timestamps and stable names.
- Thumbnails and release files. They live on disk, under ids the copy shares
  with the saved database, and every test's teardown removes them.
- Nothing cached in this process. Ids now repeat from test to test, so a cache
  keyed by organization would serve one test's answer to the next.

Saved per worker because file names on disk derive from the ids of that
worker's build, and uploads/ is shared between workers.
"""
from __future__ import annotations

import contextlib
import os
import pickle
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import inspect, text

from tests.fixtures.dashboard_corpus.build import THUMBNAILS_DIR
from tests.fixtures.dashboard_corpus.kinds import Corpus, Seeder
from tests.fixtures.dashboard_corpus.rows import Rows


def _release_files(release_id: str) -> list[Path]:
    uploads = Path(THUMBNAILS_DIR).parent
    single = [uploads / "thumbnails" / f"release-{release_id}.png",
              uploads / "pptx" / f"release-{release_id}.pptx"]
    previews = uploads / "pptx_previews" / f"release-{release_id}"
    return single + (sorted(previews.rglob("*")) if previews.is_dir() else [])


def _files_on_disk(rows: Rows, corpus: Corpus) -> dict[str, bytes]:
    paths = [Path(p) for p in corpus.thumbnail_files]
    with rows.engine.connect() as conn:
        if "artifact_releases" in inspect(conn).get_table_names():
            for (release_id,) in conn.execute(text("SELECT id FROM artifact_releases")).fetchall():
                paths += _release_files(str(release_id))
    return {str(p): p.read_bytes() for p in paths if p.is_file()}


def _forget_cached_rows() -> None:
    """Process-wide state keyed by ids a restored database brings back. A run
    of the dashboards folder that logged every module-level container in app/
    showed these filling up; the rest it showed hold no row ids."""
    from app.ai.context.context_hub import invalidate_instructions_cache, invalidate_schema_cache
    from app.ai.llm.pii.loader import invalidate as invalidate_redactors
    from app.ai.tools.mcp_tool_registry import clear_usage_cache
    from app.routes import dashboard_collab
    from app.services import artifact_activity, mcp_cooldown, obo_recovery_service

    invalidate_schema_cache()
    invalidate_instructions_cache()
    invalidate_redactors()
    clear_usage_cache()
    dashboard_collab._bring_locks.clear()
    artifact_activity._WITHHELD_SESSIONS.clear()
    mcp_cooldown._deadlines.clear()
    obo_recovery_service._attempts.clear()


class _SqliteCopy:
    def __init__(self, alembic_config, name: str):
        from tests.conftest import _sqlite_db_file

        self.db_file = _sqlite_db_file(alembic_config)
        self.saved = f"{self.db_file}.{name}"

    def save(self) -> None:
        # The backup API, so a write-ahead log still open is included.
        source, target = sqlite3.connect(self.db_file), sqlite3.connect(self.saved)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()

    def restore(self) -> None:
        from tests.conftest import _remove_sqlite_files

        _remove_sqlite_files(self.db_file)
        shutil.copyfile(self.saved, self.db_file)

    def close(self) -> None:
        from tests.conftest import _remove_sqlite_files

        _remove_sqlite_files(self.saved)


class _PostgresCopy:
    """A database next to the test database, and the same in-place reset the
    session template uses: every row out, the saved rows back with COPY, and
    a re-clone (with its DROP DATABASE checkpoint) only when the schema
    differs, which it does after a test of another state or one that migrated."""

    def __init__(self, alembic_config, db_template, name: str):
        self.url = alembic_config.get_main_option("sqlalchemy.url")
        self.db_template = db_template
        self.saved = f"{db_template.db}_{name}"
        self.reset = None

    def save(self) -> None:
        from tests.conftest import _pg_admin_engine, _PostgresInPlaceReset

        self.db_template.live.dispose()
        admin, url = _pg_admin_engine(self.url)
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{self.saved}" WITH (FORCE)'))
                conn.execute(text(f'ALTER DATABASE "{url.database}" WITH ALLOW_CONNECTIONS false'))
                conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                  "WHERE datname = :db"), {"db": url.database})
                try:
                    conn.execute(text(f'CREATE DATABASE "{self.saved}" TEMPLATE "{url.database}"'))
                finally:
                    conn.execute(text(f'ALTER DATABASE "{url.database}" WITH ALLOW_CONNECTIONS true'))
        finally:
            admin.dispose()
        self.reset = _PostgresInPlaceReset(self.url, self.saved)

    def restore(self) -> None:
        self.reset.reset()

    def close(self) -> None:
        from tests.conftest import _pg_admin_engine

        if self.reset is None:
            return
        self.reset.close()
        admin, _ = _pg_admin_engine(self.url)
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{self.saved}" WITH (FORCE)'))
        finally:
            admin.dispose()


class _Saved:
    def __init__(self, copy, seeder: bytes, files: dict[str, bytes]):
        self.copy, self.seeder, self.files = copy, seeder, files


class CorpusStore:
    """One saved corpus per fixture, for this worker's whole session."""

    def __init__(self, backend: str, alembic_config, db_template):
        self.backend = backend
        self.alembic_config = alembic_config
        self.db_template = db_template
        self._saved: dict[bool, _Saved] = {}

    def get(self, rows: Rows, *, migrate: bool, build: Callable[[], Corpus]) -> Corpus:
        if self.backend not in ("sqlite", "postgres"):  # `external`: a server we may not own
            return build()
        saved = self._saved.get(migrate)
        if saved is None:
            saved = self._saved[migrate] = self._build_and_save(rows, migrate, build)
        return self._restore(saved, rows)

    def _build_and_save(self, rows: Rows, migrate: bool, build: Callable[[], Corpus]) -> _Saved:
        from tests.conftest import _dispose_async_engine

        if self.backend == "postgres":
            # A corpus test skips the autouse reset (conftest), and this one
            # has to start from the empty template like any build.
            self.db_template.reset()
        name = "corpus_migrated" if migrate else "corpus"
        corpus = build()
        seeder = rows.clock.__self__
        files = _files_on_disk(rows, corpus)
        rows.close()
        _dispose_async_engine()
        if self.backend == "sqlite":
            copy = _SqliteCopy(self.alembic_config, name)
        else:
            copy = _PostgresCopy(self.alembic_config, self.db_template, name)
        copy.save()
        return _Saved(copy, pickle.dumps(seeder), files)

    def _restore(self, saved: _Saved, rows: Rows) -> Corpus:
        from tests.conftest import _dispose_async_engine

        _dispose_async_engine()
        _forget_cached_rows()
        saved.copy.restore()
        rows.forget_schema()
        for path, content in saved.files.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_bytes(content)
        seeder: Seeder = pickle.loads(saved.seeder)
        seeder.attach(rows)
        return seeder.corpus

    def close(self) -> None:
        for saved in self._saved.values():
            with contextlib.suppress(Exception):
                saved.copy.close()
        self._saved.clear()
