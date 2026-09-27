"""Input/output schemas for the user-memory tools (create/edit/search_memory)
and suggest_personal_instruction."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

_TITLE_DESC = (
    "Short, friendly status line shown to the user while this runs: 3-7 words, in the user's "
    "language, no private details beyond what the user just said (e.g. \"Remembering your board "
    "meeting\", \"Noting your Q3 project\")."
)
_DATE_DESC = (
    "Optional absolute ISO date (YYYY-MM-DD) when the fact is tied to a date — a meeting, a deadline, "
    "time off. Resolve 'next Thursday' against today's date first. Keep the date here, not in `text`. "
    "A dated fact ends after its date."
)


class CreateMemoryInput(BaseModel):
    text: str = Field(..., description=(
        "ONE fact about the current user (≤280 chars): their work, a project or deadline, a date in their "
        "work life, what they follow, or their own shorthand — e.g. \"Board meeting\", \"Leads the Q3 churn "
        "project\", \"Follows weekly NRR for EMEA\", \"'my region' = EMEA\". Never a rule about how to "
        "answer or compute (that is an instruction)."
    ))
    tags: List[str] = Field(..., description=(
        "1-4 short lowercase slugs (e.g. \"emea\", \"board-deck\", \"q3-churn\"). REUSE tags listed in the "
        "<memory> index. To tie the fact to an agent/data source/report, add an object tag like "
        "\"agent:<id>\"."
    ))
    aliases: Optional[List[str]] = Field(default=None, description=(
        "Other words the user uses for the same thing, e.g. [\"my region\", \"my patch\"]."
    ))
    date: Optional[str] = Field(default=None, description=_DATE_DESC)
    end_date: Optional[str] = Field(default=None, description="Absolute ISO end date for something that spans days.")
    expires_at: Optional[str] = Field(default=None, description=(
        "Optional ISO date after which an undated fact stops applying (e.g. the end of a project)."
    ))
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class CreateMemoryOutput(BaseModel):
    success: bool
    handle: Optional[str] = None
    deduped_into: Optional[str] = None
    error: Optional[str] = None


class EditMemoryInput(BaseModel):
    handle: str = Field(..., description="The entry's handle from <memory> or search_memory, e.g. \"m7\".")
    action: Literal["update", "delete"] = Field(..., description="update = replace fields; delete = forget the entry.")
    text: Optional[str] = Field(default=None, description="New text (update only; ≤280 chars, one fact).")
    tags: Optional[List[str]] = None
    aliases: Optional[List[str]] = None
    date: Optional[str] = Field(default=None, description=_DATE_DESC)
    end_date: Optional[str] = None
    expires_at: Optional[str] = None
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class EditMemoryOutput(BaseModel):
    success: bool
    handle: Optional[str] = None
    error: Optional[str] = None


class SearchMemoryInput(BaseModel):
    query: Optional[str] = Field(default=None, description="Words to look for in the user's memory (text, aliases, tags).")
    tags: Optional[List[str]] = Field(default=None, description="Only entries carrying any of these tags.")
    include_past: bool = Field(default=False, description="Also return dated facts whose date has passed.")
    limit: int = Field(default=10, ge=1, le=25)
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class SearchMemoryOutput(BaseModel):
    success: bool
    count: int = 0
    error: Optional[str] = None


class SuggestPersonalInstructionInput(BaseModel):
    text: str = Field(..., description=(
        "The user's own lasting rule for how you should answer them, written as an instruction "
        "(≤200 chars), e.g. \"Lead with the number, then one line of context.\" or \"Show money in "
        "thousands with one decimal ($2.3K).\" Only rules that are personal to this user — never "
        "business definitions or rules that hold for everyone."
    ))
    title: Optional[str] = Field(default=None, description=_TITLE_DESC)


class SuggestPersonalInstructionOutput(BaseModel):
    success: bool
    text: Optional[str] = None
    already_saved: bool = False
    error: Optional[str] = None
