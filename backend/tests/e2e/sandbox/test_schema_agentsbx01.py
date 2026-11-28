"""agentsbx01: the agent-sandbox tables (2026-11-26).

What the migration promises:

- it only ADDS tables; no existing table changes shape, in either direction;
- it applies and rolls back cleanly, and both steps are idempotent (the
  dashboard corpus harness runs revisions unstamped);
- the database itself enforces one non-deleted sandbox per report;
- secrets, keys, tokens, run code and run output are ciphertext at rest.

Runs on SQLite and on PostgreSQL (CI runs both).
"""
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.sandbox import (
    Sandbox, SandboxCompute, SandboxEnvironment, SandboxRun,
)
from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url

pytestmark = pytest.mark.e2e

PREVIOUS = "mrgheads12"
REVISION = "agentsbx01"
TABLES = {
    "sandbox_computes", "sandbox_environments", "sandbox_agent_defaults", "sandboxes",
    "sandbox_runs", "sandbox_events", "sandbox_egress_log", "sandbox_exposures",
}


def _shape(url) -> dict:
    """{table: {column: type}} for every table, plus index names."""
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {
            t: (
                {c["name"]: str(c["type"]) for c in insp.get_columns(t)},
                sorted(i["name"] for i in insp.get_indexes(t) if i["name"]),
            )
            for t in insp.get_table_names() if t != "alembic_version"
        }
    finally:
        engine.dispose()


def _alembic(url, command_name: str, target: str) -> None:
    from alembic import command
    with alembic_pointed_at(url) as cfg:
        getattr(command, command_name)(cfg, target)


