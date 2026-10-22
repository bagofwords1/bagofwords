"""Phase 1: the dashboards schema exists exactly as specified, and nothing reads it yet.

The expected shape below is the approved phase-1 plan, column by column. It is
checked against the migrated database on SQLite and on Postgres, so a
migration that drifts from the plan -- or from the ORM models -- fails here.
"""
import copy

import pytest
from sqlalchemy import Boolean, DateTime, String, Text, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import JSON

from tests.fixtures.dashboard_corpus.alembic_at import (
    MAIN_BASE_REVISION,
    PRE_MIGRATION_REVISION,
    alembic_pointed_at,
    current_revisions,
    scratch_database,
    script_directory,
)
from tests.fixtures.dashboard_corpus.rows import Rows, sync_test_url

# (type, length, nullable, server_default) -- server_default None means "none set";
# booleans are compared as True/False whatever the dialect renders.
S36 = (String, 36)
NEW_ARTIFACT_COLUMNS = {
    "owner_id": (S36, True, None),
    "project_id": (S36, True, None),
    "visibility": ((String, 20), False, "none"),
    "slug": ((String, 255), True, None),
    "shared_run_identity": ((String, 20), False, "viewer"),
    "include_data_tab": ((Boolean, None), False, True),
    "chat_enabled": ((Boolean, None), False, False),
    "chat_data_source_ids": ((JSON, None), True, None),
    "chat_model_id": (S36, True, None),
    "cron_schedule": ((String, None), True, None),
    "refresh_on_view": ((Boolean, None), False, True),
    "last_run_at": ((DateTime, None), True, None),
    "notification_subscribers": ((JSON, None), True, None),
    "published_release_id": (S36, True, None),
}
RELEASE_COLUMNS = {
    "artifact_id": (S36, False, None),
    "organization_id": (S36, False, None),
    "source_version_id": (S36, True, None),
    "content": ((JSON, None), False, None),
    "status": ((String, 20), False, "publishing"),
    "error": ((Text, None), True, None),
    "published_by": (S36, True, None),
    "published_at": ((DateTime, None), True, None),
    "bow_source_access": ((JSON, None), True, None),
    "theme_name": ((String, None), True, None),
    "theme_overrides": ((JSON, None), True, None),
    "thumbnail_path": ((String, 512), True, None),
    "pptx_path": ((String, 512), True, None),
}
SHARE_COLUMNS = {
    "artifact_id": (S36, False, None),
    "user_id": (S36, True, None),
    "group_id": (S36, True, None),
}
# (table, column) -> (fk name, referred table, ondelete or None)
FOREIGN_KEYS = {
    ("artifacts", "owner_id"): ("fk_artifacts_owner_id", "users", None),
    ("artifacts", "project_id"): ("fk_artifacts_project_id", "projects", None),
    ("artifacts", "published_release_id"): ("fk_artifacts_published_release_id", "artifact_releases", "SET NULL"),
    ("artifact_releases", "artifact_id"): ("fk_artifact_releases_artifact_id", "artifacts", "CASCADE"),
    ("artifact_releases", "organization_id"): ("fk_artifact_releases_organization_id", "organizations", None),
    ("artifact_releases", "source_version_id"): ("fk_artifact_releases_source_version_id", "artifact_versions", "SET NULL"),
    ("artifact_releases", "published_by"): ("fk_artifact_releases_published_by", "users", None),
    ("artifact_release_data_source_association", "artifact_release_id"):
        ("fk_artifact_release_ds_release_id", "artifact_releases", "CASCADE"),
    ("artifact_release_data_source_association", "data_source_id"):
        ("fk_artifact_release_ds_data_source_id", "data_sources", "CASCADE"),
    ("artifact_release_file_association", "artifact_release_id"):
        ("fk_artifact_release_file_release_id", "artifact_releases", "CASCADE"),
    ("artifact_release_file_association", "file_id"): ("fk_artifact_release_file_file_id", "files", "CASCADE"),
    ("artifact_shares", "artifact_id"): ("fk_artifact_shares_artifact_id", "artifacts", "CASCADE"),
    ("artifact_shares", "user_id"): ("fk_artifact_shares_user_id", "users", "CASCADE"),
    ("artifact_shares", "group_id"): ("fk_artifact_shares_group_id", "groups", "CASCADE"),
    ("queries", "artifact_release_id"): ("fk_queries_artifact_release_id", "artifact_releases", None),
    ("visualizations", "artifact_release_id"): ("fk_visualizations_artifact_release_id", "artifact_releases", None),
    ("reports", "source_artifact_id"): ("fk_reports_source_artifact_id", "artifacts", None),
}
INDEXES = {
    "artifacts": {"ix_artifacts_owner_id", "ix_artifacts_project_id", "ix_artifacts_published_release_id"},
    "artifact_releases": {"ix_artifact_releases_artifact_id", "ix_artifact_releases_organization_id"},
    "artifact_shares": {"ix_artifact_shares_artifact_id", "ix_artifact_shares_user_id", "ix_artifact_shares_group_id"},
    "queries": {"ix_queries_artifact_release_id"},
    "visualizations": {"ix_visualizations_artifact_release_id"},
    "reports": {"ix_reports_source_artifact_id"},
}
UNIQUE = {
    "artifacts": {"uq_artifacts_slug": ["slug"]},
    "artifact_shares": {"uq_artifact_share_user": ["artifact_id", "user_id"],
                        "uq_artifact_share_group": ["artifact_id", "group_id"]},
}
CHECKS = {
    "artifact_shares": "ck_artifact_shares_one_principal",
    "queries": "ck_queries_single_owner",
    "visualizations": "ck_visualizations_single_owner",
}
NEWLY_NULLABLE = [("artifacts", "report_id"), ("visualizations", "report_id"), ("step_user_results", "report_id"),
                  ("queries", "widget_id")]  # the last one by dashfc02
