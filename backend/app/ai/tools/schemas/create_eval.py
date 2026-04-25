from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from app.schemas.test_expectations import ExpectationsSpec


class CreateEvalPrompt(BaseModel):
    """Replay prompt for the eval — same shape as the chat PromptSchema."""

    content: str = Field(
        ...,
        description=(
            "The user prompt the eval will replay verbatim. Use the user's "
            "original question, do not paraphrase."
        ),
        min_length=1,
    )
    mode: Optional[str] = Field(
        None, description="Optional run mode (chat / deep / training)."
    )
    model_id: Optional[str] = Field(
        None, description="Pin a specific model id; null = org default.",
    )


class CreateEvalInput(BaseModel):
    """Input schema for ``create_eval`` tool.

    In knowledge mode this tool forces ``status='draft'``, forces the
    suite to the per-org default drafts suite, and sets
    ``auto_generated=True`` regardless of the values supplied here.
    Training mode respects all inputs (suite_id falls back to the default
    drafts suite when omitted).
    """

    name: str = Field(
        ..., min_length=1, max_length=200,
        description="Short, human-readable label for the case.",
    )
    prompt: CreateEvalPrompt
    expectations: ExpectationsSpec = Field(
        ...,
        description=(
            "Assertions for the case. Use ``tool.calls`` rules for set-membership "
            "of expected tool calls. Use ``judge`` rules for an LLM-as-judge rubric "
            "— the rubric MUST be grounded in this specific successful run and name: "
            "(1) the entity / output shape (e.g. 'a list of opportunities, one row "
            "per opp; not a count'), (2) the filters/joins that defined correctness "
            "(e.g. 'opps owned by the requesting user; joined to accounts via "
            "account_id; open stages only — exclude Closed-Won and Closed-Lost'), "
            "(3) any definitions the user implicitly approved (e.g. \"'my opps' = "
            "owner_id = current_user\"), and (4) 1–2 negative criteria — plausible "
            "but wrong variants to reject (e.g. 'reject if it returns a count "
            "instead of a list', 'reject if it includes closed deals'). "
            "Tautologies like 'reject if irrelevant' or 'reject if it misses the "
            "asked metric' are NOT acceptable — they pass anything plausible. "
            "Do NOT restate the user's question (it's already in ``prompt.content``). "
            "Do NOT list tools used in the judge prompt (the ``tool.calls`` rules "
            "already cover that). Do NOT add ``field`` rules — they assert on raw "
            "SQL/data and rot across schema drift."
        ),
    )
    suite_id: Optional[str] = Field(
        None,
        description=(
            "Suite to put the case in. Required in training mode unless you "
            "want it to land in the org's default drafts bucket. Ignored in "
            "knowledge mode (always routes to drafts)."
        ),
    )
    data_source_ids: Optional[List[str]] = Field(
        default_factory=list,
        description="Data sources this case scopes to. Used by run-time scoping.",
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Free-form tags for filtering / grouping in the UI.",
    )
    status: Optional[Literal["active", "draft"]] = Field(
        None,
        description=(
            "Initial status. Training mode default is ``active``; knowledge "
            "mode forces ``draft`` regardless of this value."
        ),
    )


class CreateEvalOutput(BaseModel):
    success: bool
    case_id: Optional[str] = None
    name: Optional[str] = None
    suite_id: Optional[str] = None
    suite_name: Optional[str] = None
    status: Optional[str] = None
    auto_generated: bool = False
    rejected_reason: Optional[str] = None
    message: Optional[str] = None
