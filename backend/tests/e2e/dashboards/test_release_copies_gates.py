"""Every gate that used to find a conversation, and met none, now refuses (phase 2, part B).

Before this phase a row without a conversation could not exist apart from
rare legacy rows, so several gates treated "no conversation found" as "no
restriction". A published dashboard's copies have no conversation by design,
so each gate resolves the owner with owner_of and refuses anything that is
not a conversation it can check. One test per gate.
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from tests.e2e.dashboards.release_copies import MARKER, add_release_copy
from tests.fixtures.artifact import seed_artifact


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(create_user, login_user, whoami, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Conversation", user_token=token, org_id=org_id, data_sources=[])

    async def seed():
        async with async_session_maker() as db:
            row = await db.get(Report, report["id"])
            version = await seed_artifact(db, report_id=report["id"], user_id=str(row.user_id),
                                          organization_id=org_id)
            await db.commit()
            return str(version.artifact_id), str(row.user_id)

    artifact_id, user_id = _run(seed())
    copy = _run(add_release_copy(artifact_id=artifact_id, organization_id=org_id, user_id=user_id))
    return SimpleNamespace(org_id=org_id, report_id=report["id"], artifact_id=artifact_id,
                           user_id=user_id, email=user["email"], copy=copy,
                           headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id})


async def _ownerless_step(code: str = "def generate_df(ds_clients, excel_files):\n    return None\n") -> str:
    """A step with no widget, no query and so no owner at all."""
    async with async_session_maker() as db:
        step = Step(title="orphan", slug=f"orphan-{id(code)}", status="success", widget_id=None,
                    query_id=None, code=code, data={"rows": [{"secret": MARKER}], "columns": [{"field": "secret"}]})
        db.add(step)
        await db.commit()
        return str(step.id)


@pytest.mark.e2e
def test_step_data_is_withheld_for_a_copy_and_for_a_step_with_no_owner(world):
    from app.services.viewer_data_policy import resolve_step_data

    orphan = _run(_ownerless_step())

    async def check():
        async with async_session_maker() as db:
            user = await db.get(User, world.user_id)
            for step_id in (world.copy["step"], orphan):
                step = await db.get(Step, step_id)
                resolution = await resolve_step_data(db, step, None, user)
                assert resolution.withheld and MARKER not in str(resolution.data), step_id

    _run(check())


@pytest.mark.e2e
def test_a_copy_that_loads_a_step_counts_as_identity_scoped(world):
    """Its load_step references cannot be resolved against a conversation,
    so the snapshot is treated as one identity's slice."""
    from app.services.identity_taint import step_identity_scope

    async def check():
        async with async_session_maker() as db:
            step = await db.get(Step, world.copy["step"])
            step.code = "def generate_df(ds_clients, excel_files, load_step):\n    return load_step('base')\n"
            tainted, _ = await step_identity_scope(db, step)
            assert tainted

    _run(check())


@pytest.mark.e2e
def test_the_query_overlay_withholds_a_copys_snapshot(world):
    from app.services.query_service import QueryService

    async def check():
        async with async_session_maker() as db:
            q = await db.get(Query, world.copy["query"])
            step = await db.get(Step, world.copy["step"])
            schema = SimpleNamespace(data=step.data, code=step.code, viewer_result=None,
                                     snapshot_withheld=False, applied_params=None)
            out = await QueryService()._overlay_viewer_result(db, q, step, schema, world.user_id)
            assert out.snapshot_withheld and out.data == {} and out.code == ""

    _run(check())


@pytest.mark.e2e
def test_read_query_does_not_find_a_copy(world):
    from app.ai.tools.implementations.read_query import ReadQueryTool
    from app.models.organization import Organization

    async def check():
        async with async_session_maker() as db:
            report = await db.get(Report, world.report_id)
            org = await db.get(Organization, world.org_id)
            result = await ReadQueryTool()._resolve_by_query_id(db, report, org, world.copy["query"], True)
            assert result.error and "not found" in result.error.lower(), result
            assert MARKER not in str(result)

    _run(check())


