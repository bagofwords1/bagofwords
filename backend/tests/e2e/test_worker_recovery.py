"""A dead worker cannot permanently hold an agent or an evaluation run busy."""

from datetime import datetime, timedelta
import uuid
import pytest
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.agent_automation_run import AgentAutomationRun
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.eval import TestRun, TestResult
from app.models.completion import Completion
from app.models.review_item import ReviewItem
from app.services.worker_recovery import recover_rows


@pytest.fixture
def recovery_context(
    test_client,
    create_user,
    login_user,
    whoami,
    create_report,
    create_test_suite,
    create_test_case,
    create_data_source,
    dynamic_sqlite_db,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org = me["organizations"][0]["id"]
    auth = dict(user_token=token, org_id=org)
    report = create_report(**auth)
    suite = create_test_suite(name="Recovery suite", **auth)
    case = create_test_case(suite_id=suite["id"], name="Recovery case", **auth)
    agent = create_data_source(name="Recovery agent", type="sqlite", config={"database": dynamic_sqlite_db}, **auth)
    return dict(org=org, report=report["id"], case=case["id"], suite=suite["id"], agent=agent["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("age_minutes,expired", [(2, False), (60, True), (1440, True)])
async def test_recovery_preserves_live_jobs_and_retires_orphans(recovery_context, age_minutes, expired):
    c = recovery_context
    now = datetime.utcnow()
    last = now - timedelta(minutes=age_minutes)
    # Raw writes represent process death, a state no HTTP endpoint can create.
    async with async_session_maker() as db:
        automation = AgentAutomationRun(
            organization_id=c["org"],
            data_source_id=c["agent"],
            trigger="manual",
            status="running",
            started_at=last,
            updated_at=last,
        )
        metadata = MetadataIndexingJob(organization_id=c["org"], status="running", started_at=last, updated_at=last)
        run = TestRun(title="Orphan", suite_ids=c["suite"], status="in_progress", started_at=last, updated_at=last)
        head = Completion(report_id=c["report"], role="user", status="success", model="test", turn_index=0)
        review = ReviewItem(
            organization_id=c["org"],
            data_source_id=c["agent"],
            type="test",
            title="Action",
            spawned_json=[{"kind": "train", "status": "running"}],
            status="in_progress",
            updated_at=last,
        )
        db.add_all([automation, metadata, run, head, review])
        await db.flush()
        result = TestResult(run_id=run.id, case_id=c["case"], head_completion_id=head.id, status="in_progress")
        passed = TestResult(run_id=run.id, case_id=c["case"], head_completion_id=head.id, status="pass")
        db.add_all([result, passed])
        await db.commit()
        ids = [(type(x), x.id) for x in [automation, metadata, run, result, review, passed]]
    async with async_session_maker() as db:
        await recover_rows(db, now=now)
        await db.commit()
    async with async_session_maker() as db:
        rows = [await db.get(model, key) for model, key in ids]
        assert [r.status for r in rows] == (
            ["error", "failed", "error", "error", "open", "pass"]
            if expired
            else ["running", "running", "in_progress", "in_progress", "in_progress", "pass"]
        )
        if expired:
            assert rows[0].finished_at and rows[1].completed_at and rows[2].finished_at
            assert rows[4].spawned_json[0]["status"] == "error"


@pytest.mark.asyncio
async def test_recovered_eval_is_not_returned_as_in_progress(recovery_context):
    c = recovery_context
    async with async_session_maker() as db:
        # Orphan state has no API representation.
        run = TestRun(
            title="Lost", suite_ids=c["suite"], status="in_progress", updated_at=datetime.utcnow() - timedelta(days=2)
        )
        db.add(run)
        await db.commit()
        old_id = run.id
        await recover_rows(db)
        await db.commit()
        # Public status stays terminal even after a later read/recovery pass.
        await recover_rows(db)
        await db.commit()
        row = (
            await db.execute(select(TestRun).where(TestRun.id == old_id).execution_options(populate_existing=True))
        ).scalar_one()
        assert row.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["live", "expired", "replaced"])
async def test_agent_creation_is_fenced_by_delivery_ownership(recovery_context, ownership):
    import asyncio
    from app.models.background_job import BackgroundJob
    from app.services.background_jobs import enqueue, delivery_context, checkpoint_completion

    c = recovery_context
    owner = str(uuid.uuid4())
    completion_id = str(uuid.uuid4())
    async with async_session_maker() as db:
        job = await enqueue(db, key=str(uuid.uuid4()), kind="wait", payload={})
        # Simulate a claimed delivery or a claim replaced after process death.
        job.status = "running"
        job.owner = owner if ownership != "replaced" else str(uuid.uuid4())
        job.lease_until = datetime.utcnow() + timedelta(minutes=-1 if ownership == "expired" else 5)
        await db.commit()
        job_id = job.id
    with delivery_context(job_id, owner):
        async with async_session_maker() as db:
            completion = Completion(
                id=completion_id, report_id=c["report"], role="system", status="in_progress", model="test", turn_index=1
            )
            db.add(completion)
            if ownership == "live":
                await checkpoint_completion(db, completion)
                await db.commit()
                descendant = Completion(
                    report_id=c["report"],
                    role="system",
                    status="in_progress",
                    model="test",
                    turn_index=2,
                )
                db.add(descendant)
                await checkpoint_completion(db, descendant)
                await db.commit()
            else:
                with pytest.raises(asyncio.CancelledError):
                    await checkpoint_completion(db, completion)
    async with async_session_maker() as db:
        job = await db.get(BackgroundJob, job_id)
        assert bool(await db.get(Completion, completion_id)) == (ownership == "live")
        assert job.completion_id == (completion_id if ownership == "live" else None)


@pytest.mark.asyncio
async def test_agent_receipt_rolls_back_with_its_completion(recovery_context):
    from app.models.background_job import BackgroundJob
    from app.services.background_jobs import enqueue, delivery_context, checkpoint_completion

    c = recovery_context
    owner = str(uuid.uuid4())
    completion_id = str(uuid.uuid4())
    async with async_session_maker() as db:
        job = await enqueue(db, key=str(uuid.uuid4()), kind="wait", payload={})
        job.status = "running"
        job.owner = owner
        job.lease_until = datetime.utcnow() + timedelta(minutes=5)
        await db.commit()
        job_id = job.id
    with delivery_context(job_id, owner):
        async with async_session_maker() as db:
            completion = Completion(
                id=completion_id, report_id=c["report"], role="system", status="in_progress", model="test", turn_index=1
            )
            db.add(completion)
            await checkpoint_completion(db, completion)
            await db.rollback()
    async with async_session_maker() as db:
        assert await db.get(Completion, completion_id) is None
        assert (await db.get(BackgroundJob, job_id)).completion_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wave_status", ["running", "timed_out"])
async def test_deadline_delivery_finishes_children_after_partial_worker_loss(recovery_context, wave_status):
    from app.models.sub_agent import SubAgentWave, SubAgentRun
    from app.models.background_job import BackgroundJob
    from app.services.background_jobs import BackgroundWorker, enqueue

    c = recovery_context
    async with async_session_maker() as db:
        # A lost process may have committed timed_out before stopping children.
        wave = SubAgentWave(
            organization_id=c["org"],
            report_id=c["report"],
            status=wave_status,
            total=1,
            remaining=1,
            deadline_at=datetime.utcnow() - timedelta(minutes=1),
        )
        db.add(wave)
        await db.flush()
        child = SubAgentRun(
            wave_id=wave.id, organization_id=c["org"], report_id=c["report"], status="running", goal="Test"
        )
        db.add(child)
        await db.flush()
        job = await enqueue(
            db,
            key=f"deadline:{wave.id}",
            kind="wave_deadline",
            payload={"job_id": f"deadline:{wave.id}", "wave_id": str(wave.id)},
        )
        await db.commit()
        wave_id, child_id, job_id = wave.id, child.id, job.id
    await BackgroundWorker(async_session_maker).run_one()
    async with async_session_maker() as db:
        assert (await db.get(BackgroundJob, job_id)).status == "succeeded"
        assert (await db.get(SubAgentRun, child_id)).status == "timed_out"
        followup = (
            await db.execute(select(BackgroundJob).where(BackgroundJob.key == f"fan_in:{wave_id}"))
        ).scalar_one()
        assert followup.status == "pending"