NEW_TABLES = ["artifact_releases", "artifact_release_data_source_association",
              "artifact_release_file_association", "artifact_shares"]


def _bool_default(raw):
    s = str(raw).strip("'\" ()").lower()
    return {"1": True, "true": True, "0": False, "false": False}.get(s, raw)


def _default(raw):
    if raw is None:
        return None
    b = _bool_default(raw)
    if isinstance(b, bool):
        return b
    s = str(raw)
    s = s.split("::")[0].strip().strip("'\"")
    return s


def _check_columns(insp, table, spec):
    cols = {c["name"]: c for c in insp.get_columns(table)}
    problems = []
    for name, ((typ, length), nullable, default) in spec.items():
        if name not in cols:
            problems.append(f"{table}.{name} missing")
            continue
        c = cols[name]
        if not isinstance(c["type"], typ):
            problems.append(f"{table}.{name} type {c['type']!r}, want {typ.__name__}")
        if length is not None and getattr(c["type"], "length", None) != length:
            problems.append(f"{table}.{name} length {getattr(c['type'], 'length', None)}, want {length}")
        if c["nullable"] != nullable:
            problems.append(f"{table}.{name} nullable={c['nullable']}, want {nullable}")
        if _default(c.get("default")) != default:
            problems.append(f"{table}.{name} server default {c.get('default')!r}, want {default!r}")
    return problems


@pytest.fixture
def insp():
    engine = create_engine(sync_test_url())
    try:
        yield inspect(engine)
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_new_columns_and_tables_are_exactly_as_planned(insp):
    problems = []
    tables = set(insp.get_table_names())
    problems += [f"table {t} missing" for t in NEW_TABLES if t not in tables]
    if not problems:
        problems += _check_columns(insp, "artifacts", NEW_ARTIFACT_COLUMNS)
        problems += _check_columns(insp, "artifact_releases", RELEASE_COLUMNS)
        problems += _check_columns(insp, "artifact_shares", SHARE_COLUMNS)
        for table in ("queries", "visualizations"):
            problems += _check_columns(insp, table, {"artifact_release_id": (S36, True, None)})
        problems += _check_columns(insp, "reports", {"source_artifact_id": (S36, True, None)})
        for table, column in NEWLY_NULLABLE:
            col = next(c for c in insp.get_columns(table) if c["name"] == column)
            if not col["nullable"]:
                problems.append(f"{table}.{column} is still NOT NULL")
        for table in ("artifact_release_data_source_association", "artifact_release_file_association"):
            pk = insp.get_pk_constraint(table)["constrained_columns"]
            if len(pk) != 2:
                problems.append(f"{table} primary key {pk}, want both columns")
    assert not problems, "\n".join(problems)