@pytest.mark.e2e
@pytest.mark.parametrize("ref_type,key", [("query", "query"), ("visualization", "viz")])
def test_an_email_cannot_attach_a_copy(world, ref_type, key):
    from app.ai.tools.schemas.send_email import EmailAttachmentSpec
    from app.models.organization import Organization
    from app.services.email_send_service import EmailSendService

    async def check():
        async with async_session_maker() as db:
            org = await db.get(Organization, world.org_id)
            for report in (await db.get(Report, world.report_id), None):
                spec = EmailAttachmentSpec(ref_type=ref_type, ref_id=world.copy[key])
                res, attachment, _ = await EmailSendService().resolve_attachment(spec, db, report, org)
                assert attachment is None and not res.success, (report, res)

    _run(check())


@pytest.mark.e2e
def test_a_copy_cannot_be_loaded_for_a_conversation_rerun(world):
    from app.services.step_service import StepService

    async def check():
        async with async_session_maker() as db:
            with pytest.raises(ValueError):
                await StepService()._load_step_for_rerun(db, world.copy["step"])

    _run(check())


@pytest.mark.e2e
def test_a_rerun_never_picks_a_step_of_another_query_when_a_query_has_no_widget(world):
    """A copy whose default step is not set falls back to its own latest step.
    Looking up "the latest step of its widget" with no widget would match every
    widget-less step in the database, whatever its query or organization."""
    from app.services.report_service import ReportService

    lonely = _run(add_release_copy(artifact_id=world.artifact_id, organization_id=world.org_id,
                                   user_id=world.user_id, with_default_step=False))
    # Another widget-less step, created later, that must not be picked.
    _run(add_release_copy(artifact_id=world.artifact_id, organization_id=world.org_id, user_id=world.user_id))

    async def check():
        async with async_session_maker() as db:
            targets, unresolved = await ReportService()._rerun_target_steps(db, [lonely["query"]])
            assert [t[0] for t in targets] == [lonely["step"]] and unresolved == 0, targets
            await db.execute(Step.__table__.delete().where(Step.__table__.c.id == lonely["step"]))
            await db.commit()
            targets, unresolved = await ReportService()._rerun_target_steps(db, [lonely["query"]])
            assert targets == [] and unresolved == 1, targets

    _run(check())


@pytest.mark.e2e
def test_read_query_does_not_find_a_copy_by_its_visualization(world):
    from app.ai.tools.implementations.read_query import ReadQueryTool
    from app.models.organization import Organization

    async def check():
        async with async_session_maker() as db:
            org = await db.get(Organization, world.org_id)
            result = await ReadQueryTool()._resolve_by_viz_id(db, None, org, world.copy["viz"], True)
            assert result.error and "not found" in result.error.lower(), result

    _run(check())


@pytest.mark.e2e
def test_conversation_services_refuse_a_copy_before_writing_anything(world):
    """Over HTTP guard_route answers first; the services refuse on their own
    too, before the builder run writes parameters or a step, and before the
    viewer run returns a cached result."""
    from app.schemas.query_schema import QueryRunRequest
    from app.services.query_service import QueryService
    from app.services.viewer_result_store import store_viewer_result
    from tests.e2e.dashboards.release_copies import copy_rows

    svc = QueryService()
    before = _run(copy_rows(world.copy))

    async def check():
        async with async_session_maker() as db:
            await store_viewer_result(db, step_id=world.copy["step"], user_id=world.user_id,
                                      organization_id=world.org_id, report_id=None, params_fingerprint="",
                                      status="success", status_reason=None, data={"rows": [{"secret": MARKER}]},
                                      executed_as="viewer", applied_params=None, last_run_at=None)
        calls = [
            lambda db: svc.run_existing_step(db, world.copy["step"], organization_id=world.org_id),
            lambda db: svc.run_query_new_step(db, world.copy["query"], QueryRunRequest(
                code="def generate_df(ds_clients, excel_files):\n    return None\n", parameters=[]),
                organization_id=world.org_id, user_id=world.user_id),
            lambda db: svc.preview_query_code(db, world.copy["query"], QueryRunRequest(
                code="def generate_df(ds_clients, excel_files):\n    return None\n"),
                organization_id=world.org_id, user_id=world.user_id),
            lambda db: svc.run_query_viewer(db, world.copy["query"], QueryRunRequest(),
                                            organization_id=world.org_id, user_id=world.user_id),
        ]
        for call in calls:
            async with async_session_maker() as db:
                with pytest.raises(ValueError):
                    await call(db)
        async with async_session_maker() as db:
            steps = (await db.execute(select(Step.id).where(Step.query_id == world.copy["query"]))).all()
            assert [str(s[0]) for s in steps] == [world.copy["step"]], "a step was created on a copy"

    _run(check())
    assert _run(copy_rows(world.copy)) == before


