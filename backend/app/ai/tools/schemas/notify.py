from typing import Optional, Literal, List
from pydantic import BaseModel, Field

# Reuse the exact attachment contract from send_email — same report-scoped refs.
from app.ai.tools.schemas.send_email import (
    EmailAttachmentSpec,
    SendEmailAttachmentResult,  # noqa: F401  (re-exported for convenience)
)


class NotifyInput(BaseModel):
    """Input schema for the notify tool.

    Delivers a free-form message to people in the user's organization. Each
    recipient gets an in-app notification AND, when reachable, one external nudge
    (their verified Teams/Slack, otherwise email). The requesting user is ALWAYS
    notified too, so leave ``recipients`` empty to just notify yourself.
    """

    recipients: List[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Email addresses of the ORG MEMBERS to notify. Each must belong to a "
            "member (or pending invite) of the current organization — arbitrary "
            "outside addresses are rejected. You (the requesting user) are always "
            "included automatically, so leave this empty to notify only yourself. "
            "Use the address the user gave you or one already visible in context; "
            "do not guess."
        ),
    )
    subject: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="A clear, specific subject line (also the notification title).",
    )
    body: str = Field(
        ...,
        min_length=1,
        description=(
            "The message body. Write it like a person would — short, natural, direct. "
            "Plain text by default. If you set body_format='html', keep the HTML simple "
            "(basic tags like <p>, <ul>/<li>, <strong>, small <table>); avoid heavy "
            "templated layouts, inline CSS, banners, or branded headers/footers."
        ),
    )
    body_format: Literal["text", "html"] = Field(
        default="text",
        description="'text' (default, preferred) or 'html' for light structure.",
    )
    attachments: List[EmailAttachmentSpec] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Optional files to attach (max 5), referenced by visualization_id / query_id "
            "/ artifact_id / file_id from this report. Sent on the external channel "
            "(email / Teams / Slack); in-app recipients open the report to view them."
        ),
    )


class NotifyRecipientResult(BaseModel):
    """Per-recipient delivery outcome."""

    email: str
    name: Optional[str] = None
    is_self: bool = False
    delivered: List[str] = Field(default_factory=list, description="Channels that succeeded, e.g. ['in_app','email'].")
    failed: List[str] = Field(default_factory=list, description="Channels that were attempted but failed.")
    error: Optional[str] = None


class NotifyOutput(BaseModel):
    """Output schema for the notify tool."""

    success: bool = Field(..., description="True if at least one recipient was reached on at least one channel.")
    subject: Optional[str] = None
    results: List[NotifyRecipientResult] = Field(default_factory=list)
    rejected: List[str] = Field(
        default_factory=list,
        description="Requested addresses that are not members of this org and were not notified.",
    )
    error: Optional[str] = None