@pytest.mark.e2e
def test_foreign_keys_indexes_uniques_and_checks_are_named_as_planned(insp):
    problems = []
    for (table, column), (name, referred, ondelete) in FOREIGN_KEYS.items():
        fks = [fk for fk in insp.get_foreign_keys(table) if fk["constrained_columns"] == [column]]
        if not fks:
            problems.append(f"{table}.{column}: no foreign key")
            continue
        fk = fks[0]
        if fk.get("name") != name:
            problems.append(f"{table}.{column}: fk named {fk.get('name')!r}, want {name!r}")
        if fk["referred_table"] != referred:
            problems.append(f"{table}.{column}: refers to {fk['referred_table']}, want {referred}")
        got = (fk.get("options") or {}).get("ondelete")
        if (got or None) != ondelete:
            problems.append(f"{table}.{column}: ondelete {got!r}, want {ondelete!r}")
    for table, names in INDEXES.items():
        have = {i["name"] for i in insp.get_indexes(table)}
        problems += [f"{table}: index {n} missing" for n in names - have]
    for table, uniques in UNIQUE.items():
        have = {u["name"]: u["column_names"] for u in insp.get_unique_constraints(table)}
        have.update({i["name"]: i["column_names"] for i in insp.get_indexes(table) if i.get("unique")})
        for name, cols in uniques.items():
            if have.get(name) != cols:
                problems.append(f"{table}: unique {name} is {have.get(name)}, want {cols}")
    for table, name in CHECKS.items():
        if name not in {c["name"] for c in insp.get_check_constraints(table)}:
            problems.append(f"{table}: check {name} missing")
    assert not problems, "\n".join(problems)


@pytest.mark.e2e
def test_models_declare_what_the_database_has(insp):
    """For every table this phase touches, the ORM columns and nullability
    match the migrated database (no other test compares the two)."""
    import app.models.artifact  # noqa: F401
    import app.models.artifact_release  # noqa: F401
    from app.models.base import Base

    changed = {(t, c) for t, c in NEWLY_NULLABLE}
    changed |= {("artifacts", c) for c in NEW_ARTIFACT_COLUMNS}
    changed |= {("queries", "artifact_release_id"), ("visualizations", "artifact_release_id"),
                ("reports", "source_artifact_id")}
    problems = []
    for table in ["artifacts", "queries", "visualizations", "step_user_results", "reports", *NEW_TABLES]:
        model = Base.metadata.tables.get(table)
        if model is None:
            problems.append(f"no model for {table}")
            continue
        db = {c["name"]: c["nullable"] for c in insp.get_columns(table)}
        problems += [f"{table}.{c.name} is in the model, not in the database"
                     for c in model.columns if c.name not in db]
        problems += [f"{table}.{col} is in the database, not in the model"
                     for t, col in sorted(changed) if t == table and col not in model.columns]
        if table in NEW_TABLES:
            problems += [f"{table}.{name} is in the database, not in the model"
                         for name in db if name not in model.columns]
        checked = [c for c in model.columns if c.name in db and not c.primary_key
                   and (table in NEW_TABLES or (table, c.name) in changed)]
        problems += [f"{table}.{c.name}: model nullable={c.nullable}, database {db[c.name]}"
                     for c in checked if c.nullable != db[c.name]]
    model_slug = {(i.name, i.unique) for i in Base.metadata.tables["artifacts"].indexes}
    if ("uq_artifacts_slug", True) not in model_slug:
        problems.append("artifacts model does not declare unique uq_artifacts_slug")
    for table in NEW_TABLES:
        db_pk = insp.get_pk_constraint(table).get("name")
        model_pk = Base.metadata.tables[table].primary_key.name
        if db_pk and model_pk != db_pk:
            problems.append(f"{table}: model primary key named {model_pk!r}, database {db_pk!r}")
    assert not problems, "\n".join(problems)


