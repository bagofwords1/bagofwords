"""Unit tests for the post-registration welcome email."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services import welcome_email as mod


@pytest.mark.asyncio
async def test_noop_when_smtp_not_configured():
    """No SMTP → no send, no raise (registration must never be blocked)."""
    with patch("app.settings.config.settings") as settings, \
         patch("app.services.notification_service.notification_service.send_custom_email", new=AsyncMock()) as send:
        settings.email_client = None
        await mod.send_welcome_email("user-1")
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_swallows_errors(monkeypatch):
    """Any internal failure is swallowed — never propagates into registration."""
    # email_client present so we get past the early return, but session maker blows up.
    import app.settings.config as cfg
    monkeypatch.setattr(cfg.settings, "email_client", object(), raising=False)
    with patch("app.dependencies.async_session_maker", side_effect=RuntimeError("boom")):
        # Should not raise.
        await mod.send_welcome_email("user-1")
