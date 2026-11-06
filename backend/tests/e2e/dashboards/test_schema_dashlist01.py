"""dashlist01: what the dashboards list needs to find a dashboard (2026-11-04).

- `artifacts.content_updated_at`: when the dashboard's content last changed
  (not a data refresh). Backfilled from the newest completed, non-deleted
  version, else the live release's publish time, else the creation time;
  a value already set is kept.
- `artifact_stars`: one row per (dashboard, user) who starred it.
- `artifact_list_seen`: when each user last looked at Shared, per org.

Additive. The upgrade and the downgrade are idempotent both ways: the
dashboard corpus applies the upgrade unstamped and undoes it unstamped.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import new_id, sync_test_url

PREVIOUS = "auditcontract01"
NEW_TABLES = ("artifact_stars", "artifact_list_seen")


def _inspect(url, fn):
    engine = create_engine(url)
    try:
        return fn(inspect(engine))
    finally:
        engine.dispose()


def _tables(url) -> set:
    return set(_inspect(url, lambda i: i.get_table_names()))


def _index_names(url, table) -> set:
    return {i["name"] for i in _inspect(url, lambda i: i.get_indexes(table))}


def _artifact_columns(url) -> dict:
    return {c["name"]: c for c in _inspect(url, lambda i: i.get_columns("artifacts"))}


def _run_module(url, *calls: str) -> None:
    """Run dashlist01's upgrade()/downgrade() on their own, unstamped -- as
    the dashboard corpus harness does."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    module = script_directory().get_revision("dashlist01").module
    engine = create_engine(url)
    try:
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            for call in calls:
                getattr(module, call)()
    finally:
        engine.dispose()


