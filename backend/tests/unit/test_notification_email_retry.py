"""Unit tests for NotificationService.send_custom_email reliability knobs.

Covers the hardening used by the membership-invite email:
  - awaited send returns a truthful status,
  - bounded retries on transient SMTP failures,
  - per-attempt timeout so a hung relay can't block the caller,
  - no-op/"failed" when SMTP isn't configured.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.notification_service import notification_service
from app.services import notification_service as mod


@pytest.fixture
def restore_email_client():
    saved = mod.settings.email_client
    try:
        yield
    finally:
        mod.settings.email_client = saved


@pytest.mark.asyncio
async def test_returns_failed_when_smtp_not_configured(restore_email_client):
    mod.settings.email_client = None
    result = await notification_service.send_custom_email(
        recipients=["a@b.com"], subject="s", body="b",
    )
    assert result.status == "failed"
    assert "SMTP" in (result.error or "")


@pytest.mark.asyncio
async def test_sends_on_first_try(restore_email_client):
    fm = MagicMock()
    fm.send_message = AsyncMock(return_value=None)
    mod.settings.email_client = fm
    result = await notification_service.send_custom_email(
        recipients=["a@b.com"], subject="s", body="b", retries=2, retry_delay=0,
    )
    assert result.status == "sent"
    assert fm.send_message.await_count == 1


@pytest.mark.asyncio
async def test_retries_then_succeeds(restore_email_client):
    fm = MagicMock()
    # Fail twice, then succeed on the third attempt.
    fm.send_message = AsyncMock(side_effect=[RuntimeError("temp"), RuntimeError("temp"), None])
    mod.settings.email_client = fm
    result = await notification_service.send_custom_email(
        recipients=["a@b.com"], subject="s", body="b", retries=2, retry_delay=0,
    )
    assert result.status == "sent"
    assert fm.send_message.await_count == 3


@pytest.mark.asyncio
async def test_exhausts_retries_and_reports_failure(restore_email_client):
    fm = MagicMock()
    fm.send_message = AsyncMock(side_effect=RuntimeError("smtp down"))
    mod.settings.email_client = fm
    result = await notification_service.send_custom_email(
        recipients=["a@b.com"], subject="s", body="b", retries=2, retry_delay=0,
    )
    assert result.status == "failed"
    assert result.error == "smtp down"
    assert fm.send_message.await_count == 3  # 1 + 2 retries


@pytest.mark.asyncio
async def test_per_attempt_timeout_counts_as_failure(restore_email_client):
    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)

    fm = MagicMock()
    fm.send_message = AsyncMock(side_effect=_hang)
    mod.settings.email_client = fm
    result = await notification_service.send_custom_email(
        recipients=["a@b.com"], subject="s", body="b",
        retries=1, retry_delay=0, timeout=0.05,
    )
    assert result.status == "failed"
    assert fm.send_message.await_count == 2  # timed out twice
