from typing import Optional, Literal, List
from pydantic import BaseModel, Field


class EmailAttachmentSpec(BaseModel):
    """A single file to attach to an email.

    The file is generated on the fly from something the assistant already
    produced in this report — a visualization/query result (exported as a
    spreadsheet), an artifact (exported as a slide deck or PDF), or an
    uploaded file (attached as-is). Reference it by the id you can see in the
    conversation context (visualization_id / query_id / artifact_id / file_id).
    """

    ref_type: Literal["visualization", "query", "artifact", "file"] = Field(
        ...,
        description=(
            "What kind of object to attach: 'visualization' or 'query' (attaches the "
            "underlying query result as a CSV/XLSX), 'artifact' (attaches a slides deck "
            "as PPTX or a page dashboard as PDF), or 'file' (attaches an uploaded file "
            "as-is). Note: steps are not directly attachable — use the visualization."
        ),
    )
    ref_id: str = Field(
        ...,
        description=(
            "The id of the object to attach, taken from the conversation context: a "
            "visualization_id, query_id, artifact_id, or file_id."
        ),
    )
    format: Optional[Literal["csv", "xlsx", "pptx", "pdf"]] = Field(
        default=None,
        description=(
            "Optional output format. Defaults per ref_type: visualization/query -> 'csv' "
            "(may also be 'xlsx'); artifact -> 'pptx' for slides, 'pdf' for page. Ignored "
            "for 'file' (attached in its original format)."
        ),
    )
    filename: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Optional filename override (extension is added automatically if missing).",
    )


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
    attachments: List[EmailAttachmentSpec] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Optional files to attach (max 5). Use this when the user asks you to email them "
            "a result, dashboard, or export — reference the visualization_id / query_id / "
            "artifact_id / file_id you can see in context. Leave empty for a plain message."
        ),
    )


class SendEmailAttachmentResult(BaseModel):
    """Per-attachment outcome reported back to the assistant."""

    ref_type: str
    ref_id: str
    filename: Optional[str] = None
    success: bool = False
    error: Optional[str] = None


class SendEmailOutput(BaseModel):
    """Output schema for the send_email tool."""

    success: bool = Field(..., description="Whether the email was sent to the SMTP server successfully.")
    recipient: Optional[str] = Field(default=None, description="The email address the message was sent to.")
    subject: Optional[str] = Field(default=None, description="The subject line that was sent.")
    attachments: List[SendEmailAttachmentResult] = Field(
        default_factory=list,
        description="Outcome for each requested attachment (which were included, which failed).",
    )
    error: Optional[str] = Field(default=None, description="Error message if sending failed.")
