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
            "The instruction text. Must be clear, actionable, and reusable. "
            "Should capture non-obvious semantic rules that prevent mistakes or improve accuracy. "
            "Must end with a period."
        ),
        min_length=20,
        max_length=20000000000,
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
            "'general' (business rules, definitions, terminology), "
            "'code_gen' (SQL/code patterns, joins, filters, aggregations), "
            "'visualization' (chart types, colors, formatting), "
            "'dashboard' (layout, composition), "
            "'system' (agent behavior, clarification flows)"
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
            "Brief explanation of evidence supporting this instruction. "
            "E.g., 'Observed in inspect_data: status column has values 1,2,3 mapping to active/inactive/banned'"
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

    message: str = Field(
        ...,
        description="Status message describing what happened"
    )

    rejected_reason: Optional[str] = Field(
        None,
        description="Reason if instruction was rejected (e.g., duplicate, low confidence)"
    )
