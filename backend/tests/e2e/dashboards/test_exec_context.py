"""The execution core runs from an ExecContext, not from a conversation (phase 2, part A).

Until now StepService._execute_step_code took the Report and read what it
needed off it: data sources, files, organization, the load_step scope and the
bow monitoring client. A published dashboard's copies have no Report, so the
core now reads all of that from an ExecContext. Every existing caller builds
the context from its Report (ExecContext.from_report), so nothing a user sees
changes; the before/after golden files prove that part. These tests prove the
two properties the golden files cannot:

  1. from_report carries exactly what the core used to read off the Report;
  2. the core runs with a context that has no Report at all.
"""
import asyncio

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.step import Step
from app.models.user import User

# Names the clients and files the core handed to the code, so a test can see
# exactly which connections a run was given.
WHAT_THE_CODE_GOT = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({
        "client": sorted(ds_clients.keys()) or ["<none>"],
        "files": [len(excel_files)] * max(len(ds_clients), 1),
    })
"""

TEN_ROWS = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"n": list(range(10)), "files": [len(excel_files)] * 10,
                         "clients": [len(ds_clients)] * 10})
"""


def _run(coro):
    return asyncio.run(coro)


def _transient_step(code: str) -> Step:
    """A step that belongs to nothing: no widget, no query, no conversation."""
    return Step(title="ad hoc", slug="ad-hoc", status="success", code=code)


@pytest.fixture
def report_with_source(create_user, login_user, whoami, create_report, sqlite_data_source):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds = sqlite_data_source(name="chinook", user_token=token, org_id=org_id, is_public=True)
    report = create_report(title="Context", user_token=token, org_id=org_id, data_sources=[ds["id"]])
    return {"report_id": report["id"], "org_id": org_id, "email": user["email"], "ds_id": ds["id"],
            "token": token}


async def _load(report_id: str, email: str):
    db = async_session_maker()
    report = (await db.execute(
        select(Report).options(selectinload(Report.data_sources), selectinload(Report.files))
        .where(Report.id == report_id)
    )).scalar_one()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    return db, report, user


@pytest.mark.e2e
def test_from_report_carries_what_the_core_used_to_read(report_with_source):
    from app.services.exec_context import ExecContext

    async def check():
        db, report, user = await _load(report_with_source["report_id"], report_with_source["email"])
        try:
            ctx = ExecContext.from_report(report, credential_user=user)
            org = await db.get(Organization, str(report.organization_id))
            assert str((await ctx.organization(db)).id) == str(report.organization_id)
            assert (await ctx.settings(db)).config == (await org.get_settings(db)).config
            assert [str(d.id) for d in ctx.data_sources] == [str(d.id) for d in report.data_sources] \
                == [report_with_source["ds_id"]]
            assert list(ctx.files) == list(report.files)
            assert ctx.credential_user is user
            # load_step and the bow client keep their conversation scope.
            assert ctx.report is report
        finally:
            await db.close()

    _run(check())


@pytest.mark.e2e
def test_from_report_loads_nothing_until_the_core_asks():
    """Building the context reads nothing off the report, exactly as the core
    used to read data sources only when it had no prebuilt clients."""
    from app.services.exec_context import ExecContext

    class Untouchable:
        organization_id = "org-1"

        @property
        def data_sources(self):
            raise AssertionError("data sources read before the core asked")

        @property
        def files(self):
            raise AssertionError("files read before the core asked")

    ExecContext.from_report(Untouchable())


@pytest.mark.e2e
def test_given_organization_and_settings_are_used_as_they_are():
    """A report-level rerun passes the org and its settings once for N steps;
    the context must hand back exactly those objects and fetch nothing. The
    stand-ins are not the rows the database would return, so a context that
    looked them up again would be caught."""
    from types import SimpleNamespace

    from app.services.exec_context import ExecContext

    class NoDb:
        def __getattr__(self, name):
            raise AssertionError(f"the context touched the database ({name})")

    org, settings = SimpleNamespace(id="org-1"), SimpleNamespace(config={})
    report = SimpleNamespace(organization_id="org-1", data_sources=[], files=[])
    ctx = ExecContext.from_report(report, organization=org, organization_settings=settings)
    assert _run(ctx.organization(NoDb())) is org
    assert _run(ctx.settings(NoDb())) is settings


@pytest.mark.e2e
def test_the_core_gives_code_the_same_clients_with_or_without_a_conversation(report_with_source):
    """The same data sources reach the code whether the context was built from
    the conversation or by hand without one."""
    from app.services.exec_context import ExecContext
    from app.services.step_service import StepService

    async def check():
        db, report, user = await _load(report_with_source["report_id"], report_with_source["email"])
        try:
            svc = StepService()
            from_report = ExecContext.from_report(report, credential_user=user)
            via_report = await svc._execute_step_code(
                db, _transient_step(WHAT_THE_CODE_GOT), from_report, return_raw_df=True)
            org = await db.get(Organization, str(report.organization_id))
            no_report = ExecContext(organization=org, credential_user=user,
                                    data_sources=list(report.data_sources), files=[])
            assert no_report.report is None
            standalone = await svc._execute_step_code(
                db, _transient_step(WHAT_THE_CODE_GOT), no_report, return_raw_df=True)
            assert isinstance(via_report, pd.DataFrame)
            assert via_report["client"].tolist() == standalone["client"].tolist()
            assert via_report["client"].tolist() != ["<none>"], "the data source never reached the code"
        finally:
            await db.close()

    _run(check())


