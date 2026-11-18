"""Organization-scope query guard: what it reports and what it leaves alone.

Inside a request served for an organization, a read, update or delete of an
organization-scoped model must carry a criterion on that model's organization
(directly, or through an entity it is joined to). The guard only observes: it
never changes what a statement returns.
"""
import logging
import uuid

import pytest
from sqlalchemy import create_engine, delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.pool import StaticPool

import main  # noqa: F401 — registers all mappers

from app.core import org_scope_guard as guard
from app.core.request_org import set_request_organization_id
from app.models.base import Base
from app.models.organization import Organization
from app.models.report import Report
from app.models.widget import Widget
from app.settings.bow_config import SecurityConfig

THIS_FILE = "tests/unit/test_org_scope_guard.py"


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def seeded(engine):
    """One organization with a report and a widget; returns their ids."""
    org_id, report_id, widget_id = (str(uuid.uuid4()) for _ in range(3))
    with Session(engine) as session:
        session.add(Organization(id=org_id, name=f"org-{org_id[:8]}"))
        session.add(Report(
            id=report_id, title="r", slug=f"r-{report_id}", user_id=str(uuid.uuid4()),
            organization_id=org_id,
        ))
        session.add(Widget(id=widget_id, title="w", slug=f"w-{widget_id}", report_id=report_id))
        session.commit()
    return {"org": org_id, "report": report_id, "widget": widget_id}


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def warn_mode():
    previous = guard.get_mode()
    guard.install("warn")
    guard.reset_findings()
    guard.reset_caches()
    yield
    guard.install(previous)
    guard.reset_findings()
    set_request_organization_id(None)


@pytest.fixture
def in_org_request(warn_mode, seeded):
    """Behave as code running inside a request authorized for the seeded org."""
    set_request_organization_id(seeded["org"])
    return seeded


def _flagged_models():
    return [finding.models for finding in guard.get_findings()]


# --- what is reported -------------------------------------------------------

def test_read_by_id_alone_is_reported_and_still_returns_the_row(session, in_org_request):
    report = session.execute(
        select(Report).where(Report.id == in_org_request["report"])
    ).scalar_one()

    assert report.id == in_org_request["report"]  # warn mode changes no result
    (finding,) = guard.get_findings()
    assert finding.models == ("Report",)
    assert finding.operation == "select"
    assert "reports" in finding.statement
    assert finding.call_site.startswith(THIS_FILE)


def test_identity_load_by_primary_key_is_reported(session, in_org_request):
    assert session.get(Report, in_org_request["report"]) is not None
    assert _flagged_models() == [("Report",)]


@pytest.mark.parametrize("build", [
    lambda ids: update(Report).where(Report.id == ids["report"]).values(title="x"),
    lambda ids: delete(Report).where(Report.id == ids["report"]),
], ids=["update", "delete"])
def test_bulk_write_by_id_alone_is_reported(session, in_org_request, build):
    session.execute(build(in_org_request))
    assert _flagged_models() == [("Report",)]


def test_join_to_a_parent_without_an_organization_criterion_is_reported(session, in_org_request):
    session.execute(
        select(Widget).join(Widget.report).where(Widget.id == in_org_request["widget"])
    ).all()
    assert _flagged_models() == [("Report",)]


def test_joining_two_organization_columns_does_not_scope_either(session, in_org_request):
    session.execute(
        select(Report)
        .join(Organization, Report.organization_id == Organization.id)
        .where(Report.id == in_org_request["report"])
    ).all()
    assert _flagged_models() == [("Organization", "Report")]


# --- what is left alone -----------------------------------------------------

@pytest.mark.parametrize("build", [
    lambda ids: select(Report).where(Report.id == ids["report"], Report.organization_id == ids["org"]),
    lambda ids: select(Report).where(Report.organization_id.in_([ids["org"]])),
    lambda ids: update(Report).where(Report.id == ids["report"], Report.organization_id == ids["org"]).values(title="y"),
    lambda ids: delete(Report).where(Report.id == "missing", Report.organization_id == ids["org"]),
    lambda ids: select(Organization).where(Organization.id == ids["org"]),
    # parent-scoped access: the widget is reached through its report's organization
    lambda ids: select(Widget).join(Widget.report).where(Widget.id == ids["widget"], Report.organization_id == ids["org"]),
    lambda ids: select(Widget).where(
        Widget.report_id.in_(select(Report.id).where(Report.organization_id == ids["org"]))
    ),
], ids=["select", "select-in", "update", "delete", "organization-by-id", "join-parent", "in-subquery"])
def test_statement_with_an_organization_criterion_is_not_reported(session, in_org_request, build):
    session.execute(build(in_org_request))
    assert guard.get_findings() == []


