"""MCP Tool: send_email - email the requesting user a summary or export.

The recipient is ALWAYS the authenticated token user — there is no recipient
argument — so an external MCP client can only ever email that user their own
data, never a third party. Attachments are scoped to a report the user's org
owns (verified here before resolution), mirroring the internal send_email tool.

Hidden from ``tools/list`` when SMTP isn't configured (``is_available``), so we
never advertise a tool that can only fail.
"""

import logging
from typing import Dict, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.user import User
from app.models.organization import Organization
from app.schemas.mcp import MCPSendEmailInput, MCPSendEmailOutput
from app.settings.config import settings

logger = logging.getLogger(__name__)


class SendEmailMCPTool(MCPTool):
    """Send a free-form email (optionally with report-scoped attachments) to the
    authenticated user's own inbox."""

    name = "send_email"
    description = (
        "Send an email to the current user (yourself), and optionally to other members "
        "of your organization via 'recipients' (their email addresses; outside addresses "
        "are rejected). You are always included, and every recipient also gets an in-app "
        "notification. Use it when the user asks to be emailed — or to notify a teammate — "
        "something (a summary, a result, an export). Keep the body short and natural; "
        "default to plain text. Attachments (optional, up to 5) are generated from objects "
        "in a report — reference a visualization_id / query_id (CSV/XLSX), artifact_id "
        "(PPTX/PDF), or file_id, and pass the owning report_id."
    )

    # Availability is decided per-org at request time (see is_available_for_org),
    # not by the startup-global is_available, so orgs that configured SMTP via the
    # UI (org SMTP) are covered even when the global bow-config SMTP is empty.
    @property
    def is_available(self) -> bool:
        return True

    async def is_available_for_org(self, db, organization) -> bool:
        """Whether outbound email resolves for this org (AI mailbox / org SMTP /
        global), mirroring the analyst send path."""
        org_id = getattr(organization, "id", None)
        if not db or not org_id:
            return settings.email_client is not None
        from app.services.email_client_resolver import is_outbound_available
        return await is_outbound_available(db, str(org_id), purpose="analyst")

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPSendEmailInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        try:
            input_data = MCPSendEmailInput(**args)
        except Exception as e:
            return MCPSendEmailOutput(success=False, error=f"Invalid input: {e}").model_dump()

        if not await self.is_available_for_org(db, organization):
            return MCPSendEmailOutput(
                success=False, subject=input_data.subject,
                error="Email is not configured for this organization.",
            ).model_dump()

        recipient = getattr(user, "email", None)
        if not recipient:
            return MCPSendEmailOutput(
                success=False, subject=input_data.subject,
                error="Could not resolve your email address.",
            ).model_dump()

        # Attachments are scoped to a report. Require report_id and verify the
        # report belongs to the caller's org before trusting it for scoping —
        # _load_report alone does not check ownership.
        report = None
        if input_data.attachments:
            if not input_data.report_id:
                return MCPSendEmailOutput(
                    success=False, subject=input_data.subject,
                    error="report_id is required when sending attachments.",
                ).model_dump()
            try:
                report = await self._load_report(db, input_data.report_id)
            except Exception:
                return MCPSendEmailOutput(
                    success=False, subject=input_data.subject,
                    error="Report not found.",
                ).model_dump()
            if str(report.organization_id) != str(organization.id):
                return MCPSendEmailOutput(
                    success=False, subject=input_data.subject,
                    error="Report not found.",
                ).model_dump()
        elif input_data.report_id:
            # No attachments but a report was named — used only for the in-app
            # deep link; still verify org ownership before trusting it.
            try:
                report = await self._load_report(db, input_data.report_id)
                if str(report.organization_id) != str(organization.id):
                    report = None
            except Exception:
                report = None

        # Resolve recipients (self always included; members validated; outsiders
        # rejected) and fan out across in-app + email/chat via NotifyService —
        # the same path the internal notify tool uses.
        try:
            from app.services.notify_service import notify_service

            result = await notify_service.notify(
                db,
                sender=user,
                organization=organization,
                report=report,
                subject=input_data.subject,
                body=input_data.body,
                body_format=input_data.body_format,
                attachment_specs=input_data.attachments,
                recipient_emails=input_data.recipients,
            )
        except Exception as e:
            logger.exception("MCP send_email failed")
            return MCPSendEmailOutput(
                success=False, recipient=recipient, subject=input_data.subject,
                error=f"Failed to send email: {e}",
            ).model_dump()

        reached = [r["email"] for r in result["results"] if r["delivered"]]
        return MCPSendEmailOutput(
            success=result["success"],
            recipient=recipient,
            recipients=reached,
            subject=input_data.subject,
            rejected=result["rejected"],
            error=None if result["success"] else "Notification could not be delivered.",
        ).model_dump()
