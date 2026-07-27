from typing import Optional, List
from pydantic import BaseModel, Field


class CreateInstructionInput(BaseModel):
    """Input schema for create_instruction tool - creates a new instruction during training mode.

    This tool creates reusable instructions that guide AI behavior. Only use when
    you have HIGH CONFIDENCE in the instruction based on evidence from exploration.
    """

    text: str = Field(
        ...,
        description=(
            "The instruction text. Must be clear, actionable, and reusable — a general rule, "
            "never a record-level fact. "
            "Do NOT write raw data facts as standalone statements (e.g. 'the orders table has 32 rows', "
            "'revenue is $100,000', 'there are 5 users') — these become stale as data changes. "
            "Do NOT write facts about one specific person/customer/row (e.g. 'Maria's last name "
            "is Novak', 'exclude order 9174') — such facts live in the data, not in instructions. "
            "If the session's learning is an instance of a general rule, capture the rule without "
            "the record-specific detail; record-level text is rejected with rejected_reason='overfit'. "
            "DO capture clarified terms and definitions (e.g. 'X means revenue > $5 USD'), "
            "business rules, enum meanings, schema relationships, and SQL/code patterns. "
            "Use markdown formatting for clarity."
        ),
        min_length=20,
        max_length=20000,
    )

    title: Optional[str] = Field(
        None,
        description="Short title for the instruction (auto-generated if not provided)",
        max_length=200,
    )

    category: str = Field(
        default="general",
        description=(
            "Category for the instruction: "
            "'general' (business rules, domain definitions, terminology, clarified terms — default when the instruction names a domain term or entity), "
            "'code_gen' (code-level rules applied when generating SQL/code: error fixes, dialect quirks, join patterns, cast/NULL handling, column transformations), "
            "'visualization' (chart types, colors, formatting), "
            "'dashboard' (layout, composition), "
            "'system' (agent behavior / meta-rules only — do NOT use for domain term bindings)"
        ),
    )

    confidence: float = Field(
        ...,
        description=(
            "Confidence level (0.0-1.0) that this instruction is correct and valuable. "
            "Only create instructions with confidence >= 0.7. "
            "If confidence is lower, use clarify tool to ask the user first."
        ),
        ge=0.0,
        le=1.0,
    )

    evidence: Optional[str] = Field(
        None,
        description=(
            "ALWAYS provide. ONE short sentence (aim for under 150 characters) naming the "
            "source and the fact: what you observed or who confirmed it. Shown to reviewers "
            "next to 'AI suggested' in the review UI, so keep it scannable — no preamble, "
            "no restating the instruction. "
            "E.g. 'inspect_data: orders.status includes cancelled/refunded.' or "
            "'User confirmed: status 1=active, 2=inactive, 3=banned.'"
        ),
        max_length=500,
    )

    load_mode: str = Field(
        default="intelligent",
        description=(
            "When to load this instruction into AI context: "
            "'always' (always include - use for critical business rules), "
            "'intelligent' (include when referenced tables/columns are relevant - recommended for most)"
        ),
    )

    table_names: Optional[List[str]] = Field(
        None,
        description=(
            "List of table names this instruction relates to (e.g., 'orders', 'public.users'). "
            "Supports exact names or patterns. Names are matched case-insensitively with optional schema prefix. "
            "The instruction will be scoped to the data sources of these tables. "
            "For 'intelligent' load_mode, instruction loads when these tables are queried."
        ),
    )


class CreateInstructionOutput(BaseModel):
    """Output schema for create_instruction tool response."""

    success: bool = Field(
        ...,
        description="Whether the instruction was created successfully"
    )

    instruction_id: Optional[str] = Field(
        None,
        description="ID of the created instruction (if successful)"
    )

    title: Optional[str] = Field(
        None,
        description="Title of the created instruction (provided or auto-generated)"
    )

    build_id: Optional[str] = Field(
        None,
        description="ID of the draft build this instruction was added to."
    )

    message: str = Field(
        ...,
        description="Status message describing what happened"
    )

    rejected_reason: Optional[str] = Field(
        None,
        description="Reason if instruction was rejected (e.g., duplicate, low confidence)"
    )