def test_nothing_is_reported_without_a_request_organization(session, warn_mode, seeded):
    statement = select(Report).where(Report.id == seeded["report"])

    session.execute(statement).all()  # background job, sign-in, startup
    assert guard.get_findings() == []

    set_request_organization_id(seeded["org"])
    session.execute(statement).all()
    assert _flagged_models() == [("Report",)]


@pytest.mark.parametrize("eager", [False, True], ids=["lazy", "selectinload"])
def test_relationship_load_off_a_loaded_row_is_not_reported(engine, session, in_org_request, eager):
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))

    query = select(Widget).where(Widget.id == in_org_request["widget"])
    if eager:
        query = query.options(selectinload(Widget.report))
    widget = session.execute(query).scalar_one()
    assert widget.report.id == in_org_request["report"]

    # the report really was fetched by a separate, id-only statement
    assert any("FROM reports" in sql and "organization_id =" not in sql for sql in statements)
    assert guard.get_findings() == []


def test_system_scope_opt_outs_are_not_reported(session, in_org_request):
    statement = select(Report).where(Report.id == in_org_request["report"])

    session.execute(statement.execution_options(org_scope="system")).all()
    session.execute(statement, execution_options={"org_scope": "system"}).all()
    session.expunge_all()
    session.get(Report, in_org_request["report"], execution_options={"org_scope": "system"})
    with guard.org_scope_system():
        session.execute(statement).all()
    assert guard.get_findings() == []

    session.execute(statement).all()  # the scope ended with the block
    assert _flagged_models() == [("Report",)]


# --- modes ------------------------------------------------------------------

def test_enforce_mode_raises_for_an_unscoped_statement_only(session, in_org_request):
    guard.install("enforce")

    scoped = select(Report).where(
        Report.id == in_org_request["report"], Report.organization_id == in_org_request["org"]
    )
    assert session.execute(scoped).scalar_one().id == in_org_request["report"]

    with pytest.raises(guard.OrgScopeViolation) as raised:
        session.execute(select(Report).where(Report.id == in_org_request["report"]))
    assert "Report" in str(raised.value)


def test_off_mode_registers_no_hook_and_records_nothing(session, in_org_request):
    guard.install("off")

    assert not guard.is_installed()
    assert not event.contains(Session, "do_orm_execute", guard._on_orm_execute)
    session.execute(select(Report).where(Report.id == in_org_request["report"])).all()
    assert guard.get_findings() == []


def test_install_rejects_an_unknown_mode(warn_mode):
    with pytest.raises(ValueError):
        guard.install("loud")
    assert guard.get_mode() == "warn"


def test_setting_defaults_to_off_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("BOW_ORG_SCOPE_GUARD", raising=False)
    assert SecurityConfig().org_scope_guard == "off"
    assert SecurityConfig(org_scope_guard="enforce").org_scope_guard == "enforce"
    assert SecurityConfig(org_scope_guard=None).org_scope_guard == "off"  # unresolved ${ENV}

    monkeypatch.setenv("BOW_ORG_SCOPE_GUARD", "warn")
    assert SecurityConfig().org_scope_guard == "warn"
    monkeypatch.setenv("BOW_ORG_SCOPE_GUARD", "shout")
    assert SecurityConfig().org_scope_guard == "off"


# --- reporting --------------------------------------------------------------

def test_a_repeated_statement_is_logged_once_and_counted(session, in_org_request, caplog):
    def read_by_id(report_id):
        return session.execute(select(Report).where(Report.id == report_id)).all()

    with caplog.at_level(logging.WARNING, logger=guard.logger.name):
        for report_id in (in_org_request["report"], "another-id", "a-third-id"):
            read_by_id(report_id)

        (finding,) = guard.get_findings()
        assert finding.count == 3
        records = [r for r in caplog.records if r.name == guard.logger.name]
        assert len(records) == 1
        assert records[0].org_scope_guard["models"] == ["Report"]
        assert records[0].org_scope_guard["call_site"] == finding.call_site

        # the same statement from another call site is a separate finding
        session.execute(select(Report).where(Report.id == "from-elsewhere")).all()
        assert len(guard.get_findings()) == 2
        assert len([r for r in caplog.records if r.name == guard.logger.name]) == 2

    report = guard.findings_markdown()
    assert finding.call_site in report and "Report" in report


@pytest.mark.asyncio
async def test_async_session_finding_points_at_the_awaiting_code(warn_mode):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Report.__table__.create)
        set_request_organization_id("some-org")
        async with AsyncSession(engine) as session:
            await session.execute(select(Report.id).where(Report.id == "x"))
            assert await session.get(Report, "x", options=[selectinload("*")]) is None
    finally:
        await engine.dispose()

    findings = guard.get_findings()
    assert [f.models for f in findings] == [("Report",), ("Report",)]
    assert all(f.call_site.startswith(THIS_FILE) for f in findings)