def _drop_indexes(url, *indexes: tuple[str, str]) -> None:
    """Leave the schema as a run that stopped before creating these indexes
    would (a direct DDL write: no API produces a half-applied migration)."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            op = Operations(MigrationContext.configure(conn))
            for name, table in indexes:
                op.drop_index(name, table_name=table)
    finally:
        engine.dispose()


def _alembic(url, command_name: str, target: str) -> None:
    from alembic import command

    with alembic_pointed_at(url) as cfg:
        getattr(command, command_name)(cfg, target)


@pytest.mark.e2e
def test_the_tables_and_column_match_the_models():
    from app.models.artifact import Artifact
    from app.models.artifact_list_seen import ArtifactListSeen
    from app.models.artifact_star import ArtifactStar

    url = sync_test_url()
    columns = _artifact_columns(url)
    assert columns["content_updated_at"]["nullable"] is True
    assert Artifact.__table__.c.content_updated_at.nullable
    # Revision 2 of the plan had an org_reached_at column; it was withdrawn.
    assert "org_reached_at" not in columns
    assert "org_reached_at" not in Artifact.__table__.c
    assert "ix_artifacts_content_updated_at" in _index_names(url, "artifacts")
    assert {"ix_artifact_stars_artifact_id", "ix_artifact_stars_user_id"} <= _index_names(url, "artifact_stars")

    for model, not_null, unique, references in (
        (ArtifactStar, ("artifact_id", "user_id"), ("artifact_id", "user_id"),
         {(("artifact_id",), "artifacts"), (("user_id",), "users")}),
        (ArtifactListSeen, ("user_id", "organization_id", "shared_seen_at"), ("user_id", "organization_id"),
         {(("user_id",), "users"), (("organization_id",), "organizations")}),
    ):
        table = model.__tablename__
        db_columns = {c["name"]: c for c in _inspect(url, lambda i, t=table: i.get_columns(t))}
        assert set(db_columns) == {c.name for c in model.__table__.columns}, table
        for name in not_null:
            assert db_columns[name]["nullable"] is False, (table, name)
            assert model.__table__.c[name].nullable is False, (table, name)
        uniques = {tuple(u["column_names"]) for u in _inspect(url, lambda i, t=table: i.get_unique_constraints(t))}
        assert unique in uniques, table
        # A star or a watermark goes with its user and its dashboard / org.
        fks = _inspect(url, lambda i, t=table: i.get_foreign_keys(t))
        assert {(tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in fks} == references, fks
        assert len(fks) == len(references), fks
        for fk in fks:
            assert (fk["options"] or {}).get("ondelete", "").upper() == "CASCADE", (table, fk)


@pytest.mark.e2e
def test_round_trip():
    url = sync_test_url()
    head = script_directory().get_current_head()
    _alembic(url, "downgrade", PREVIOUS)
    assert current_revisions(url) == (PREVIOUS,)
    assert not set(NEW_TABLES) & _tables(url)
    assert "content_updated_at" not in _artifact_columns(url)
    _alembic(url, "upgrade", "head")
    assert current_revisions(url) == (head,)
    assert set(NEW_TABLES) <= _tables(url)
    assert "content_updated_at" in _artifact_columns(url)


@pytest.mark.e2e
def test_downgrade_is_idempotent():
    # Lesson 21: a downgrade that drops what is not there breaks every
    # database that never had it (the corpus harness undoes it unstamped).
    url = sync_test_url()
    _alembic(url, "downgrade", PREVIOUS)
    _run_module(url, "downgrade", "downgrade")
    assert current_revisions(url) == (PREVIOUS,)
    assert not set(NEW_TABLES) & _tables(url)
    assert "content_updated_at" not in _artifact_columns(url)
    _alembic(url, "upgrade", "head")
    assert set(NEW_TABLES) <= _tables(url)


@pytest.mark.e2e
def test_upgrade_completes_a_half_applied_run():
    # SQLite DDL is not transactional: a run that stopped after creating a
    # table but before its indexes must be finished by the next run, and
    # the downgrade must still work over it.
    url = sync_test_url()
    _drop_indexes(url, ("ix_artifact_stars_user_id", "artifact_stars"),
                  ("ix_artifacts_content_updated_at", "artifacts"))
    _run_module(url, "upgrade")
    assert "ix_artifact_stars_user_id" in _index_names(url, "artifact_stars")
    assert "ix_artifacts_content_updated_at" in _index_names(url, "artifacts")

    _drop_indexes(url, ("ix_artifact_stars_artifact_id", "artifact_stars"),
                  ("ix_artifacts_content_updated_at", "artifacts"))
    _run_module(url, "downgrade", "downgrade")
    assert "content_updated_at" not in _artifact_columns(url)
    assert not set(NEW_TABLES) & _tables(url)
    _run_module(url, "upgrade")
    assert set(NEW_TABLES) <= _tables(url)


# ── the backfill ────────────────────────────────────────────────────────

T0 = datetime(2025, 1, 5, 9, 0, 0)
T1 = datetime(2025, 3, 10, 12, 30, 0)
T2 = datetime(2025, 6, 20, 8, 15, 0)
T3 = datetime(2025, 8, 1, 17, 45, 0)
T4 = datetime(2025, 9, 9, 6, 0, 0)
T5 = datetime(2025, 11, 11, 11, 11, 0)


def _versions_of(rows, aid) -> list:
    tbl = rows.table("artifact_versions")
    with rows.engine.connect() as conn:
        return list(conn.execute(select(tbl).where(tbl.c.artifact_id == aid)).mappings())


def _artifact(rows, aid):
    tbl = rows.table("artifacts")
    with rows.engine.connect() as conn:
        return conn.execute(select(tbl).where(tbl.c.id == aid)).mappings().one()


def _all_versions_at(rows, aid, *, created_at, deleted_at=None) -> None:
    tbl = rows.table("artifact_versions")
    with rows.engine.begin() as conn:
        conn.execute(tbl.update().where(tbl.c.artifact_id == aid).values(
            status="completed", created_at=created_at, deleted_at=deleted_at))


def _add_version(rows, aid, *, number, created_at, status="completed", deleted_at=None) -> None:
    art = _artifact(rows, aid)
    report_id = art["report_id"] or _versions_of(rows, aid)[0]["report_id"]
    rows.insert("artifact_versions", artifact_id=aid, report_id=report_id, user_id=art["created_by"],
                organization_id=art["organization_id"], version=number, content={"code": "x"},
                status=status, created_at=created_at, updated_at=created_at, deleted_at=deleted_at)


def _with_versions(rows, *, published: bool, exclude=()) -> str:
    """A corpus dashboard with at least one version, live or not."""
    artifacts, versions = rows.table("artifacts"), rows.table("artifact_versions")
    query = (select(artifacts.c.id).where(artifacts.c.id.in_(select(versions.c.artifact_id)))
             .where(artifacts.c.published_release_id.is_not(None) if published
                    else artifacts.c.published_release_id.is_(None))
             .order_by(artifacts.c.id).limit(1))
    if exclude:
        query = query.where(artifacts.c.id.not_in(list(exclude)))
    with rows.engine.connect() as conn:
        aid = conn.execute(query).scalar()
    assert aid, f"the corpus has no {'published' if published else 'unpublished'} dashboard with versions"
    return aid


@pytest.mark.e2e
def test_backfill_takes_the_newest_completed_version_else_the_release_else_created(corpus, corpus_rows):
    url = sync_test_url()
    _alembic(url, "upgrade", "head")  # the corpus stops before the data migration
    _alembic(url, "downgrade", PREVIOUS)
    rows = corpus_rows
    rows.forget_schema()

    # Direct row writes: these are states of data written before this
    # migration, which today's API cannot produce at this revision.
    soft_deleted_newest = _with_versions(rows, published=False)
    _all_versions_at(rows, soft_deleted_newest, created_at=T1)
    _add_version(rows, soft_deleted_newest, number=1000, created_at=T2, deleted_at=T2)

    failed_newest = _with_versions(rows, published=False, exclude=[soft_deleted_newest])
    _all_versions_at(rows, failed_newest, created_at=T0)
    _add_version(rows, failed_newest, number=1000, created_at=T3, status="failed")

    all_deleted = _with_versions(rows, published=True, exclude=[soft_deleted_newest, failed_newest])
    _all_versions_at(rows, all_deleted, created_at=T1, deleted_at=T2)
    rows.update("artifact_releases", _artifact(rows, all_deleted)["published_release_id"], published_at=T4)

    # A live version beats the release, whether the release was published
    # before it or after it (a republish without an edit).
    used = [soft_deleted_newest, failed_newest, all_deleted]
    published_before = _with_versions(rows, published=True, exclude=used)
    _all_versions_at(rows, published_before, created_at=T3)
    rows.update("artifact_releases", _artifact(rows, published_before)["published_release_id"], published_at=T1)
    used.append(published_before)
    published_after = _with_versions(rows, published=True, exclude=used)
    _all_versions_at(rows, published_after, created_at=T3)
    rows.update("artifact_releases", _artifact(rows, published_after)["published_release_id"], published_at=T4)

    some = _artifact(rows, soft_deleted_newest)
    no_versions = rows.insert("artifacts", id=new_id(), organization_id=some["organization_id"],
                              created_by=some["created_by"], mode="page", title="no versions",
                              created_at=T5, updated_at=T5)
    # created_at is nullable (BaseSchema); a row without it still gets a time.
    no_time_at_all = rows.insert("artifacts", id=new_id(), organization_id=some["organization_id"],
                                 created_by=some["created_by"], mode="page", title="no created_at",
                                 created_at=None, updated_at=None)

    before = datetime.utcnow()
    _alembic(url, "upgrade", "head")
    after = datetime.utcnow()
    rows.forget_schema()
    assert _artifact(rows, soft_deleted_newest)["content_updated_at"] == T1
    assert _artifact(rows, failed_newest)["content_updated_at"] == T0
    assert _artifact(rows, all_deleted)["content_updated_at"] == T4
    assert _artifact(rows, published_before)["content_updated_at"] == T3
    assert _artifact(rows, published_after)["content_updated_at"] == T3
    assert _artifact(rows, no_versions)["content_updated_at"] == T5
    # The upgrade's own time, in naive UTC like every other timestamp (a
    # minute of slack for clock granularity; a timezone slip is hours).
    filled = _artifact(rows, no_time_at_all)["content_updated_at"]
    assert filled is not None
    assert before - timedelta(minutes=1) <= filled <= after + timedelta(minutes=1), (before, filled, after)
    # Every dashboard has an edited time after the upgrade.
    artifacts = rows.table("artifacts")
    with rows.engine.connect() as conn:
        missing = conn.execute(select(artifacts.c.id).where(artifacts.c.content_updated_at.is_(None))).all()
    assert missing == []


@pytest.mark.e2e
def test_backfill_leaves_a_set_value_alone(corpus, corpus_rows):
    url = sync_test_url()
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", PREVIOUS)
    rows = corpus_rows
    # Applied unstamped first (as the corpus harness does), then `upgrade
    # head` runs the same upgrade over a column that already exists.
    _run_module(url, "upgrade")
    rows.forget_schema()
    kept = _with_versions(rows, published=False)
    _all_versions_at(rows, kept, created_at=T1)
    rows.update("artifacts", kept, content_updated_at=T5)
    refilled = _with_versions(rows, published=False, exclude=[kept])
    _all_versions_at(rows, refilled, created_at=T2)
    rows.update("artifacts", refilled, content_updated_at=None)

    _alembic(url, "upgrade", "head")
    assert current_revisions(url) == (script_directory().get_current_head(),)
    assert _artifact(rows, kept)["content_updated_at"] == T5
    # The backfill still runs when the column already exists.
    assert _artifact(rows, refilled)["content_updated_at"] == T2
