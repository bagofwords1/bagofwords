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
        description="A clear, specific subject line. Don't just restate it in the body.",
    )
    body: str = Field(
        ...,
        min_length=1,
        description=(
            "The email body. Write it like a person would — short, natural, and direct. "
            "Plain text by default. If you set body_format='html', keep the HTML simple and "
            "human-looking (basic tags like <p>, <ul>/<li>, <strong>, small <table>); avoid "
            "heavy templated layouts, inline CSS, wrapper divs, banners, or branded "
            "headers/footers."
        ),
    )
    body_format: Literal["text", "html"] = Field(
        default="text",
        description=(
            "Body format: 'text' for plain text (default, preferred) or 'html' for simple "
            "HTML. Only choose 'html' when light structure (a few bullets or a small table) "
            "genuinely helps readability."
        ),
    )


class SendEmailOutput(BaseModel):
    """Output schema for the send_email tool."""

    success: bool = Field(..., description="Whether the email was sent to the SMTP server successfully.")
    recipient: Optional[str] = Field(default=None, description="The email address the message was sent to.")
    subject: Optional[str] = Field(default=None, description="The subject line that was sent.")
    error: Optional[str] = Field(default=None, description="Error message if sending failed.")
