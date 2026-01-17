from typing import Optional, List
from pydantic import BaseModel, Field


class EditInstructionInput(BaseModel):
    """Input schema for edit_instruction tool - edits an existing instruction during training mode.

    Use this when you need to correct, improve, or refine an instruction.
    """

    instruction_id: str = Field(
        ...,
        description=(
            "The ID of the instruction to edit. You can find instruction IDs in the "
            "observation from previous create_instruction calls, or from existing "
            "instructions in the context."
        ),
    )

    text: Optional[str] = Field(
        None,
        description=(
            "The new instruction text. If provided, must be clear, actionable, and reusable. "
            "Should capture non-obvious semantic rules that prevent mistakes or improve accuracy. "
            "Must end with a period."
        ),
        min_length=20,
        max_length=20000000000,
    )

    title: Optional[str] = Field(
        None,
        description="Updated title for the instruction",
        max_length=200,
    )

    category: Optional[str] = Field(
        None,
        description=(
            "Updated category for the instruction: "
            "'general' (business rules, definitions, terminology), "
            "'code_gen' (SQL/code patterns, joins, filters, aggregations), "
            "'visualization' (chart types, colors, formatting), "
            "'dashboard' (layout, composition), "
            "'system' (agent behavior, clarification flows)"
        ),
    )

    confidence: Optional[float] = Field(
        None,
        description=(
            "Updated confidence level (0.0-1.0). "
            "Only update if you have new evidence that changes your confidence. "
            "Minimum allowed is 0.7."
        ),
        ge=0.0,
        le=1.0,
    )

    evidence: Optional[str] = Field(
        None,
        description=(
            "Updated or additional evidence supporting this instruction. "
            "E.g., 'User confirmed via clarify: status 1=active, 2=inactive'"
        ),
        max_length=500,
    )

    load_mode: Optional[str] = Field(
        None,
        description=(
            "Updated load mode: "
            "'always' (always include - use for critical business rules), "
            "'intelligent' (include when referenced tables/columns are relevant)"
        ),
    )

    table_names: Optional[List[str]] = Field(
        None,
        description=(
            "Updated list of table names this instruction relates to. "
            "Supports exact names or patterns. Names are matched case-insensitively. "
            "Set to empty list to clear table associations."
        ),
    )


class EditInstructionOutput(BaseModel):
    """Output schema for edit_instruction tool response."""

    success: bool = Field(
        ...,
        description="Whether the instruction was updated successfully"
    )

    instruction_id: str = Field(
        ...,
        description="ID of the instruction that was edited"
    )

    version_number: Optional[int] = Field(
        None,
        description="The new version number after the edit (if content changed)"
    )

    message: str = Field(
        ...,
        description="Status message describing what happened"
    )

    rejected_reason: Optional[str] = Field(
        None,
        description="Reason if edit was rejected (e.g., not_found, permission_denied, invalid_format)"
    )