def _run_module(url, *calls: str) -> None:
    """Run the revision's upgrade()/downgrade() on their own, unstamped."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    module = script_directory().get_revision(REVISION).module
    engine = create_engine(url)
    try:
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            for call in calls:
                getattr(module, call)()
    finally:
        engine.dispose()


def _org_id(session: Session) -> str:
    """A real organization row: the sandbox tables reference organizations."""
    org_id = str(uuid.uuid4())
    session.execute(
        text("INSERT INTO organizations (id, name, created_at, updated_at) VALUES (:id, :name, :now, :now)"),
        {"id": org_id, "name": f"sbx-{org_id[:8]}", "now": datetime.utcnow()},
    )
    return org_id


@pytest.fixture
def session():
    engine = create_engine(sync_test_url())
    with Session(engine) as s:
        yield s
        s.rollback()
    engine.dispose()


def test_migration_adds_the_eight_tables_and_changes_nothing_else():
    url = sync_test_url()
    with_tables = _shape(url)
    assert TABLES <= set(with_tables)

    _alembic(url, "downgrade", PREVIOUS)
    try:
        without = _shape(url)
        assert not (TABLES & set(without)), "downgrade leaves no sandbox table behind"
        # Every pre-existing table is exactly as it is with the feature's tables present.
        assert without == {t: s for t, s in with_tables.items() if t not in TABLES}
    finally:
        _alembic(url, "upgrade", "head")
    assert _shape(url) == with_tables
    assert REVISION in {r.revision for r in script_directory().iterate_revisions("head", PREVIOUS)}
    assert current_revisions(url) == (script_directory().get_current_head(),)


def test_upgrade_and_downgrade_are_idempotent():
    url = sync_test_url()
    before = _shape(url)
    _run_module(url, "upgrade", "upgrade")
    assert _shape(url) == before
    _run_module(url, "downgrade", "downgrade")
    try:
        assert not (TABLES & set(_shape(url)))
    finally:
        _run_module(url, "upgrade")
    assert _shape(url) == before


def test_models_and_migration_agree_on_columns():
    """The ORM models and the hand-written migration describe the same tables."""
    from app.models.base import BaseSchema

    shape = _shape(sync_test_url())
    for table in sorted(TABLES):
        model_columns = {c.name for c in BaseSchema.metadata.tables[table].columns}
        assert set(shape[table][0]) == model_columns, table
        model_indexes = {i.name for i in BaseSchema.metadata.tables[table].indexes}
        assert model_indexes <= set(shape[table][1]), table


def test_database_allows_one_live_sandbox_per_report(session):
    org = _org_id(session)
    report_a, report_b = str(uuid.uuid4()), str(uuid.uuid4())
    first = Sandbox(organization_id=org, report_id=report_a, state="ready", limits={})
    session.add_all([first, Sandbox(organization_id=org, report_id=report_b, state="ready", limits={})])
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(Sandbox(organization_id=org, report_id=report_a, state="provisioning", limits={}))
            session.flush()

    # Once the first one is deleted the report may get a new sandbox, and the
    # deleted row stays as history.
    first.state = "deleted"
    first.deleted_at = datetime.utcnow()
    session.flush()
    session.add(Sandbox(organization_id=org, report_id=report_a, state="provisioning", limits={}))
    session.flush()
    rows = session.execute(text("SELECT count(*) FROM sandboxes WHERE report_id = :r"), {"r": report_a}).scalar()
    assert rows == 2


def test_names_are_unique_among_live_computes_and_environments(session):
    org = _org_id(session)
    compute = SandboxCompute(organization_id=org, name="sbx-1", type="fake", config={}, driver_state={},
                             capabilities=[], labels=[], bootstrap_steps=[])
    env = SandboxEnvironment(organization_id=org, name="Default", image="img", allowed_domains=[], env_vars={},
                             compute_labels=[], ports=[])
    session.add_all([compute, env])
    session.flush()
    for duplicate in (
        SandboxCompute(organization_id=org, name="sbx-1", type="fake", config={}, driver_state={},
                       capabilities=[], labels=[], bootstrap_steps=[]),
        SandboxEnvironment(organization_id=org, name="Default", image="other", allowed_domains=[], env_vars={},
                           compute_labels=[], ports=[]),
    ):
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(duplicate)
                session.flush()
    # A deleted compute frees its name.
    compute.deleted_at = datetime.utcnow()
    session.flush()
    session.add(SandboxCompute(organization_id=org, name="sbx-1", type="fake", config={}, driver_state={},
                               capabilities=[], labels=[], bootstrap_steps=[]))
    session.flush()


def test_secrets_are_ciphertext_at_rest_and_readable_through_the_models(session):
    org = _org_id(session)
    marker = uuid.uuid4().hex  # unique per run: any plaintext leak is findable
    key, password, secret, token, code, out = (f"{kind}-{marker}" for kind in ("key", "pw", "secret", "tok", "code", "out"))

    compute = SandboxCompute(organization_id=org, name=f"b-{marker[:8]}", type="fake", config={}, driver_state={},
                             capabilities=[], labels=[], bootstrap_steps=[], credentials={"ssh_private_key": key})
    env = SandboxEnvironment(organization_id=org, name=f"e-{marker[:8]}", image="img", allowed_domains=[],
                             env_vars={}, compute_labels=[], ports=[],
                             registry_password=password, secrets={"API_KEY": secret})
    session.add_all([compute, env])
    session.flush()
    sandbox = Sandbox(organization_id=org, report_id=str(uuid.uuid4()), state="ready", limits={}, token=token,
                      compute_id=compute.id, environment_id=env.id)
    session.add(sandbox)
    session.flush()
    run = SandboxRun(sandbox_id=sandbox.id, organization_id=org, report_id=sandbox.report_id,
                     code=code, stdout=out, stderr=out, input_file_ids=[], artifact_file_ids=[])
    session.add(run)
    session.flush()
    ids = (compute.id, env.id, sandbox.id, run.id)
    session.expire_all()

    raw = " ".join(
        str(value)
        for table, columns, row_id in (
            ("sandbox_computes", "credentials", ids[0]),
            ("sandbox_environments", "registry_password, secrets", ids[1]),
            ("sandboxes", "token", ids[2]),
            ("sandbox_runs", "code, stdout, stderr", ids[3]),
        )
        for value in session.execute(text(f"SELECT {columns} FROM {table} WHERE id = :id"), {"id": row_id}).one()
    )
    assert marker not in raw
    assert "API_KEY" not in raw, "secret names are not readable either"

    assert session.get(SandboxCompute, ids[0]).credentials == {"ssh_private_key": key}
    loaded_env = session.get(SandboxEnvironment, ids[1])
    assert loaded_env.registry_password == password and loaded_env.secrets == {"API_KEY": secret}
    assert session.get(Sandbox, ids[2]).token == token
    loaded_run = session.get(SandboxRun, ids[3])
    assert (loaded_run.code, loaded_run.stdout, loaded_run.stderr) == (code, out, out)
