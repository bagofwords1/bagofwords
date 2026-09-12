"""overlayconn01 must round-trip, including over rows the new key legalizes.

`user_data_source_tables` was keyed on (data_source_id, user_id, table_name).
Per-connection overlays add connection_id to that key, which makes two rows
with the same table name legal — one per connection. Recreating the old, narrow
constraint on the way down then failed outright:

    sqlite3.IntegrityError: UNIQUE constraint failed:
      user_data_source_tables.data_source_id, ..., table_name

so any deployment that had actually used the feature could not be rolled back.
The downgrade reconciles those duplicates first: accessible beats inaccessible
for a name, lowest id breaks remaining ties.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/unit/test_overlay_connection_migration.py -v
"""
import os
import uuid
from pathlib import Path

import pytest
from alembic import op  # noqa: F401  (imported for its side effects on context)
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import importlib.util


REV = Path(__file__).resolve().parents[2] / "alembic" / "versions" / \
    "overlayconn01_per_connection_user_overlay.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("overlayconn01_rev", REV)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The pre-migration schema, only as far as this migration touches it. Built
# from metadata rather than raw DDL so it is correct on both dialects (a
# `BOOLEAN DEFAULT 1` is fine in SQLite and a type error in PostgreSQL).
def _pre_metadata() -> sa.MetaData:
    md = sa.MetaData()
    sa.Table("connections", md, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table(
        "connection_tables", md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36)),
    )
    sa.Table(
        "datasource_tables", md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_table_id", sa.String(36)),
    )
    sa.Table(
        "user_data_source_tables", md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("data_source_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("table_name", sa.String, nullable=False),
        sa.Column("data_source_table_id", sa.String(36)),
        sa.Column("is_accessible", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String, nullable=False, server_default="accessible"),
        sa.Column("metadata_json", sa.JSON),
        sa.UniqueConstraint(
            "data_source_id", "user_id", "table_name", name="uq_user_ds_table"
        ),
    )
    sa.Table(
        "user_data_source_columns", md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_data_source_table_id", sa.String(36), nullable=False),
        sa.Column("column_name", sa.String, nullable=False),
        sa.Column("is_accessible", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_masked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("data_type", sa.String),
    )
    return md


def _pg_url():
    """A PostgreSQL to also run against, when one is offered.

    SQLite rebuilds the table for every constraint change and PostgreSQL alters
    it in place, so the two exercise genuinely different code in
    batch_alter_table — a downgrade that passes on one can still fail on the
    other."""
    return os.environ.get("BOW_TEST_PG_URL")


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    if request.param == "postgres":
        url = _pg_url()
        if not url:
            pytest.skip("set BOW_TEST_PG_URL to also run against PostgreSQL")
        eng = sa.create_engine(url)
        schema = f"mig_{uuid.uuid4().hex[:8]}"
        with eng.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        eng = sa.create_engine(
            url, connect_args={"options": f"-csearch_path={schema}"}
        )
    else:
        eng = sa.create_engine(f"sqlite:///{tmp_path/'mig.db'}")
    _pre_metadata().create_all(eng)
    return eng


def _run(engine, fn):
    """Run one migration function against a live connection, the way alembic
    does — `op` is a module-level proxy bound to the current context."""
    mod = _load_revision()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(mod, fn)()


def _seed_shared_dataset(engine, ds="ds-1", user="u-1"):
    """One user, one agent, the same table name on two connections."""
    rows = []
    with engine.begin() as conn:
        for i, cname in enumerate(("conn-a", "conn-b")):
            conn.execute(sa.text(
                "INSERT INTO connections (id) VALUES (:i)"), {"i": cname})
            ct = f"ct-{i}"
            dst = f"dst-{i}"
            conn.execute(sa.text(
                "INSERT INTO connection_tables (id, connection_id) VALUES (:i, :c)"),
                {"i": ct, "c": cname})
            conn.execute(sa.text(
                "INSERT INTO datasource_tables (id, connection_table_id) VALUES (:i, :c)"),
                {"i": dst, "c": ct})
            rows.append((f"row-{i}", dst, cname))
    return rows


def _insert_overlay(engine, row_id, ds, user, name, dst=None, accessible=True):
    with engine.begin() as conn:
        conn.execute(sa.text(
            """INSERT INTO user_data_source_tables
               (id, data_source_id, user_id, table_name, data_source_table_id,
                is_accessible, status)
               VALUES (:id, :ds, :u, :n, :dst, :acc, 'accessible')"""),
            {"id": row_id, "ds": ds, "u": user, "n": name, "dst": dst,
             "acc": accessible})
        conn.execute(sa.text(
            """INSERT INTO user_data_source_columns
               (id, user_data_source_table_id, column_name)
               VALUES (:id, :t, 'id')"""),
            {"id": f"col-{row_id}", "t": row_id})


def _overlay_rows(engine):
    """(id, table_name, is_accessible) — the shape both schemas share.

    is_accessible is normalized to bool: SQLite hands back 1/0."""
    with engine.connect() as conn:
        return [(r[0], r[1], bool(r[2])) for r in conn.execute(sa.text(
            "SELECT id, table_name, is_accessible "
            "FROM user_data_source_tables ORDER BY id"
        ))]


def _connection_ids(engine):
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(sa.text(
            "SELECT id, connection_id FROM user_data_source_tables"))}


def _columns(engine):
    with engine.connect() as conn:
        return {c["name"] for c in sa.inspect(conn).get_columns("user_data_source_tables")}


def _constraints(engine):
    with engine.connect() as conn:
        insp = sa.inspect(conn)
        return {c["name"] for c in insp.get_unique_constraints("user_data_source_tables")} | \
               {i["name"] for i in insp.get_indexes("user_data_source_tables")}


def test_upgrade_backfills_connection_id(engine):
    rows = _seed_shared_dataset(engine)
    for i, (row_id, dst, _conn) in enumerate(rows[:1]):
        _insert_overlay(engine, row_id, "ds-1", "u-1", "orders", dst=dst)

    _run(engine, "upgrade")

    assert "connection_id" in _columns(engine)
    assert "uq_user_ds_conn_table" in _constraints(engine)
    assert _overlay_rows(engine) == [("row-0", "orders", True)]
    assert _connection_ids(engine) == {"row-0": "conn-a"}


def test_upgrade_is_repeatable(engine):
    _seed_shared_dataset(engine)
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    assert "uq_user_ds_conn_table" in _constraints(engine)


def test_downgrade_reconciles_cross_connection_duplicates(engine):
    """The row the new key legalizes: same name, same user, two connections."""
    rows = _seed_shared_dataset(engine)
    _insert_overlay(engine, "row-0", "ds-1", "u-1", "orders", dst=rows[0][1])
    _run(engine, "upgrade")
    # Only legal AFTER the upgrade — this is the row that broke the downgrade.
    _insert_overlay(engine, "row-1", "ds-1", "u-1", "orders", dst=rows[1][1])
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE user_data_source_tables SET connection_id='conn-b' WHERE id='row-1'"))

    _run(engine, "downgrade")

    assert "connection_id" not in _columns(engine)
    assert "uq_user_ds_table" in _constraints(engine)
    assert _overlay_rows(engine) == [("row-0", "orders", True)]
    # Orphaned columns went with the row they belonged to.
    with engine.connect() as conn:
        kept = [r[0] for r in conn.execute(sa.text(
            "SELECT user_data_source_table_id FROM user_data_source_columns"))]
    assert kept == ["row-0"]


def test_downgrade_prefers_the_accessible_row(engine):
    """Losing the connection dimension must not silently revoke access: the
    accessible answer wins even when it sorts second."""
    rows = _seed_shared_dataset(engine)
    _insert_overlay(engine, "row-0", "ds-1", "u-1", "orders",
                    dst=rows[0][1], accessible=False)
    _run(engine, "upgrade")
    _insert_overlay(engine, "row-1", "ds-1", "u-1", "orders",
                    dst=rows[1][1], accessible=True)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE user_data_source_tables SET connection_id='conn-b' WHERE id='row-1'"))

    _run(engine, "downgrade")

    assert _overlay_rows(engine) == [("row-1", "orders", True)]


def test_downgrade_keeps_distinct_names(engine):
    rows = _seed_shared_dataset(engine)
    _insert_overlay(engine, "row-0", "ds-1", "u-1", "orders", dst=rows[0][1])
    _run(engine, "upgrade")
    _insert_overlay(engine, "row-1", "ds-1", "u-1", "customers", dst=rows[1][1])

    _run(engine, "downgrade")

    assert sorted(r[1] for r in _overlay_rows(engine)) == ["customers", "orders"]
