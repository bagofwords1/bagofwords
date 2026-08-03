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

    old_text: Optional[str] = Field(
        None,
        description=(
            "REQUIRED whenever you pass `text`. An exact snippet of the CURRENT instruction "
            "text, which `text` replaces. Must match exactly once — use a short unique snippet "
            "(a phrase or sentence), never the whole instruction.\n"
            "It must be REAL text: \"\" is not an anchor, and omitting it is not either — both "
            "are rejected. To ADD a rule, anchor the sentence it belongs after and repeat that "
            "sentence verbatim at the start of `text`, followed by your addition.\n"
            "Replacing the whole instruction is a separate, deliberate act — set "
            "`replace_entire_text: true` — never something you get by leaving this out."
        ),
    )

    replace_entire_text: bool = Field(
        False,
        description=(
            "Opt in to discarding the ENTIRE current instruction and replacing it with `text`. "
            "Only for an explicit rewrite request ('rewrite this instruction', 'start over'). "
            "Rejected in knowledge mode, where the harness edits autonomously and must not be "
            "able to delete curated content. For everything else — including adding a rule, "
            "correcting wording, or narrowing scope — use `old_text` instead. Making several "
            "small changes means several anchored calls, which is correct and preferred over "
            "one rewrite."
        ),
    )

    text: Optional[str] = Field(
        None,
        description=(
            "The new text. It replaces the snippet named by `old_text`; with "
            "`replace_entire_text: true` it replaces the whole instruction instead. Passing it "
            "with neither is rejected. "
            "Write ONLY the change that was asked for — not a restatement of the surrounding "
            "text, and not the same change propagated through the rest of the instruction. "
            "Must be clear, actionable, and reusable. "
            "Should capture non-obvious semantic rules that prevent mistakes or improve accuracy. "
            "Do not include volatile data facts (row counts, specific metric values, date ranges, distributions) that change as data is updated. "
            "Do NOT include record-level facts — attributes of one specific person/customer/row "
            "(e.g. 'Maria's last name is Novak', 'exclude order 9174') or observed counts/values. "
            "State the general rule the observation is an instance of. "
            "Use markdown formatting for clarity."
        ),
        # No min_length here: with an anchor, `text` is a snippet and may be
        # short. The tool validates the RESULTING full text is >= 20 chars.
        max_length=20000,
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
            "ALWAYS provide. ONE short sentence (aim for under 150 characters) naming the "
            "source and the fact behind this edit. Shown to reviewers next to 'AI suggested' "
            "in the review UI, so keep it scannable — no preamble. "
            "E.g. 'User confirmed: status 1=active, 2=inactive.'"
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

    title: Optional[str] = Field(
        None,
        description="Title of the instruction after edit"
    )

    build_id: Optional[str] = Field(
        None,
        description="ID of the draft build this edit was added to."
    )

    previous_text: Optional[str] = Field(
        None,
        description="The instruction text before this edit, when the text field was updated."
    )

    new_text: Optional[str] = Field(
        None,
        description="The instruction text after this edit, when the text field was updated."
    )
