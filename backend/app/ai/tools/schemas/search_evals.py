from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class SearchEvalsInput(BaseModel):
    """Input schema for ``search_evals`` tool.

    Lists/searches eval test cases in the organization. Use BEFORE
    ``create_eval`` to find duplicates or related cases that should be
    edited rather than re-created.
    """

    query: Optional[str] = Field(
        None,
        description=(
            "Substring to match (case-insensitive) against the case name "
            "and the prompt content. Leave empty to list cases in the "
            "scope filtered only by suite_id / status."
        ),
        max_length=400,
    )

    suite_id: Optional[str] = Field(
        None,
        description="Restrict to a single suite by id.",
    )

    status: Literal["active", "draft", "archived", "all"] = Field(
        "all",
        description=(
            "Filter by case status. ``all`` includes drafts and archived "
            "cases — useful when checking for any prior version of a case."
        ),
    )

    limit: int = Field(
        10,
        ge=1,
        le=50,
        description="Maximum number of cases to return (1-50).",
    )


class SearchEvalsItem(BaseModel):
    id: str
    name: str
    prompt_content: str
    suite_id: str
    suite_name: str
    status: str
    auto_generated: bool = False
    rule_count: int = 0


class SearchEvalsOutput(BaseModel):
    success: bool
    items: List[SearchEvalsItem] = Field(default_factory=list)
    total: int = 0
    message: Optional[str] = None
