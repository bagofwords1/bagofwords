"""Edit Instruction Tool - Edits instructions during training mode exploration.

This tool allows the training mode agent to edit instructions that were created
in the current training session. All edits create new versions that are added
to the same draft build.
"""

import difflib
import re
from typing import AsyncIterator, Dict, Any, Optional, Tuple, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.implementations.create_instruction import clamp_evidence
from app.ai.tools.schemas.edit_instruction import EditInstructionInput, EditInstructionOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

# Minimum confidence to maintain for an instruction
MIN_CONFIDENCE_THRESHOLD = 0.7

# Valid categories
VALID_CATEGORIES = {"general", "code_gen", "visualization", "dashboard", "system"}

# Minimum length for the RESULTING instruction text after an edit. The schema
# no longer enforces min_length on `text` (an anchored replacement snippet may
# legitimately be short), so the invariant is checked on the final text here.
MIN_RESULT_TEXT_LENGTH = 20


def _closest_region(current: str, anchor: str, context: int = 60) -> Optional[str]:
    """Best-effort diagnostic for a failed anchor match: the region of the
    current text most similar to the anchor, so the model can retry with a
    snippet that actually exists (same idea as edit_artifact's near-miss
    reporting)."""
    try:
        sm = difflib.SequenceMatcher(None, current, anchor, autojunk=False)
        m = sm.find_longest_match(0, len(current), 0, len(anchor))
        if m.size < min(10, max(1, len(anchor) // 2)):
            return None
        start = max(0, m.a - context)
        end = min(len(current), m.a + m.size + context)
        return current[start:end]
    except Exception:
        return None


async def _load_pending_version(db, build_id: Optional[str], instruction_id: str):
    """The version this build already stages for ``instruction_id``, if any.

    Edits are staged as versions and the live row is intentionally NOT mutated
    until the build is promoted, so within a single session the live row still
    holds the pre-edit text. ``add_to_build`` keeps exactly ONE version pointer
    per (build, instruction) — so a second edit that based itself on the live
    row would produce a version missing the first edit, and repointing the
    build to it would silently discard that first edit. Basing each edit on the
    pending version instead makes sequential edits accumulate.
    """
    if not build_id:
        return None
    from sqlalchemy import select
    from app.models.build_content import BuildContent
    from app.models.instruction_version import InstructionVersion

    result = await db.execute(
        select(InstructionVersion)
        .join(BuildContent, BuildContent.instruction_version_id == InstructionVersion.id)
        .where(
            BuildContent.build_id == build_id,
            BuildContent.instruction_id == instruction_id,
        )
    )
    return result.scalars().first()


def _apply_anchor_edit(
    current: str, old_text: str, new_snippet: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Replace ``old_text`` with ``new_snippet`` inside ``current``.

    Matching is exact-first, then whitespace-normalized (instructions are
    human prose, so runs of spaces/newlines must not fail the match).

    Returns (result_text, error_code, error_detail); exactly one of
    result_text / error_code is set. error_code is 'anchor_not_found' or
    'ambiguous_anchor'.
    """
    # Exact substring match
    count = current.count(old_text)
    if count == 1:
        return current.replace(old_text, new_snippet, 1), None, None
    if count > 1:
        return None, "ambiguous_anchor", (
            f"old_text matches {count} locations in the instruction text. "
            "Provide a longer, unique snippet."
        )

    # Whitespace-normalized fallback: whitespace runs in the anchor match any
    # whitespace run in the text.
    tokens = old_text.split()
    if tokens:
        pattern = r"\s+".join(re.escape(tok) for tok in tokens)
        matches = list(re.finditer(pattern, current))
        if len(matches) == 1:
            m = matches[0]
            return current[: m.start()] + new_snippet + current[m.end():], None, None
        if len(matches) > 1:
            return None, "ambiguous_anchor", (
                f"old_text matches {len(matches)} locations in the instruction text "
                "(after whitespace normalization). Provide a longer, unique snippet."
            )

    detail = "old_text was not found in the current instruction text."
    closest = _closest_region(current, old_text)
    if closest:
        detail += f" Closest matching region: \"…{closest}…\""
    return None, "anchor_not_found", detail


class EditInstructionTool(Tool):
    """Edit instruction tool - edits existing instructions during training mode.

    This tool is available only in training mode. It edits instructions that
    belong to the organization. Edits create new versions that are tracked
    in the training draft build.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_instruction",
            description=(
                "ACTION: Edit an existing instruction. "
                "Use when you need to correct mistakes, improve clarity, update confidence after "
                "user confirmation, or refine table associations.\n\n"
                "TEXT EDITS ARE ANCHORED. `text` always needs `old_text`: a short unique "
                "snippet of the CURRENT instruction text, which `text` replaces. It must be "
                "real text — \"\" is not an anchor, and omitting it is not either; both are "
                "REJECTED (rejected_reason='anchor_required'). To ADD a rule at the end, "
                "anchor the last sentence and repeat it verbatim at the start of `text` "
                "followed by your addition.\n\n"
                "CHANGE ONLY WHAT WAS ASKED. Encode the rule the user's request or your cited "
                "evidence establishes, and nothing else. Do not restate untouched sentences, "
                "propagate the change through the rest of the document for consistency, or add "
                "adjacent rules and tool-usage guidance nobody requested — whether the rest "
                "should follow suit is the reviewer's call, not yours. Leave metadata "
                "(category, load_mode, confidence) alone unless the request is about it. When "
                "reverting, remove exactly what was added — not the sentence that contained "
                "it.\n\n"
                "SEVERAL CHANGES = SEVERAL CALLS. One call edits one place. If a request "
                "touches three sentences, make three edit_instruction calls, each anchored on "
                "its own snippet — sequentially is fine, they need not be in one turn, and the "
                "observation of each returns the resulting text for the next one to anchor on. "
                "That is the CORRECT shape, not a workaround: never widen a single edit, and "
                "never reach for a rewrite, because a change touches more than one place.\n\n"
                "REWRITING EVERYTHING is a separate, deliberate act: `replace_entire_text: true`, "
                "and only when the user explicitly asked to rewrite or start over — never "
                "because several places need changing, and never to work around a failed "
                "anchor. Rejected in knowledge mode.\n\n"
                "ANCHOR ON TEXT YOU HAVE READ. Anchors must match the instruction's CURRENT "
                "text exactly. Call read_instruction first if you have not seen it this "
                "session; after an edit of your own, anchor on the `new_text` that edit "
                "returned, not on what the instruction said before it.\n\n"
                "CHECK WHAT IS ALREADY PROPOSED. read_instruction also returns "
                "`pending_changes` — suggestions awaiting review, with who proposed each and "
                "when. Those are NOT part of the live text: never anchor on them (the anchor "
                "will not match) and do not assume they landed. If one already makes the "
                "change you were about to make, say so and ask whether to replace it rather "
                "than stacking a second suggestion; if the user asks you to undo something "
                "that is still only pending, the answer is that it was never applied — do not "
                "edit the live text to remove it.\n\n"
                "SCOPING — table_names: Pass ONLY when you want to change the table scope. "
                "Pass an empty list [] to make the instruction global (remove all table scoping). "
                "OMIT the field entirely to leave the existing scoping unchanged. Listing every "
                "table inspected is wrong — it scopes the instruction and may prevent it from "
                "loading in unrelated queries.\n\n"
                "EVIDENCE: always set `evidence` — ONE short sentence (under 150 characters) "
                "naming the source and the fact. Reviewers see it next to the suggested change; "
                "no preamble."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditInstructionInput.model_json_schema(),
            output_schema=EditInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=["manage_instructions"],
            tags=["training", "instruction", "semantic-learning"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "confidence": 0.95,
                        "evidence": "User confirmed: status 1=active, 2=inactive, 3=banned."
                    },
                    "description": "Update confidence only — omit table_names to keep existing scope."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "old_text": "Exclude cancelled orders from revenue.",
                        "text": "Exclude cancelled orders from revenue. Refunded orders are excluded too.",
                        "evidence": "User clarified: refunds never count toward revenue."
                    },
                    "description": "Add a related learning by anchoring the sentence it belongs after and repeating it verbatim before the addition."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "old_text": "exclude orders with status='cancelled'",
                        "text": "exclude orders with status='cancelled' or status='refunded'",
                        "evidence": "User corrected: refunded orders must be excluded too."
                    },
                    "description": "Surgical correction — old_text anchors the exact snippet to replace."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "old_text": "find relevant files by topic",
                        "text": "find relevant PDF files by topic",
                        "evidence": "User asked to handle only PDFs."
                    },
                    "description": "Narrow one rule to PDFs. A request that touches several sentences is several anchored calls like this — NOT a rewrite, and untouched sentences stay untouched."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "table_names": []
                    },
                    "description": "Make it global — empty list removes all table scoping so the instruction applies everywhere."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "category": "code_gen",
                        "load_mode": "always"
                    },
                    "description": "Change category and load mode for a critical rule."
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditInstructionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        """Execute edit_instruction - updates instruction in training session's draft build."""

        try:
            data = EditInstructionInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Invalid input: {str(e)}",
                    "code": "INVALID_INPUT"
                }
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "instruction_id": data.instruction_id,
                "updating_fields": [k for k, v in tool_input.items() if v is not None and k != "instruction_id"],
            }
        )


        # Validate confidence threshold if provided
        if data.confidence is not None and data.confidence < MIN_CONFIDENCE_THRESHOLD:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=f"Confidence {data.confidence} is below minimum threshold {MIN_CONFIDENCE_THRESHOLD}.",
                        rejected_reason="low_confidence"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edit rejected: confidence {data.confidence} < {MIN_CONFIDENCE_THRESHOLD}",
                        "artifacts": [],
                    },
                }
            )
            return

        # Validate category if provided
        if data.category is not None and data.category not in VALID_CATEGORIES:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=f"Invalid category '{data.category}'. Must be one of: {', '.join(VALID_CATEGORIES)}",
                        rejected_reason="invalid_category"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edit rejected: invalid category '{data.category}'",
                        "artifacts": [],
                    },
                }
            )
            return

        # Get required context from runtime
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        training_build_id = runtime_ctx.get("training_build_id")
        agent_execution_id = runtime_ctx.get("agent_execution_id")
        mode = runtime_ctx.get("mode")
        report = runtime_ctx.get("report")

        # In knowledge-harness / post-analysis mode, restrict table resolution
        # to data sources actually attached to the current report (parity with
        # create_instruction). Training mode is broader — user is intentionally
        # curating the org — so we keep it org-scoped there.
        allowed_data_source_ids = None
        if mode == "knowledge" and report is not None:
            try:
                allowed_data_source_ids = {
                    str(ds.id) for ds in (report.data_sources or [])
                }
            except Exception:
                allowed_data_source_ids = set()

        # Lazy build creation: the harness no longer pre-seeds a draft.
        # The first create/edit in a session lazily creates the build below
        # (in the add_to_build path) and writes the id back into runtime_ctx.
        if not all([db, organization]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization)",
                    "code": "MISSING_CONTEXT"
                }
            )
            return

        try:
            from sqlalchemy import select, or_, func
            from sqlalchemy.orm import selectinload
            from app.models.instruction import Instruction
            from app.models.datasource_table import DataSourceTable
            from app.models.data_source import DataSource
            from app.services.build_service import BuildService
            from app.services.instruction_version_service import InstructionVersionService
            from app.schemas.instruction_reference_schema import InstructionReferenceCreate

            build_service = BuildService()
            version_service = InstructionVersionService()

            # Fetch the instruction (read-only — we do NOT mutate the live row).
            # Edits are staged as a new InstructionVersion attached to the draft
            # build. Live row is updated only when the build is promoted.
            stmt = (
                select(Instruction)
                .options(
                    selectinload(Instruction.data_sources),
                    selectinload(Instruction.labels),
                    selectinload(Instruction.references),
                )
                .where(
                    Instruction.id == data.instruction_id,
                    Instruction.organization_id == str(organization.id)
                )
            )
            result = await db.execute(stmt)
            instruction = result.scalar_one_or_none()

            if not instruction:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": EditInstructionOutput(
                            success=False,
                            instruction_id=data.instruction_id,
                            message=f"Instruction '{data.instruction_id}' not found",
                            rejected_reason="not_found"
                        ).model_dump(),
                        "observation": {
                            "summary": f"Edit failed: instruction '{data.instruction_id}' not found",
                            "artifacts": [],
                        },
                    }
                )
                return

            # Cross-agent authority gate (knowledge/post-analysis mode): a shared
            # instruction is a single row loaded by every attached agent. Editing
            # one from a session scoped to a subset of its agents would stage a
            # change that reaches agents outside this session — the
            # instruction-sharing escalation. Refuse when the instruction is
            # attached to any data source outside the session's scope. Training
            # mode is deliberately broader (explicit org-wide curation), and a
            # GLOBAL instruction (no attached agents) passes this gate — its
            # blast radius is handled by the human publish gate, which requires
            # org-level authority to promote. Ported from PR #732.
            if allowed_data_source_ids is not None:
                attached_ds_ids = {str(ds.id) for ds in (instruction.data_sources or [])}
                out_of_scope = attached_ds_ids - set(allowed_data_source_ids)
                if out_of_scope:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=data.instruction_id,
                                title=getattr(instruction, "title", None),
                                message=(
                                    "Cannot edit this instruction: it is shared with "
                                    "agent(s) outside this session. Editing it would "
                                    "change behaviour for agents not in scope."
                                ),
                                rejected_reason="out_of_scope",
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    "Edit rejected: instruction shared with out-of-scope "
                                    "agents. Do not retry — suggest the change to the "
                                    "user instead."
                                ),
                                "artifacts": [],
                            },
                        }
                    )
                    return

            # === Resolve the base this edit applies to ===
            # Prefer the version already staged in this build over the live row:
            # the live row is not updated until promotion, so a second edit in
            # the same session would otherwise re-read pre-edit text and drop
            # the first edit (see _load_pending_version).
            build = None
            if training_build_id:
                build = await build_service.get_build(db, training_build_id)
            pending_version = None
            if build is not None and build.can_be_edited:
                pending_version = await _load_pending_version(
                    db, str(build.id), str(instruction.id)
                )
            base_text = (
                pending_version.text if pending_version is not None else instruction.text
            )

            previous_text = base_text if data.text is not None else None

            # === Compute the resulting text (anchor semantics) ===
            # old_text non-empty  -> search/replace within the current text
            # old_text == ""      -> rejected (not an anchor)
            # old_text omitted    -> rejected, unless replace_entire_text=true
            #
            # The anchor is mandatory by design. Every mainstream edit tool
            # (Claude Code's Edit, aider's SEARCH/REPLACE, unified diffs) makes
            # the old side required and puts whole-file replacement behind a
            # SEPARATELY NAMED act — so a surgical edit is the only thing the
            # edit tool can express, and destroying content takes a deliberate
            # decision rather than one fewer argument. This tool used to invert
            # that: omitting old_text — the call with the FEWEST arguments —
            # replaced the whole instruction. Asked to add one PDF-only rule, a
            # model took that path and rewrote every bullet plus category,
            # load_mode and confidence, because "one call that always applies"
            # beat "several anchored calls that can miss".
            new_text = base_text
            if data.text is not None:
                current_text = base_text or ""
                if data.old_text is None and not data.replace_entire_text:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=(
                                    "Edit rejected: `text` needs an anchor. Pass `old_text` with "
                                    "an exact snippet of the current text to replace it, or "
                                    "`old_text: \"\"` to append your text as a new paragraph. "
                                    "Make several small anchored edits rather than one large "
                                    "one. Only if the user explicitly asked to rewrite the whole "
                                    "instruction, re-send with `replace_entire_text: true`. "
                                    f"Current instruction text: \"{current_text}\""
                                ),
                                rejected_reason="anchor_required",
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    "Edit rejected: `text` without `old_text`. Retry with "
                                    "old_text (exact snippet to replace) or old_text: \"\" "
                                    "(append). Change only what was asked — several small "
                                    "anchored edits, not one rewrite."
                                ),
                                "current_text": current_text,
                                "artifacts": [],
                            },
                        }
                    )
                    return
                if data.old_text is None:
                    if mode == "knowledge":
                        yield ToolEndEvent(
                            type="tool.end",
                            payload={
                                "output": EditInstructionOutput(
                                    success=False,
                                    instruction_id=str(instruction.id),
                                    title=getattr(instruction, "title", None),
                                    message=(
                                        "replace_entire_text is not allowed in knowledge mode — "
                                        "the harness edits autonomously, so it must not be able "
                                        "to discard curated content. Pass old_text with an exact "
                                        "snippet of the current text to replace it, or "
                                        "old_text: \"\" to append your text as a new paragraph. "
                                        f"Current instruction text: \"{current_text}\""
                                    ),
                                    rejected_reason="full_replace_not_allowed",
                                ).model_dump(),
                                "observation": {
                                    "summary": (
                                        "Edit rejected: full rewrite not allowed in knowledge mode. "
                                        "Retry with old_text (exact snippet to replace) or "
                                        "old_text: \"\" (append)."
                                    ),
                                    "current_text": current_text,
                                    "artifacts": [],
                                },
                            }
                        )
                        return
                    new_text = data.text
                elif data.old_text == "":
                    # `old_text: ""` used to mean "append". That was the last
                    # sentinel overload on this parameter — one field meaning
                    # three different operations depending on whether it was a
                    # real string, empty, or absent. Standard edit tools have no
                    # such convention: the anchor is always real text, and you
                    # add at the end by anchoring the last sentence and
                    # including it in the replacement. One rule is easier to get
                    # right than three, and an anchored append states WHERE the
                    # addition goes instead of always landing at the bottom.
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=(
                                    "Edit rejected: `old_text` must be real text from the "
                                    "instruction — \"\" is not an anchor. To ADD something at "
                                    "the end, anchor the last sentence and repeat it verbatim "
                                    "at the start of `text`, followed by your addition. "
                                    f"Current instruction text: \"{current_text}\""
                                ),
                                rejected_reason="anchor_required",
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    "Edit rejected: old_text: \"\" is not an anchor. Anchor on "
                                    "an exact snippet of the current text; to append, anchor "
                                    "the last sentence and repeat it before your addition."
                                ),
                                "current_text": current_text,
                                "artifacts": [],
                            },
                        }
                    )
                    return
                else:
                    applied, err_code, err_detail = _apply_anchor_edit(
                        current_text, data.old_text, data.text
                    )
                    if err_code == "anchor_not_found":
                        # The classic miss: anchoring on wording from an edit
                        # staged in an EARLIER turn. That edit is pending review
                        # in another build, so the current text never contains
                        # it — and models tend to trust their conversation
                        # memory over the text echoed back. Say so explicitly.
                        err_detail += (
                            " If you anchored on wording from an edit you made in a "
                            "previous turn: that edit is still pending review and is "
                            "NOT part of the current text. Anchor on the current text "
                            "exactly as quoted below and apply only this turn's change."
                        )
                    if err_code:
                        yield ToolEndEvent(
                            type="tool.end",
                            payload={
                                "output": EditInstructionOutput(
                                    success=False,
                                    instruction_id=str(instruction.id),
                                    title=getattr(instruction, "title", None),
                                    message=(
                                        f"Edit rejected: {err_detail} "
                                        "Current instruction text: "
                                        f"\"{current_text}\""
                                    ),
                                    rejected_reason=err_code,
                                ).model_dump(),
                                "observation": {
                                    "summary": f"Edit rejected ({err_code}): {err_detail}",
                                    "current_text": current_text,
                                    "artifacts": [],
                                },
                            }
                        )
                        return
                    new_text = applied

                if len((new_text or "").strip()) < MIN_RESULT_TEXT_LENGTH:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=(
                                    f"Edit rejected: resulting instruction text would be shorter "
                                    f"than {MIN_RESULT_TEXT_LENGTH} characters."
                                ),
                                rejected_reason="result_too_short",
                            ).model_dump(),
                            "observation": {
                                "summary": "Edit rejected: resulting text too short",
                                "artifacts": [],
                            },
                        }
                    )
                    return

                # NO GENERALITY GATE ON EDITS.
                #
                # The gate used to run here on the RESULTING text — the whole
                # instruction, not the change. That judged the model on prose it
                # did not write and was not asked to touch: an instruction whose
                # inherited text contains anything the critic dislikes fails
                # every edit forever, however small and however general the edit
                # itself is. Onboarding (data_source_service, which writes
                # through the service and so never faced the critic) routinely
                # produces exactly that — "no files were indexed at
                # schema-review time", "the schema lists no tables" — and those
                # read as observed values.
                #
                # Worse, the rejection ARGUED FOR A REWRITE: "restate the edit
                # as the general rule ... or leave the instruction unchanged"
                # tells a model the document is the problem. Watched live, that
                # is exactly what it concluded — it stopped making the one-line
                # change it was asked for and rewrote everything to purge the
                # offending inherited lines.
                #
                # The veto is also redundant. Every AI-authored edit is staged
                # as a suggestion a human accepts or rejects hunk by hunk, so
                # there is already a gate, and it is one that can tell "this
                # sentence was here before" from "the model just wrote this".
                # create_instruction keeps its gate: there the model authored
                # every word, so judging the whole text is judging its own work.

            # Start from the current live row state, overlay only the fields
            # the caller wants to change. These values become the new version.
            # Unchanged fields carry over from the same base as the text (the
            # pending version when this build already stages one), so a second
            # edit doesn't revert metadata set by the first.
            base_title = (
                pending_version.title if pending_version is not None else instruction.title
            )
            base_load_mode = (
                pending_version.load_mode if pending_version is not None else instruction.load_mode
            )
            new_title = data.title if data.title is not None else base_title
            new_category = data.category if data.category is not None else instruction.category
            if data.load_mode is not None:
                valid_load_modes = {"always", "intelligent"}
                new_load_mode = data.load_mode if data.load_mode in valid_load_modes else "intelligent"
            else:
                new_load_mode = base_load_mode or "always"

            matched_table_names = []
            if data.table_names is not None:
                # Resolve the requested table list to ds_ids + references.
                resolved_ds_ids = set()
                resolved_refs = []
                if data.table_names:
                    conditions = []
                    for name in data.table_names:
                        name_lower = name.lower()
                        if '.' in name:
                            conditions.append(func.lower(DataSourceTable.name) == name_lower)
                        else:
                            conditions.append(func.lower(DataSourceTable.name) == name_lower)
                            conditions.append(func.lower(DataSourceTable.name).like(f'%.{name_lower}'))
                    if conditions:
                        where_clauses = [
                            DataSource.organization_id == str(organization.id),
                            or_(*conditions),
                        ]
                        if allowed_data_source_ids is not None:
                            if not allowed_data_source_ids:
                                # Report has no data sources — skip table resolution.
                                where_clauses.append(DataSource.id.in_([]))
                            else:
                                where_clauses.append(
                                    DataSource.id.in_(list(allowed_data_source_ids))
                                )
                        table_stmt = (
                            select(DataSourceTable)
                            .join(DataSource, DataSourceTable.datasource_id == DataSource.id)
                            .where(*where_clauses)
                        )
                        table_result = await db.execute(table_stmt)
                        tables = table_result.scalars().all()
                        for table in tables:
                            if table.datasource_id:
                                resolved_ds_ids.add(table.datasource_id)
                            resolved_refs.append({
                                "object_type": "datasource_table",
                                "object_id": str(table.id),
                                "column_name": None,
                                "display_text": table.name,
                            })
                            matched_table_names.append(table.name)
                new_data_source_ids = list(resolved_ds_ids)
                new_references_json = resolved_refs
            elif pending_version is not None:
                new_data_source_ids = pending_version.data_source_ids or None
                new_references_json = pending_version.references_json or None
            else:
                new_data_source_ids = [ds.id for ds in (instruction.data_sources or [])] or None
                new_references_json = [
                    {
                        "object_type": ref.object_type,
                        "object_id": ref.object_id,
                        "column_name": ref.column_name,
                        "display_text": ref.display_text,
                    }
                    for ref in (instruction.references or [])
                ] or None

            label_ids = [label.id for label in (instruction.labels or [])] or None
            category_ids = None
            if new_category:
                category_ids = new_category if isinstance(new_category, list) else [new_category]

            version_number = None
            try:
                version = await version_service.create_version_from_data(
                    db=db,
                    instruction_id=str(instruction.id),
                    text=new_text,
                    title=new_title,
                    description=instruction.description,
                    structured_data=instruction.structured_data,
                    formatted_content=instruction.formatted_content,
                    # AI-suggested edit: stage as 'published' so promote_build
                    # flips the live row on approval. The live row's current
                    # status ('draft' for pending suggestions) is preserved
                    # until promotion.
                    status="published",
                    load_mode=new_load_mode,
                    references_json=new_references_json,
                    data_source_ids=new_data_source_ids,
                    label_ids=label_ids,
                    category_ids=category_ids,
                    user_id=user.id if user else None,
                    evidence=clamp_evidence(data.evidence),
                )
                version_number = version.version_number

                # `build` was resolved above (it determines the edit's base).
                if not build or not build.can_be_edited:
                    # No usable draft yet — create one now and write the id
                    # back so subsequent harness tool calls share it.
                    build = await build_service.get_or_create_draft_build(
                        db=db,
                        org_id=str(organization.id),
                        source='ai',
                        user_id=str(user.id) if user else None,
                        agent_execution_id=agent_execution_id,
                    )
                    runtime_ctx["training_build_id"] = str(build.id)
                    training_build_id = str(build.id)
                if build and build.can_be_edited:
                    await build_service.add_to_build(
                        db, build.id, str(instruction.id), version.id
                    )

                await db.commit()
                logger.info(
                    f"Staged edit as version {version.id} (v{version_number}) for instruction {instruction.id} in build {training_build_id}"
                )
            except Exception as e:
                logger.warning(f"Failed to stage edit for instruction {instruction.id}: {e}")

            # Build summary of changes
            changes = []
            if data.text is not None:
                changes.append("text")
            if data.title is not None:
                changes.append("title")
            if data.category is not None:
                changes.append(f"category={data.category}")
            if data.confidence is not None:
                changes.append(f"confidence={data.confidence}")
            if data.load_mode is not None:
                changes.append(f"load_mode={data.load_mode}")
            if data.table_names is not None:
                changes.append(f"tables={matched_table_names}")

            changes_str = ", ".join(changes) if changes else "no changes"
            version_str = f" (v{version_number})" if version_number else ""

            logger.info(f"Edited instruction {instruction.id}{version_str}: {changes_str}")

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=True,
                        instruction_id=str(instruction.id),
                        title=getattr(instruction, "title", None),
                        version_number=version_number,
                        build_id=str(training_build_id) if training_build_id else None,
                        message=f"Instruction updated successfully{version_str}",
                        previous_text=previous_text,
                        new_text=(new_text if data.text is not None else None),
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edited instruction{version_str}: {changes_str}",
                        # The resulting instruction text, so the NEXT anchored
                        # edit has something real to anchor on. Without it the
                        # model is blind after its first edit: the instructions
                        # context section renders the LIVE row, which is not
                        # mutated until promotion, while a second edit in the
                        # same build applies to the PENDING version (see
                        # _load_pending_version). Its anchor then misses,
                        # costing an anchor_not_found round trip — and a model
                        # that cannot anchor reliably reaches for a rewrite.
                        # Mirrors the `current_text` the failure paths return.
                        **({"new_text": new_text} if data.text is not None else {}),
                        "artifacts": [
                            {
                                "type": "instruction_edit",
                                "id": str(instruction.id),
                                "version_number": version_number,
                                "changes": changes,
                                "tables": matched_table_names,
                            }
                        ],
                    },
                }
            )

        except Exception as e:
            logger.exception(f"Failed to edit instruction: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to edit instruction: {str(e)}",
                    "code": "EDIT_FAILED"
                }
            )
