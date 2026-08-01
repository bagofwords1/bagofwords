"""Input/output schemas for the browser tools.

Five tools share one session model: `browser_navigate` opens (or reuses) the
report's browser session and returns a `session_id`; the other four take that id.
Refs (`e12`) come from a snapshot and are only valid until the next navigation or
DOM change — a stale ref returns a typed error telling the model to re-snapshot.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class _TitleMixin(BaseModel):
    title: Optional[str] = Field(
        default=None,
        description=(
            "Short human-readable label for this action, active voice, 3-6 words "
            "(e.g. 'Opening the vendor portal'). Shown as the live status line."
        ),
    )


class BrowserNavigateInput(_TitleMixin):
    url: str = Field(..., description="The http(s) URL to open. Must match the connection's allowlist.")
    session_id: Optional[str] = Field(
        default=None,
        description="Existing browser session to reuse. Omit to open a new session for this report.",
    )


class BrowserSnapshotInput(_TitleMixin):
    session_id: str = Field(..., description="The browser session to snapshot (from browser_navigate).")
    full: bool = Field(
        default=False,
        description="Include non-interactive containers too. Default false keeps the tree small.",
    )
    max_chars: int = Field(
        default=8000, ge=500, le=40000,
        description="Truncate the snapshot text to this many characters.",
    )


class BrowserExtractInput(_TitleMixin):
    session_id: str = Field(..., description="The browser session to read (from browser_navigate).")
    query: Optional[str] = Field(
        default=None,
        description="Optional focus for what to extract; currently returns the page's readable text either way.",
    )
    max_chars: int = Field(
        default=12000, ge=500, le=60000,
        description="Truncate the extracted text to this many characters.",
    )


class BrowserActInput(_TitleMixin):
    session_id: str = Field(..., description="The browser session to act in (from browser_navigate).")
    ref: str = Field(..., description="Element ref from the most recent snapshot, e.g. 'e12'.")
    action: str = Field(
        ...,
        description="One of: click, type, press, hover, select, scroll.",
    )
    text: Optional[str] = Field(
        default=None,
        description="Text for 'type', key for 'press' (e.g. 'Enter'), or option label/value for 'select'.",
    )


class BrowserVisionInput(_TitleMixin):
    session_id: str = Field(..., description="The browser session to screenshot (from browser_navigate).")
    full_page: bool = Field(
        default=False,
        description="Capture the full scrollable page instead of just the viewport.",
    )


class BrowserOutput(BaseModel):
    success: bool = Field(..., description="Whether the action succeeded.")
    session_id: Optional[str] = Field(default=None, description="The browser session id.")
    url: Optional[str] = Field(default=None, description="Current page URL after the action.")
    title: Optional[str] = Field(default=None, description="Current page title.")
    snapshot: Optional[str] = Field(default=None, description="Accessibility snapshot with element refs.")
    text: Optional[str] = Field(default=None, description="Extracted readable text (browser_extract).")
    screenshot_file_id: Optional[str] = Field(default=None, description="File id of a captured screenshot.")
    downloads: Optional[List[dict]] = Field(default=None, description="Files downloaded during the action: [{file_id, filename}].")
    blocked_reason: Optional[str] = Field(
        default=None,
        description="Set when the page needs something the agent can't do: 'authentication', 'captcha', 'allowlist'.",
    )
    truncated: bool = Field(default=False, description="True if snapshot/text was truncated.")
    error_message: Optional[str] = Field(default=None, description="Error message if the action failed.")
    error_code: Optional[str] = Field(default=None, description="Machine code, e.g. 'stale_ref', 'not_allowed', 'no_session'.")
