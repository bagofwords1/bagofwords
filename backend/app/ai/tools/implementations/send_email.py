"""Send Email Tool - sends a free-form email to the requesting user.

The recipient is always the current user (resolved from runtime context), so the
agent cannot email anyone else. Subject and body are free-form. This is the first
of a planned family of notification tools (email today; Slack / WhatsApp / Teams
in the future) — each channel gets its own tool so the UI status stays per-channel.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.send_email import SendEmailInput, SendEmailOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.settings.config import settings

logger = logging.getLogger(__name__)


class SendEmailTool(Tool):
    """Send a free-form email to the current user's own inbox."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="send_email",
            description=(
                "ACTION: Send an email to the current user (yourself). The recipient is "
                "ALWAYS the requesting user — you cannot send to anyone else, so there is "
                "no recipient argument.\n\n"
                "When to use: the user explicitly asks to be emailed something (a summary, "
                "a result, a reminder, a list of findings), or asks to 'send me' / 'email me' "
                "the answer. Do NOT use it to deliver every response — only when the user "
                "wants the content in their inbox.\n\n"
                "Writing the email — write like a person, not a marketing system:\n"
                "- Default to plain text (body_format='text'). Keep it short, natural, and "
                "to the point, the way a colleague would write a quick email.\n"
                "- Use body_format='html' ONLY when light structure genuinely helps (a few "
                "bullet points or a small table). Even then, keep the HTML simple and "
                "human-looking — basic tags like <p>, <ul>/<li>, <strong>, <table>. Do NOT "
                "build heavy templated layouts, inline CSS styling, wrapper divs, banners, "
                "or branded headers/footers.\n"
                "- Write a clear, specific subject line. Put the real content in the body; "
                "don't just restate the subject."
            ),
            category="action",
            version="1.0.0",
            input_schema=SendEmailInput.model_json_schema(),
            output_schema=SendEmailOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            # Hidden from the catalog when SMTP isn't configured. Evaluated at
            # registry startup, where settings.email_client is already resolved.
            is_active=settings.email_client is not None,
            required_permissions=[],
            tags=["notification", "email", "action"],
            examples=[
                {
                    "input": {
                        "subject": "Your revenue summary",
                        "body": "Q2 revenue was $1.2M, up 14% QoQ.",
                    },
                    "description": "Send a plain-text summary to yourself.",
                },
                {
                    "input": {
                        "subject": "Weekly report",
                        "body": "<h1>Weekly report</h1><p>All metrics nominal.</p>",
                        "body_format": "html",
                    },
                    "description": "Send an HTML-formatted email to yourself.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SendEmailInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SendEmailOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SendEmailInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {str(e)}", "code": "INVALID_INPUT"},
            )
            return

        # Recipient is always the requesting user — never caller-controllable.
        user = runtime_ctx.get("user")
        recipient = getattr(user, "email", None) if user else None

        yield ToolStartEvent(
            type="tool.start",
            payload={"subject": data.subject, "recipient": recipient},
        )

        if not recipient:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": SendEmailOutput(
                        success=False,
                        subject=data.subject,
                        error="Could not resolve your email address.",
                    ).model_dump(),
                    "observation": {
                        "summary": "Email not sent: no recipient address available for the current user.",
                        "success": False,
                        "artifacts": [],
                    },
                },
            )
            return

        try:
            from app.services.notification_service import notification_service

            subtype = "html" if data.body_format == "html" else "plain"
            result = await notification_service.send_custom_email(
                recipients=[recipient],
                subject=data.subject,
                body=data.body,
                subtype=subtype,
            )

            success = result.status == "sent"
            summary = (
                f"Email sent to {recipient}: {data.subject}"
                if success
                else f"Failed to send email: {result.error or 'unknown error'}"
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": SendEmailOutput(
                        success=success,
                        recipient=recipient,
                        subject=data.subject,
                        error=None if success else (result.error or "Failed to send email"),
                    ).model_dump(),
                    "observation": {
                        "summary": summary,
                        "success": success,
                        "artifacts": [],
                    },
                },
            )
        except Exception as e:
            logger.exception("Failed to send email: %s", e)
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to send email: {str(e)}", "code": "SEND_FAILED"},
            )
