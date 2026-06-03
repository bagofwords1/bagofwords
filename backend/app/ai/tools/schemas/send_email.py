from typing import Optional, Literal
from pydantic import BaseModel, Field


class SendEmailInput(BaseModel):
    """Input schema for the send_email tool.

    Sends a free-form email to the requesting user themselves. The recipient is
    always the current user — it is not selectable here, so the agent cannot
    email anyone else. Use this to deliver a summary, reminder, or result to the
    user's inbox.
    """

    subject: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="The email subject line.",
    )
    body: str = Field(
        ...,
        min_length=1,
        description="The email body. Plain text by default; set body_format='html' to send HTML.",
    )
    body_format: Literal["text", "html"] = Field(
        default="text",
        description="Format of the body: 'text' for plain text (default) or 'html' for HTML content.",
    )


class SendEmailOutput(BaseModel):
    """Output schema for the send_email tool."""

    success: bool = Field(..., description="Whether the email was sent to the SMTP server successfully.")
    recipient: Optional[str] = Field(default=None, description="The email address the message was sent to.")
    subject: Optional[str] = Field(default=None, description="The subject line that was sent.")
    error: Optional[str] = Field(default=None, description="Error message if sending failed.")
