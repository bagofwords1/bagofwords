"""artver01 — the artifacts → artifacts + artifact_versions normalization.

Two layers of protection for existing customer data:

- `_backfill_parents` in isolation: every pre-migration version row gets a
  parent of its own (decision D4 — no lineage guessing), every identity field
  is copied (deleted_at included, so soft-deleted versions don't resurface as
  live artifacts), and version numbers are never touched.
- The full upgrade → downgrade round trip through the real alembic chain:
  a downgrade must hand back the exact pre-migration shape and values,
  because that is the production rollback path.

Run:
    cd backend
    TESTING=true uv run pytest tests/unit/test_artver01_backfill.py --db=sqlite
"""
import importlib.util
import os
import uuid
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "artver01_normalize_artifact_versions.py"
    )
    spec = importlib.util.spec_from_file_location("artver01_backfill", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _mid_migration_schema(metadata: sa.MetaData) -> tuple[sa.Table, sa.Table]:
    """The schema exactly as it stands when _backfill_parents runs: the old
    table already renamed to artifact_versions with a nullable artifact_id,
    the parent table created and empty."""
    versions = sa.Table(
        "artifact_versions", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("report_id", sa.String(36)),
        sa.Column("user_id", sa.String(36)),
        sa.Column("organization_id", sa.String(36)),
        sa.Column("title", sa.String(255)),
        sa.Column("mode", sa.String(20)),
        sa.Column("version", sa.Integer),
        sa.Column("content", sa.JSON),
        sa.Column("status", sa.String(20)),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
        sa.Column("deleted_at", sa.DateTime),
    )
    parents = sa.Table(
        "artifacts", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("report_id", sa.String(36)),
        sa.Column("organization_id", sa.String(36)),
        sa.Column("created_by", sa.String(36)),
        sa.Column("mode", sa.String(20)),
        sa.Column("title", sa.String(255)),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
        sa.Column("deleted_at", sa.DateTime),
    )
    return versions, parents


def test_backfill_gives_every_version_row_its_own_parent(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/backfill.db")
    metadata = sa.MetaData()
    versions, parents = _mid_migration_schema(metadata)
    metadata.create_all(engine)

    report_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    made = datetime(2025, 3, 1, 12, 0, 0)
    gone = datetime(2025, 6, 1, 9, 30, 0)
    rows = [
        # A two-version dashboard chain (pre-migration: two flat rows) with a
        # Hebrew title — the copy must be byte-faithful, not ASCII-mangled.
        {"id": "v1", "title": "סקירת מכירות", "mode": "page", "version": 1,
         "created_at": made, "deleted_at": None},
        {"id": "v2", "title": "סקירת מכירות", "mode": "page", "version": 2,
         "created_at": made, "deleted_at": None},
        # A soft-deleted doc: it still gets a parent (its version number must
        # stay reserved), and the parent inherits deleted_at so it never shows
        # up in live parent-table scans.
        {"id": "v3", "title": "Old doc", "mode": "doc", "version": 5,
         "created_at": made, "deleted_at": gone},
    ]
    with engine.begin() as conn:
        conn.execute(sa.insert(versions), [
            {**r, "report_id": report_id, "organization_id": org_id,
             "user_id": user_id, "content": {}, "status": "completed",
             "updated_at": made, "artifact_id": None}
            for r in rows
        ])

    migration = _load_migration()
    with engine.begin() as conn:
        made_count = migration._backfill_parents(conn)
    assert made_count == 3

    with engine.connect() as conn:
        version_rows = conn.execute(
            sa.select(versions).order_by(versions.c.id)
        ).mappings().all()
        parent_rows = {
            p["id"]: p for p in conn.execute(sa.select(parents)).mappings().all()
        }

    # One parent per version row — D4, no lineage guessing: even the two
    # same-title dashboard versions get separate parents.
    assert len(parent_rows) == 3
    assert len({v["artifact_id"] for v in version_rows}) == 3

    for v, seed in zip(version_rows, rows):
        parent = parent_rows[v["artifact_id"]]
        assert parent["title"] == seed["title"]
        assert parent["mode"] == seed["mode"]
        assert parent["report_id"] == report_id
        assert parent["organization_id"] == org_id
        assert parent["created_by"] == user_id
        assert parent["created_at"] == seed["created_at"]
        assert parent["deleted_at"] == seed["deleted_at"]
        # Version numbers are history — the backfill must not renumber.
        assert v["version"] == seed["version"]


def test_artver01_upgrade_downgrade_round_trip(tmp_path):
    """officejs01 → seed old-shape rows → artver01 → downgrade officejs01.

    The downgrade is the production rollback path; it must restore the exact
    original shape and values, not merely something that parses.
    """
    from alembic import command
    from alembic.config import Config
    from app.settings.config import settings

    db_url = f"sqlite:///{tmp_path}/roundtrip.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)

    # alembic/env.py resolves the URL from settings.TEST_DATABASE_URL under
    # TESTING (same dance as the conftest template build) — point it at our
    # scratch file and restore afterwards.
    saved_setting = settings.TEST_DATABASE_URL
    saved_env = os.environ.get("TEST_DATABASE_URL")
    settings.TEST_DATABASE_URL = db_url
    os.environ["TEST_DATABASE_URL"] = db_url
    try:
        command.upgrade(cfg, "officejs01")

        engine = sa.create_engine(db_url)
        seed = [
            ("a1", "לוח מחוונים", "page", 1, None),
            ("a2", "לוח מחוונים", "page", 2, None),
            ("a3", "Quarterly doc", "doc", 1, "2025-06-01 09:30:00"),
        ]
        with engine.begin() as conn:
            for vid, title, mode, version, deleted in seed:
                conn.execute(sa.text(
                    "INSERT INTO artifacts (id, report_id, user_id, organization_id,"
                    " title, mode, version, content, status, created_at, updated_at, deleted_at)"
                    " VALUES (:id, 'r1', 'u1', 'o1', :title, :mode, :version, '{}',"
                    " 'completed', '2025-03-01 12:00:00', '2025-03-01 12:00:00', :deleted)"
                ), {"id": vid, "title": title, "mode": mode,
                    "version": version, "deleted": deleted})

        command.upgrade(cfg, "artver01")
        with engine.connect() as conn:
            n_parents = conn.execute(sa.text("SELECT count(*) FROM artifacts")).scalar()
            n_null = conn.execute(sa.text(
                "SELECT count(*) FROM artifact_versions WHERE artifact_id IS NULL"
            )).scalar()
            version_cols = {
                r[1] for r in conn.execute(sa.text("PRAGMA table_info(artifact_versions)"))
            }
        assert n_parents == 3
        assert n_null == 0
        assert "title" not in version_cols and "mode" not in version_cols

        command.downgrade(cfg, "officejs01")
        with engine.connect() as conn:
            back = conn.execute(sa.text(
                "SELECT id, title, mode, version, deleted_at FROM artifacts ORDER BY id"
            )).fetchall()
            tables = {
                r[0] for r in conn.execute(sa.text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'artifact%'"
                ))
            }
        assert tables == {"artifacts"}
        assert [(r[0], r[1], r[2], r[3]) for r in back] == [
            (vid, title, mode, version) for vid, title, mode, version, _ in seed
        ]
        # The soft-deleted doc came back soft-deleted.
        assert back[2][4] is not None and back[0][4] is None
        engine.dispose()
    finally:
        settings.TEST_DATABASE_URL = saved_setting
        if saved_env is not None:
            os.environ["TEST_DATABASE_URL"] = saved_env
        else:
            os.environ.pop("TEST_DATABASE_URL", None)
