from typing import Optional
from pydantic import BaseModel, Field


class WebFetchInput(BaseModel):
    url: str = Field(
        ...,
        description="The HTTP or HTTPS URL to fetch. Must be a publicly reachable address.",
    )


class WebFetchOutput(BaseModel):
    success: bool = Field(..., description="Whether the fetch succeeded.")
    url: Optional[str] = Field(default=None, description="The URL that was requested.")
    final_url: Optional[str] = Field(
        default=None,
        description="The final URL after any redirects.",
    )
    status_code: Optional[int] = Field(default=None, description="HTTP status code.")
    content_type: Optional[str] = Field(default=None, description="Response Content-Type header.")
    content: Optional[str] = Field(default=None, description="Decoded response body (truncated if large).")
    truncated: bool = Field(default=False, description="True if the content was truncated.")
    error_message: Optional[str] = Field(default=None, description="Error message if the fetch failed.")
