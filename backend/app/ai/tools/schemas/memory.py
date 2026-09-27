"""Input/output schemas for the user-memory tools (create/edit/search_memory)."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

MemorySection = Literal["style", "role", "vocabulary", "events", "focus", "preferences"]

_TITLE_DESC = (
    "Short, friendly status line shown to the user while this runs: 3-7 words, in the user's "
    "language, no private details beyond what the user just said (e.g. \"Remembering your board "
    "meeting\", \"Noting you prefer the number first\")."
)


class CreateMemoryInput(BaseModel):
    text: str = Field(..., description=(
        "ONE declarative, personal fact about the current user (≤280 chars), e.g. \"Prefers the number "
        "first, then one line of context\", \"Presents to the CFO monthly\", \"'my region' = EMEA\". "
        "Never an imperative, never a business definition or rule."
    ))
    section: MemorySection = Field(..., description=(
        "style (writing/output style) · role (role and work context) · vocabulary (their personal "
        "shorthand) · events (dated meetings, deadlines, time off) · focus (what they work on now) · "
        "preferences (how they like to work with you)."
    ))
    tags: List[str] = Field(..., description=(
        "1-4 short lowercase slugs (e.g. \"emea\", \"board-deck\", \"churn\"). REUSE tags listed in the "
        "<memory> index. To tie the fact to an agent/data source/report, add an object tag like "
        "\"agent:<id>\"."
    ))
    aliases: Optional[List[str]] = Field(default=None, description=(
        "Other words the user uses for the same thing (vocabulary/focus), e.g. [\"my region\", \"my patch\"]."
    ))
    event_start: Optional[str] = Field(default=None, description=(
        "Absolute ISO date (YYYY-MM-DD). REQUIRED for section=events — resolve 'next Thursday' against "
        "today's date first. Keep dates here, not in `text` (text: \"Board meeting\", \"Out of office\")."
    ))
    event_end: Optional[str] = Field(default=None, description="Absolute ISO end date for multi-day events.")
    expires_at: Optional[str] = Field(default=None, description="Optional ISO date after which the fact stops applying.")
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class CreateMemoryOutput(BaseModel):
    success: bool
    handle: Optional[str] = None
    deduped_into: Optional[str] = None
    error: Optional[str] = None


class EditMemoryInput(BaseModel):
    handle: str = Field(..., description="The entry's handle from <memory> or search_memory, e.g. \"m7\".")
    action: Literal["update", "delete"] = Field(..., description="update = replace fields; delete = forget the entry.")
    text: Optional[str] = Field(default=None, description="New text (update only; ≤280 chars, declarative).")
    section: Optional[MemorySection] = None
    tags: Optional[List[str]] = None
    aliases: Optional[List[str]] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    expires_at: Optional[str] = None
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class EditMemoryOutput(BaseModel):
    success: bool
    handle: Optional[str] = None
    error: Optional[str] = None


class SearchMemoryInput(BaseModel):
    query: Optional[str] = Field(default=None, description="Words to look for in the user's memory (text, aliases, tags).")
    tags: Optional[List[str]] = Field(default=None, description="Only entries carrying any of these tags.")
    section: Optional[MemorySection] = None
    include_past_events: bool = Field(default=False, description="Also return events that already ended.")
    limit: int = Field(default=10, ge=1, le=25)
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class SearchMemoryOutput(BaseModel):
    success: bool
    count: int = 0
    error: Optional[str] = None