@pytest.mark.e2e
def test_the_entity_bow_client_refuses_a_copy(world):
    from app.errors import AppError
    from app.models.organization import Organization
    from app.services.bow_source_access import install_entity_client

    async def check():
        async with async_session_maker() as db:
            org = await db.get(Organization, world.org_id)
            user = await db.get(User, world.user_id)
            entity = SimpleNamespace(source_step_id=world.copy["step"],
                                     bow_source_access={"organization_id": world.org_id, "scope_ids": None})
            with pytest.raises(AppError) as refused:
                await install_entity_client(db, org, user, entity, {})
            assert refused.value.status_code == 403

    _run(check())


@pytest.mark.e2e
def test_the_credential_note_is_not_computed_for_a_copy(world):
    from app.services.query_service import QueryService

    async def check():
        async with async_session_maker() as db:
            q = await db.get(Query, world.copy["query"])
            schema = SimpleNamespace(credential_scoped=None)
            out = await QueryService().annotate_credential_scope(db, q, schema)
            assert out.credential_scoped is None

    _run(check())


@pytest.mark.e2e
def test_the_agent_cannot_run_or_parameterize_a_copy(world):
    """run_query and add_parameter look a query up by id even with no
    conversation in hand; a copy is never found."""
    from app.ai.tools.implementations.add_parameter import AddParameterTool
    from app.ai.tools.implementations.run_query import RunQueryTool
    from app.models.organization import Organization
    from tests.e2e.dashboards.release_copies import copy_rows

    async def check():
        async with async_session_maker() as db:
            org = await db.get(Organization, world.org_id)
            user = await db.get(User, world.user_id)
            tool = RunQueryTool()
            assert await tool._resolve_target(db, org, None, world.copy["query"], prefer_viz=False) == (None, None)
            assert await tool._resolve_target(db, org, None, world.copy["viz"], prefer_viz=True) == (None, None)
            events = [e async for e in AddParameterTool().run_stream(
                {"query_id": world.copy["query"], "parameter": {"name": "region"}, "column": "secret"},
                {"db": db, "report": None, "user": user, "organization": org})]
            output = events[-1].payload["output"]
            assert output["success"] is False and "not found" in (output.get("error") or "").lower(), output
            assert MARKER not in str(events)

    before = _run(copy_rows(world.copy))
    _run(check())
    assert _run(copy_rows(world.copy)) == before


@pytest.mark.e2e
def test_ids_that_do_not_exist_answer_as_before(world, test_client):
    """The copy refusal in guard_route applies to rows that exist without a
    conversation; an id with no row still gets the route's own answer."""
    import uuid

    missing = str(uuid.uuid4())
    step = test_client.get(f"/api/steps/{missing}", headers=world.headers)
    assert step.status_code == 404 and "report.not_found" in step.text, step.text
    entity = test_client.post(f"/api/entities/from_step/{missing}", json={}, headers=world.headers)
    assert entity.status_code == 400, entity.text
