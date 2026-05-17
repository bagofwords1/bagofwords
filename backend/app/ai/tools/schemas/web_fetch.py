from typing import Any, Dict, List, Optional
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
    content: Optional[str] = Field(
        default=None,
        description="For HTML responses: visible text extracted from the body with scripts/styles stripped. For other text content types: the raw body. Truncated if large.",
    )
    truncated: bool = Field(default=False, description="True if the content was truncated.")
    error_message: Optional[str] = Field(default=None, description="Error message if the fetch failed.")
    title: Optional[str] = Field(default=None, description="HTML <title> contents (HTML responses only).")
    description: Optional[str] = Field(default=None, description="HTML meta description (HTML responses only).")
    metadata: Optional[Dict[str, str]] = Field(
        default=None,
        description="Flat map of HTML <meta> tags (OpenGraph, Twitter Cards, standard meta). HTML responses only.",
    )
    json_ld: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Parsed JSON-LD blocks from <script type='application/ld+json'>. Often contains structured Product/NewsArticle/Recipe data. HTML responses only.",
    )
    headings: Optional[List[str]] = Field(
        default=None,
        description="Text of <h1> and <h2> elements in document order. HTML responses only.",
    )