@pytest.mark.e2e
def test_existing_dashboards_take_the_conversation_defaults(corpus, corpus_rows):
    """A dashboard written before the upgrade gets, from the migration, the
    same settings a conversation has by default."""
    with corpus_rows.engine.connect() as conn:
        row = conn.execute(text(
            "SELECT visibility, shared_run_identity, include_data_tab, chat_enabled, refresh_on_view, "
            "report_id, owner_id, slug, published_release_id FROM artifacts WHERE id = :id"
        ), {"id": corpus.ids["conv_one_dash/dash"]}).mappings().one()
    assert row["visibility"] == "none" and row["shared_run_identity"] == "viewer"
    assert bool(row["include_data_tab"]) is True and bool(row["refresh_on_view"]) is True
    assert bool(row["chat_enabled"]) is False
    assert row["report_id"] == corpus.reports["conv_one_dash"]
    assert row["owner_id"] is None and row["slug"] is None and row["published_release_id"] is None


@pytest.mark.e2e
def test_code_that_does_not_know_the_new_columns_still_creates_dashboards(corpus, corpus_rows):
    """During a rolling update the previous release keeps inserting artifacts
    with only the columns it knows. The database defaults fill the rest (the
    ORM's Python defaults would hide a missing server default, so this goes
    through plain SQL)."""
    from datetime import datetime

    from tests.fixtures.dashboard_corpus.rows import new_id

    art = new_id()
    with corpus_rows.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO artifacts (id, report_id, organization_id, created_by, mode, title, created_at, updated_at)"
            " VALUES (:id, :r, :o, :u, 'page', 'old code', :t, :t)"
        ), {"id": art, "r": corpus.reports["conv_no_dash"], "o": corpus.cast.org_id,
            "u": corpus.cast.id("owner"), "t": datetime(2026, 3, 5)})
        row = conn.execute(text(
            "SELECT visibility, shared_run_identity, include_data_tab, chat_enabled, refresh_on_view "
            "FROM artifacts WHERE id = :id"), {"id": art}).mappings().one()
    assert row["visibility"] == "none" and row["shared_run_identity"] == "viewer"
    assert bool(row["include_data_tab"]) is True and bool(row["refresh_on_view"]) is True
    assert bool(row["chat_enabled"]) is False


@pytest.mark.e2e
def test_check_constraints_are_enforced(corpus, corpus_rows):
    rows, ids = corpus_rows, corpus.ids
    release = rows.insert("artifact_releases", artifact_id=ids["conv_one_dash/dash"],
                          organization_id=corpus.cast.org_id, content={})
    with pytest.raises(IntegrityError):  # a query owned by a conversation and a release at once
        rows.update("queries", ids["conv_one_dash/q_sales"], artifact_release_id=release)
    with pytest.raises(IntegrityError):
        rows.update("visualizations", ids["conv_one_dash/q_sales/viz"], artifact_release_id=release)
    with pytest.raises(IntegrityError):  # a share with no principal
        rows.insert("artifact_shares", artifact_id=ids["conv_one_dash/dash"])
    with pytest.raises(IntegrityError):  # and with both
        rows.insert("artifact_shares", artifact_id=ids["conv_one_dash/dash"],
                    user_id=corpus.cast.id("member"), group_id=corpus.cast.groups["analysts"])
    # The allowed shapes still go in.
    rows.update("queries", ids["conv_no_dash/q_sales"], report_id=None, artifact_release_id=release)
    rows.insert("artifact_shares", artifact_id=ids["conv_one_dash/dash"], user_id=corpus.cast.id("member"))


