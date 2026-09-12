"""Invites must go out over the organization's own SMTP.

The invite path used to gate on ``settings.email_client`` — the *global*
bow-config client, whose ``smtp_settings`` block ships commented out — and to
call ``send_custom_email`` without any org context. So an organization that
configured SMTP through Settings → SMTP got two independent failures: the guard
said "no SMTP" and skipped, and even past the guard the send would have gone to
the empty global client. Both halves are covered here.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.organization_service import OrganizationService


@pytest.mark.asyncio
async def test_invite_send_passes_org_context_to_the_mailer():
    """Without db + organization_id, send_custom_email skips the resolver and
    goes straight to the global client — the org's SMTP is never consulted."""
    svc = OrganizationService()
    db = MagicMock()

    sent = AsyncMock(return_value=SimpleNamespace(status="sent", error=None))
    with patch("app.services.notification_service.notification_service.send_custom_email", sent):
        status = await svc._send_invitation_email(
            "new@example.com", "tok-123", db=db, organization_id="org-1",
        )

    assert status == "sent"
    kwargs = sent.await_args.kwargs
    assert kwargs["db"] is db
    assert kwargs["organization_id"] == "org-1"
    # "system" purpose is org SMTP → global; never the AI mailbox.
    assert kwargs["purpose"] == "system"


@pytest.mark.asyncio
async def test_resend_uses_org_smtp_when_global_is_empty():
    """The regression, at the guard: org SMTP configured, global bow-config
    empty. The invite must still be sent, not reported as skipped_no_smtp."""
    svc = OrganizationService()
    membership = SimpleNamespace(
        id="m1", user_id=None, email="new@example.com",
        invite_token="old", invite_expires_at=None,
    )
    svc.get_member = AsyncMock(return_value=membership)
    svc._send_invitation_email = AsyncMock(return_value="sent")

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one=MagicMock(return_value=membership))
    )

    with patch("app.services.email_client_resolver.is_outbound_available",
               AsyncMock(return_value=True)) as avail, \
         patch("app.schemas.organization_schema.MembershipSchema.from_orm",
               MagicMock(return_value=SimpleNamespace(invite_email_status=None))):
        schema = await svc.resend_invite(db, "m1", "org-1")

    assert schema.invite_email_status == "sent"
    assert avail.await_args.kwargs["purpose"] == "system"
    svc._send_invitation_email.assert_awaited_once()
    assert svc._send_invitation_email.await_args.kwargs["organization_id"] == "org-1"


@pytest.mark.asyncio
async def test_resend_still_skips_when_no_transport_resolves():
    """No AI mailbox, no org SMTP, no global → honestly report the skip so the
    UI can warn instead of showing a green success."""
    svc = OrganizationService()
    membership = SimpleNamespace(
        id="m1", user_id=None, email="new@example.com",
        invite_token="old", invite_expires_at=None,
    )
    svc.get_member = AsyncMock(return_value=membership)
    svc._send_invitation_email = AsyncMock(return_value="sent")

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one=MagicMock(return_value=membership))
    )

    with patch("app.services.email_client_resolver.is_outbound_available",
               AsyncMock(return_value=False)), \
         patch("app.schemas.organization_schema.MembershipSchema.from_orm",
               MagicMock(return_value=SimpleNamespace(invite_email_status=None))):
        schema = await svc.resend_invite(db, "m1", "org-1")

    assert schema.invite_email_status == "skipped_no_smtp"
    svc._send_invitation_email.assert_not_awaited()
