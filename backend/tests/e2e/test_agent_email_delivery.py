"""One agent execution may attempt email delivery to each recipient once."""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.email_delivery_claim import EmailDeliveryClaim
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.services.email_send_service import EmailSendService


async def _execution_ids():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Email claim org {suffix}")
        db.add(org)
        await db.flush()
        user = User(
            name="Email claim user", email=f"owner-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()
        report = Report(
            title=f"Email claim report {suffix}", slug=f"email-claim-{suffix}",
            status="draft", user_id=user.id, organization_id=org.id,
        )
        db.add(report)
        await db.flush()
        executions = []
        for _ in range(2):
            completion = Completion(status="success", role="system", report_id=report.id)
            db.add(completion)
            await db.flush()
            execution = AgentExecution(completion_id=completion.id, report_id=report.id)
            db.add(execution)
            await db.flush()
            executions.append(execution.id)
        await db.commit()
        return executions


@pytest.mark.asyncio
async def test_one_email_per_recipient_per_execution_even_when_content_changes(monkeypatch):
    first_run, next_run = await _execution_ids()
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs["recipients"][0])
        return SimpleNamespace(status="sent", error=None)

    from app.services.notification_service import notification_service
    monkeypatch.setattr(notification_service, "send_custom_email", fake_send)

    async def send(run, recipient, body):
        # A new session models another tool action or worker resuming the run.
        async with async_session_maker() as db:
            return await EmailSendService().send(
                db, recipient=recipient, subject="Daily report", body=body,
                agent_execution_id=run,
            )

    assert (await send(first_run, "person@example.com", "Version one")).success
    duplicate = await send(first_run, "PERSON@example.com", "Version two")
    assert not duplicate.success
    assert (await send(first_run, "colleague@example.com", "Another recipient")).success
    assert (await send(next_run, "person@example.com", "Next scheduled run")).success
    assert sent == ["person@example.com", "colleague@example.com", "person@example.com"]

    async with async_session_maker() as db:
        assert (await db.scalar(select(func.count()).select_from(EmailDeliveryClaim))) == 3


@pytest.mark.asyncio
async def test_uncertain_provider_failure_does_not_retry_same_run(monkeypatch):
    run, _ = await _execution_ids()
    attempts = 0

    async def uncertain_send(**kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("provider response lost after handoff")

    from app.services.notification_service import notification_service
    monkeypatch.setattr(notification_service, "send_custom_email", uncertain_send)

    async with async_session_maker() as db:
        with pytest.raises(TimeoutError):
            await EmailSendService().send(
                db, recipient="person@example.com", subject="Daily report",
                body="Summary", agent_execution_id=run,
            )

    async with async_session_maker() as db:
        retry = await EmailSendService().send(
            db, recipient="person@example.com", subject="Daily report",
            body="Summary", agent_execution_id=run,
        )
    assert not retry.success
    assert attempts == 1