def _norm_default(raw):
    if raw is None:
        return None
    b = _bool_default(raw)
    if isinstance(b, bool):
        return b
    return str(raw).replace("(", "").replace(")", "").split("::")[0].strip().strip("'\"")


def _schema(url):
    """Everything the downgrade must restore: columns with type, nullability
    and default; foreign keys with name and ON DELETE; indexes; unique, check
    and primary-key constraints."""
    engine = create_engine(url)
    try:
        i = inspect(engine)
        out = {}
        for t in sorted(i.get_table_names()):
            # alembic's own table; APScheduler's, which it creates at runtime.
            if t in ("alembic_version", "apscheduler_jobs"):
                continue
            pk = i.get_pk_constraint(t)
            out[t] = {
                "columns": {c["name"]: (str(c["type"]), c["nullable"], _norm_default(c.get("default")))
                            for c in i.get_columns(t)},
                "fks": {(tuple(f["constrained_columns"]), f["referred_table"]):
                        (f.get("name"), (f.get("options") or {}).get("ondelete"))
                        for f in i.get_foreign_keys(t)},
                "indexes": {x["name"]: (tuple(x["column_names"]), bool(x.get("unique"))) for x in i.get_indexes(t)},
                "uniques": {u["name"]: tuple(u["column_names"]) for u in i.get_unique_constraints(t)},
                "checks": {c["name"]: c["sqltext"] for c in i.get_check_constraints(t)},
                "pk": tuple(pk["constrained_columns"]),
            }
        return out
    finally:
        engine.dispose()


def _restricted_to(head, main):
    """`head` keeping only what `main` has: the pre-existing part of the schema."""
    out = {}
    for t, spec in main.items():
        h = head.get(t, {})
        out[t] = {k: ({n: v for n, v in h.get(k, {}).items() if n in spec[k]}
                      if isinstance(spec[k], dict) else h.get(k))
                  for k in spec}
    return out


def _leftover_batch_tables(url):
    engine = create_engine(url)
    try:
        return [t for t in inspect(engine).get_table_names() if t.startswith("_alembic_tmp")]
    finally:
        engine.dispose()


def _counts(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
                    for t in inspect(engine).get_table_names() if t != "alembic_version"}
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_downgrade_restores_main_exactly_and_upgrade_again_keeps_every_row(corpus, tmp_path):
    from alembic import command

    url = sync_test_url()
    with scratch_database(tmp_path) as base_url:
        with alembic_pointed_at(base_url) as cfg:
            command.upgrade(cfg, MAIN_BASE_REVISION)
        main_schema = _schema(base_url)
    head_schema, head_counts = _schema(url), _counts(url)
    # The upgrade keeps everything main had on the tables it rebuilds
    # (defaults, ON DELETE, uniques, indexes -- SQLite batch mode copies them),
    # except the planned NOT NULL relaxations.
    expected = copy.deepcopy(main_schema)
    for table, column in NEWLY_NULLABLE:
        typ, _nullable, default = expected[table]["columns"][column]
        expected[table]["columns"][column] = (typ, True, default)
    assert _restricted_to(head_schema, main_schema) == expected

    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, MAIN_BASE_REVISION)
    assert current_revisions(url) == (MAIN_BASE_REVISION,)
    assert _schema(url) == main_schema
    down_counts = _counts(url)
    assert {t: n for t, n in head_counts.items() if t in down_counts} == down_counts

    # Back to where the corpus stands (before the data migration, dashfc08).
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, PRE_MIGRATION_REVISION)
    assert _schema(url) == head_schema
    assert _counts(url) == head_counts


def _make_release(corpus, rows):
    return rows.insert("artifact_releases", artifact_id=corpus.ids["conv_one_dash/dash"],
                       organization_id=corpus.cast.org_id, content={})


