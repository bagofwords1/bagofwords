"""Notify Tool - deliver a free-form message to people in the organization.

The channel-agnostic successor to send_email. Resolves recipients against the
org's memberships (the requesting user is always included), then for each one
delivers an in-app notification plus one external nudge — the recipient's
verified Teams/Slack DM when reachable, otherwise email. Recipients, channel
selection, and attachment scoping are all handled by ``NotifyService`` so the
security boundary lives in exactly one place.

Use send_email for the narrow "email myself" case; use notify whenever the user
wants to reach other people or wants the message in their in-app inbox.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.notify import (
    NotifyInput,
    NotifyOutput,
    NotifyRecipientResult,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class NotifyTool(Tool):
    """Notify org members (in-app + one external channel) with a free-form message."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="notify",
            description=(
                "ACTION: Notify people in the user's organization with a free-form "
                "message. Each recipient gets an in-app notification and, when "
                "reachable, ONE external nudge (their verified Teams/Slack DM, "
                "otherwise email). The requesting user is ALWAYS notified too.\n\n"
                "When to use: the user asks to notify, ping, message, or email "
                "someone — 'let Alice know when this is ready', 'email the finance "
                "team the summary', 'send me and Bob the results'. To notify only "
                "yourself, leave 'recipients' empty (this replaces emailing yourself).\n\n"
                "Recipients are EMAIL ADDRESSES that must belong to members (or "
                "pending invites) of this organization — outside addresses are "
                "rejected. Use an address the user gave you or one already in "
                "context; don't guess. You don't choose channels — that's resolved "
                "per recipient automatically.\n\n"
                "Writing it — like a person, not a marketing system: short, natural, "
                "plain text by default. Use body_format='html' only when a few bullets "
                "or a small table genuinely help; keep the HTML simple.\n\n"
                "Attachments (optional, up to 5): reference a visualization_id / "
                "query_id (CSV/XLSX), artifact_id (PPTX/PDF), or file_id from this "
                "report. They ride the external channel; in-app recipients open the "
                "report to view them."
            ),
            category="action",
            version="1.0.0",
            input_schema=NotifyInput.model_json_schema(),
            output_schema=NotifyOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=False,
            # Always advertised: even when no email transport is configured, the
            # in-app channel still delivers — so unlike send_email this tool is not
            # filtered out by email availability.
            is_active=True,
            required_permissions=[],
            tags=["notification", "email", "in_app", "action"],
            examples=[
                {
                    "input": {
                        "subject": "Q2 revenue is final",
                        "body": "Q2 closed at $1.24M, up 14% QoQ.",
                        "recipients": ["alice@acme.com"],
                    },
                    "description": "Notify a teammate (and yourself) about a result.",
                },
                {
                    "input": {
                        "subject": "Your weekly digest",
                        "body": "No anomalies this week — all clear.",
                    },
                    "description": "Notify only yourself (empty recipients).",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return NotifyInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return NotifyOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = NotifyInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {str(e)}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"subject": data.subject, "recipient_count": len(data.recipients) + 1},
        )

        db = runtime_ctx.get("db")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        report = runtime_ctx.get("report")

        if not db or not user or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": NotifyOutput(
                        success=False,
                        subject=data.subject,
                        error="Notifications require an active user/organization context.",
                    ).model_dump(),
                    "observation": {
                        "summary": "Could not notify: missing user/organization context.",
                        "success": False,
                        "artifacts": [],
                    },
                },
            )
            return

        try:
            from app.services.notify_service import notify_service

            result = await notify_service.notify(
                db,
                sender=user,
                organization=organization,
                report=report,
                subject=data.subject,
                body=data.body,
                body_format=data.body_format,
                attachment_specs=data.attachments,
                recipient_emails=data.recipients,
                system_completion=runtime_ctx.get("system_completion"),
            )
        except Exception as e:
            logger.exception("notify failed: %s", e)
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to notify: {str(e)}", "code": "NOTIFY_FAILED"},
            )
            return

        output = NotifyOutput(
            success=result["success"],
            subject=data.subject,
            results=[NotifyRecipientResult(**r) for r in result["results"]],
            rejected=result["rejected"],
        )

        reached = sum(1 for r in result["results"] if r["delivered"])
        summary = (
            f"Notified {reached} recipient(s): {data.subject}"
            if result["success"]
            else "Notification could not be delivered."
        )
        if result["rejected"]:
            summary += f" (skipped non-members: {', '.join(result['rejected'])})"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {"summary": summary, "success": result["success"], "artifacts": []},
            },
        )