@pytest.mark.e2e
def test_the_core_runs_without_a_conversation(report_with_source):
    """No Report anywhere. Files and the organization's settings still come
    from the context: the code sees the one file it was given, and the
    organization's row limit (3) caps the saved result. This is what
    refreshing a published dashboard will need."""
    from types import SimpleNamespace

    from app.schemas.organization_settings_schema import FeatureState
    from app.services.exec_context import ExecContext
    from app.services.step_service import StepService

    def get_config(key):
        if key == "limit_row_count":
            return SimpleNamespace(value=3, state=FeatureState.ENABLED)
        return SimpleNamespace(value=False, state=FeatureState.DISABLED)

    async def check():
        db, report, user = await _load(report_with_source["report_id"], report_with_source["email"])
        try:
            org = await db.get(Organization, str(report.organization_id))
            ctx = ExecContext(organization=org, credential_user=user, data_sources=[],
                              files=[SimpleNamespace(path="/nowhere/sales.csv", filename="sales.csv")],
                              organization_settings=SimpleNamespace(get_config=get_config))
            out = await StepService()._execute_step_code(db, _transient_step(TEN_ROWS), ctx)
            rows = out.get("rows") if isinstance(out, dict) else None
            assert rows and len(rows) == 3, out
            assert rows[0]["files"] == 1 and rows[0]["clients"] == 0, rows[0]
        finally:
            await db.close()

    _run(check())


@pytest.mark.e2e
def test_clients_are_built_with_the_context_credential_user(report_with_source, create_user,
                                                           sqlite_data_source):  # noqa: F811
    """Whose credentials open the connections comes from the context: a user
    outside the organization is refused a private data source."""
    from fastapi import HTTPException

    from app.services.exec_context import ExecContext
    from app.services.step_service import StepService

    private = sqlite_data_source(name="private", user_token=report_with_source["token"],
                                 org_id=report_with_source["org_id"], is_public=False)
    outsider = create_user(email="outsider_ctx@test.com")

    async def check():
        db, report, _ = await _load(report_with_source["report_id"], report_with_source["email"])
        try:
            stranger = (await db.execute(select(User).where(User.email == outsider["email"]))).scalar_one()
            org = await db.get(Organization, str(report.organization_id))
            from app.models.data_source import DataSource
            ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                                   .where(DataSource.id == private["id"]))).scalar_one()
            ctx = ExecContext(organization=org, credential_user=stranger, data_sources=[ds], files=[])
            with pytest.raises(HTTPException) as refused:
                await StepService()._execute_step_code(db, _transient_step(WHAT_THE_CODE_GOT), ctx)
            assert refused.value.status_code == 403
        finally:
            await db.close()

    _run(check())


@pytest.mark.e2e
def test_a_conversation_rerun_still_resolves_load_step(report_with_source, test_client):
    """load_step stays scoped to the conversation through ctx.report and the
    organization's settings: a rerun of a step that loads another step of the
    same conversation gets that step's data, exactly as before."""
    import uuid

    from app.models.query import Query
    from app.models.widget import Widget
    from app.services.step_service import StepService

    token = report_with_source["token"]
    resp = test_client.put("/api/organization/settings", json={"config": {"enable_load_step": {"value": True}}},
                           headers={"Authorization": f"Bearer {token}",
                                    "X-Organization-Id": report_with_source["org_id"]})
    assert resp.status_code == 200, resp.text
    report_id, suffix = report_with_source["report_id"], uuid.uuid4().hex[:8]

    async def seed():
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            ids = {}
            for name, code, data in (
                ("base", "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n"
                         "    return pd.DataFrame({'x': [1, 2]})\n",
                 {"rows": [{"x": 1}, {"x": 2}], "columns": [{"field": "x"}]}),
                ("derived", "def generate_df(ds_clients, excel_files, load_step):\n"
                            "    return load_step('base')\n", {"rows": [], "columns": []}),
            ):
                widget = Widget(title=name, slug=f"{name}-{suffix}", report_id=report_id)
                db.add(widget)
                await db.flush()
                query = Query(title=name, report_id=report_id, widget_id=widget.id,
                              organization_id=report.organization_id, user_id=report.user_id)
                db.add(query)
                await db.flush()
                step = Step(title=name, slug=f"s-{name}-{suffix}", status="success", widget_id=widget.id,
                            query_id=query.id, code=code, data=data)
                db.add(step)
                await db.flush()
                query.default_step_id = step.id
                ids[name] = str(step.id)
            await db.commit()
            return ids

    async def rerun(step_id):
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.email == report_with_source["email"]))).scalar_one()
            await StepService().rerun_step(db, step_id, current_user=user)
        async with async_session_maker() as db:
            return (await db.get(Step, step_id)).data

    ids = _run(seed())
    data = _run(rerun(ids["derived"]))
    assert [r["x"] for r in data["rows"]] == [1, 2], data


@pytest.mark.e2e
def test_the_core_no_longer_takes_a_report():
    """Nothing in the core may reach for a Report again; callers pass a context."""
    import inspect

    from app.services.step_service import StepService

    params = inspect.signature(StepService._execute_step_code).parameters
    assert "report" not in params and "ctx" in params