# One case per kind of data the downgrade must not delete (dashfc01 _DEPENDENT_DATA).
DEPENDENT_CASES = {
    "artifact_without_conversation":
        lambda c, r: r.update("artifacts", c.ids["conv_one_dash/dash"], report_id=None),
    "chart_without_conversation":
        lambda c, r: r.update("visualizations", c.ids["conv_one_dash/q_sales/viz"], report_id=None),
    "viewer_result_without_conversation": lambda c, r: _null_viewer_result_report(c, r),
    "release": lambda c, r: _make_release(c, r),
    "share": lambda c, r: r.insert("artifact_shares", artifact_id=c.ids["conv_one_dash/dash"],
                                   user_id=c.cast.id("member")),
    "release_owned_query": lambda c, r: r.update("queries", c.ids["conv_no_dash/q_sales"], report_id=None,
                                                 artifact_release_id=_make_release(c, r)),
    "release_owned_chart": lambda c, r: r.update("visualizations", c.ids["conv_one_dash/q_sales/viz"],
                                                 report_id=None, artifact_release_id=_make_release(c, r)),
    "viewer_chat_tied_to_dashboard": lambda c, r: r.update("reports", c.reports["chat_enabled/thread_member"],
                                                           source_artifact_id=c.ids["chat_enabled/dash"]),
    "dashboard_slug": lambda c, r: r.update("artifacts", c.ids["conv_one_dash/dash"], slug="sales"),
    "dashboard_visibility": lambda c, r: r.update("artifacts", c.ids["conv_one_dash/dash"], visibility="internal"),
    "dashboard_data_tab_off": lambda c, r: r.update("artifacts", c.ids["conv_one_dash/dash"],
                                                    include_data_tab=False),
}


def _null_viewer_result_report(corpus, rows):
    with rows.engine.begin() as conn:
        conn.execute(text("UPDATE step_user_results SET report_id = NULL WHERE report_id = :r"),
                     {"r": corpus.reports["run_viewer_results"]})


@pytest.mark.e2e
@pytest.mark.parametrize("case", sorted(DEPENDENT_CASES))
def test_downgrade_refuses_to_drop_data_that_needs_the_new_schema(case, corpus, corpus_rows):
    """The downgrade stops with its own message, before touching the schema,
    whenever data exists that the previous schema cannot hold."""
    from alembic import command

    url = sync_test_url()
    DEPENDENT_CASES[case](corpus, corpus_rows)
    # Undo dashfc02 first (every query here still has its widget): its own
    # downgrade commits on SQLite before dashfc01's refusal runs.
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc01")
    head = "dashfc01"
    before = _schema(url)
    with alembic_pointed_at(url) as cfg, pytest.raises(RuntimeError, match="dashfc01 downgrade refused"):
        command.downgrade(cfg, MAIN_BASE_REVISION)
    assert current_revisions(url) == (head,)
    assert _schema(url) == before
    assert _leftover_batch_tables(url) == []


@pytest.mark.e2e
def test_concurrent_upgrades_take_turns(tmp_path):
    """Every pod runs `alembic upgrade head` on boot (start.sh). Two pods at
    once on Postgres must both finish, with the chain applied once. Separate
    processes, as pods are (alembic's context is not thread-safe)."""
    if not sync_test_url().startswith("postgresql"):
        pytest.skip("the migration lock is a Postgres advisory lock")
    import os
    import subprocess
    import sys
    from pathlib import Path

    from alembic import command

    backend = Path(__file__).resolve().parents[3]
    with scratch_database(tmp_path) as url:
        with alembic_pointed_at(url) as cfg:
            command.upgrade(cfg, MAIN_BASE_REVISION)
        env = {**os.environ, "TESTING": "true", "TEST_DATABASE_URL": url}
        pods = [subprocess.Popen([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for _ in range(2)]
        outputs = [p.communicate(timeout=600)[0] for p in pods]
        failed = [out[-2000:] for p, out in zip(pods, outputs, strict=True) if p.returncode != 0]
        assert not failed, "\n---\n".join(failed)
        assert current_revisions(url) == (script_directory().get_current_head(),)
        rows = Rows(create_engine(url))
        try:
            with rows.engine.connect() as conn:
                assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 1
        finally:
            rows.close()
