"""Search Instructions Tool - Discover existing instructions before creating/editing.

Used primarily by the knowledge harness phase to check for duplicate or related
instructions before creating new ones. Also available in training mode.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.search_instructions import (
    SearchInstructionsInput,
    SearchInstructionsOutput,
    SearchInstructionsItem,
    PendingEdit,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

# Builds whose contents are still awaiting a review decision.
PENDING_BUILD_STATUSES = ("draft", "pending_approval")

# The delta is advisory context shown per search hit, so keep it short.
MAX_PENDING_DELTA_LENGTH = 200


def _pending_delta(live_text: str, staged_text: str) -> str | None:
    """What ``staged_text`` adds on top of ``live_text``, clamped.

    Appends are the common shape (the harness edits additively), so the cheap
    prefix check handles most cases; anything else falls back to a clamped
    view of the staged text.
    """
    live = (live_text or "").strip()
    staged = (staged_text or "").strip()
    if not staged or staged == live:
        return None
    delta = staged[len(live):].strip() if live and staged.startswith(live) else staged
    if not delta:
        return None
    if len(delta) > MAX_PENDING_DELTA_LENGTH:
        delta = delta[: MAX_PENDING_DELTA_LENGTH - 1].rstrip() + "…"
    return delta


async def _load_pending_edits(db, live_text_by_id: dict, current_build_id=None) -> dict:
    """Map instruction_id -> PendingEdit for unapproved staged versions.

    One batched query over the (already narrowed) result set. When several
    builds stage the same instruction, the one belonging to this session wins
    so the harness is told it can safely stack onto it.
    """
    ids = [i for i in live_text_by_id if i]
    if not ids:
        return {}
    try:
        from sqlalchemy import select
        from app.models.build_content import BuildContent
        from app.models.instruction_version import InstructionVersion
        from app.models.instruction_build import InstructionBuild

        rows = (await db.execute(
            select(BuildContent.instruction_id, InstructionVersion, InstructionBuild.id)
            .join(
                InstructionVersion,
                BuildContent.instruction_version_id == InstructionVersion.id,
            )
            .join(InstructionBuild, BuildContent.build_id == InstructionBuild.id)
            .where(
                BuildContent.instruction_id.in_(ids),
                InstructionBuild.status.in_(PENDING_BUILD_STATUSES),
            )
        )).all()

        out: dict = {}
        for instruction_id, version, build_id in rows:
            key = str(instruction_id)
            is_current = bool(current_build_id) and str(build_id) == str(current_build_id)
            # A suggestion from this session takes precedence — it is the one
            # the harness may safely stack onto.
            if key in out and not is_current:
                continue
            out[key] = PendingEdit(
                build_id=str(build_id),
                version_number=getattr(version, "version_number", None),
                is_current_session=is_current,
                evidence=getattr(version, "evidence", None),
                delta=_pending_delta(
                    live_text_by_id.get(key, ""), getattr(version, "text", "") or ""
                ),
            )
        return out
    except Exception as e:
        # Advisory context only — never fail the search over it.
        logger.warning(f"Failed to load pending instruction edits: {e}")
        return {}


class SearchInstructionsTool(Tool):
    """Search/list existing instructions to find duplicates or related entries
    before creating or editing instructions.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_instructions",
            description=(
                "RESEARCH: Search existing organization instructions by a list of "
                "keyword OR regex queries (case-insensitive, unioned). Each `query` "
                "entry can be a plain keyword/phrase (matched as a literal substring) "
                "or a regex pattern (auto-detected by regex metacharacters, e.g. "
                "`revenue\\s*>\\s*\\$?\\d+`, `.*cancel.*`). "
                "In training/knowledge mode: use BEFORE create_instruction to check "
                "for duplicates or to find an existing instruction to edit (returns "
                "full text). In chat mode: use when you suspect a rule exists that "
                "is not loaded and not listed in <available_instructions> — results "
                "are compact (title + snippet, scoped to this report's data); call "
                "read_instruction with the id to load the full text. "
                "Cast a wide net: pass 3-6 queries in ONE call covering different "
                "angles of the topic.\n\n"
                "PENDING EDITS: a hit may carry `pending_edit` — an unapproved "
                "suggestion already staged against it, NOT reflected in `text`. "
                "When `is_current_session` is true it came from this session and "
                "your edit stacks on it safely. When false, a separate review is "
                "already pending: do NOT re-propose the same learning there."
            ),
            category="research",
            version="1.2.0",
            input_schema=SearchInstructionsInput.model_json_schema(),
            output_schema=SearchInstructionsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["instruction", "search", "knowledge"],
            allowed_modes=["training", "knowledge", "chat"],
            examples=[
                {
                    "input": {"query": ["revenue"], "limit": 10},
                    "description": "Simple single-keyword lookup",
                },
                {
                    "input": {
                        "query": ["album revenue", "invoiceline", "sales", "black-elephant", "revenue threshold"],
                        "limit": 20,
                    },
                    "description": (
                        "Thorough multi-angle search — preferred when checking whether "
                        "a clarified term is already captured"
                    ),
                },
                {
                    "input": {"query": [r"revenue\s*>\s*\$?\d+", r"\b(album|track)_revenue\b"], "limit": 20},
                    "description": "Regex queries (auto-detected by metacharacters)",
                },
                {
                    "input": {
                        "query": ["cancelled order", "refund", ".*cancel.*"],
                        "category": "code_gen",
                    },
                    "description": "Mixed literal + regex queries scoped to code-gen instructions",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchInstructionsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchInstructionsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SearchInstructionsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "query": data.query,
                "category": data.category,
                "limit": data.limit,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            import re
            from app.services.instruction_service import InstructionService

            service = InstructionService()
            categories = [data.category] if data.category else None

            # --- Chat mode: forced report scope, published-only, compact output ---
            # In chat the agent-supplied data_source_ids are IGNORED — the scope
            # is the report's data sources (mirroring what the instruction
            # catalog advertises). Without a report context the tool refuses
            # rather than searching org-wide.
            chat_mode = runtime_ctx.get("mode") == "chat"
            effective_ds_ids = data.data_source_ids
            if chat_mode:
                from app.ai.tools.implementations.read_instruction import ReadInstructionTool
                scope_resolved, scope_ds_ids = ReadInstructionTool._resolve_scope(runtime_ctx)
                if not scope_resolved:
                    output = SearchInstructionsOutput(
                        success=False,
                        instructions=[],
                        total=0,
                        message="search_instructions is only available within a report session.",
                    )
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": output.model_dump(),
                            "observation": {"summary": output.message, "artifacts": []},
                        },
                    )
                    return
                effective_ds_ids = scope_ds_ids

            # --- Resolve each query into a compiled pattern ---
            # Mirrors describe_tables: literal substrings are escaped and wrapped
            # as case-insensitive regexes; entries with regex metacharacters are
            # also compiled as raw patterns (best-effort — invalid regex is
            # silently skipped and the literal form still matches).
            queries: list[str] = [q for q in (data.query or []) if isinstance(q, str) and q.strip()]

            special = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
            compiled_patterns: list[re.Pattern] = []
            pattern_errors: list[str] = []
            for q in queries:
                stripped = q.strip()
                # Always include a case-insensitive literal substring match.
                try:
                    compiled_patterns.append(re.compile(re.escape(stripped), re.IGNORECASE))
                except re.error:
                    pass
                # Also compile as raw regex if it looks like one.
                if special.search(stripped):
                    try:
                        compiled_patterns.append(re.compile(stripped, re.IGNORECASE))
                    except re.error as re_err:
                        pattern_errors.append(f"'{stripped}': {re_err}")

            # --- Fetch candidate window ---
            # We intentionally pull a broad window once (no DB-level LIKE) and
            # apply all patterns in-process. This matches the describe_tables
            # approach and keeps the hot path simple: one SQL round-trip regardless
            # of how many queries the agent passes.
            window = max(data.limit * 5, 100) if compiled_patterns else data.limit

            result = await service.get_instructions(
                db=db,
                organization=organization,
                current_user=user,
                skip=0,
                limit=window,
                status="published",
                categories=categories,
                data_source_ids=effective_ds_ids,
                search=None,
                include_global=True,
            )

            candidates = result.get("items", []) if isinstance(result, dict) else []
            candidate_total = result.get("total", len(candidates)) if isinstance(result, dict) else len(candidates)

            # Chat mode never surfaces drafts and applies per-user table
            # accessibility (readable set == advertisable set).
            if chat_mode and candidates:
                from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
                _builder = InstructionContextBuilder(
                    db, organization, current_user=user, data_source_ids=effective_ds_ids,
                )
                candidates = await _builder._filter_items_by_table_accessibility(candidates)
                candidate_total = len(candidates)

            # Also include instructions from the current draft build so the
            # harness can see instructions it created earlier in this session.
            # No status filter here: AI-created instructions are status='draft'
            # until the build is promoted, but the agent still needs to see and
            # edit them within the same session.
            training_build_id = runtime_ctx.get("training_build_id")
            if training_build_id and not chat_mode:
                draft_result = await service.get_instructions(
                    db=db,
                    organization=organization,
                    current_user=user,
                    skip=0,
                    limit=window,
                    status=None,
                    categories=categories,
                    data_source_ids=data.data_source_ids,
                    search=None,
                    include_global=True,
                    build_id=training_build_id,
                )
                draft_items = draft_result.get("items", []) if isinstance(draft_result, dict) else []
                if draft_items:
                    seen_ids = {getattr(c, "id", None) for c in candidates}
                    for item in draft_items:
                        if getattr(item, "id", None) not in seen_ids:
                            candidates.append(item)
                    candidate_total += len(draft_items)

            # The caller's OWN drafts, from ANY turn. Builds are per agent
            # execution, so the block above only covers instructions created in
            # THIS turn — an instruction created two turns ago is status='draft'
            # in some other build and invisible to the published-only query,
            # which read as "the instruction disappeared" to the agent that had
            # just created it. Only the author's drafts: no org-wide widening.
            if not chat_mode:
                from sqlalchemy import select as _select, and_ as _and
                from app.models.instruction import Instruction as _Instruction
                own_drafts = (await db.execute(
                    _select(_Instruction).where(_and(
                        _Instruction.organization_id == organization.id,
                        _Instruction.user_id == str(user.id),
                        _Instruction.status == "draft",
                        _Instruction.deleted_at.is_(None),
                    )).limit(window)
                )).unique().scalars().all()
                if own_drafts:
                    seen_ids = {str(getattr(c, "id", None)) for c in candidates}
                    for row in own_drafts:
                        if str(row.id) not in seen_ids:
                            candidates.append(row)
                            candidate_total += 1

            # --- Apply patterns (union) ---
            # Haystack includes the instruction id so UUID/fragment queries
            # work — agents often pass partial ids (e.g. "be8090") expecting
            # an id-aware lookup, and without this they silently match nothing.
            if compiled_patterns:
                matched: list = []
                for it in candidates:
                    haystack = (
                        (getattr(it, "text", "") or "") + "\n" +
                        (getattr(it, "title", "") or "") + "\n" +
                        str(getattr(it, "id", "") or "")
                    )
                    if any(p.search(haystack) for p in compiled_patterns):
                        matched.append(it)
                items = matched[: data.limit]
                total = len(matched)
            else:
                items = candidates[: data.limit]
                total = candidate_total

            def _snippet(text: str, max_len: int = 140) -> str:
                collapsed = " ".join((text or "").split())
                return collapsed[: max_len - 1] + "…" if len(collapsed) > max_len else collapsed

            # --- Attach unapproved staged edits ---
            # Edits never touch the live row (promotion does), so the text above
            # hides any pending suggestion — including ones this session staged.
            # Surfacing them stops the harness re-proposing a learning that is
            # already awaiting review in another build, which would duplicate the
            # review and can overwrite it on approval. Batched over the narrowed
            # result set (<= limit) so the wide candidate window costs nothing.
            pending_by_instruction = {}
            if items and not chat_mode:
                pending_by_instruction = await _load_pending_edits(
                    db,
                    {
                        str(getattr(it, "id", "")): (getattr(it, "text", "") or "")
                        for it in items
                    },
                    current_build_id=runtime_ctx.get("training_build_id"),
                )

            search_items = []
            for it in items:
                full_text = getattr(it, "text", "") or ""
                search_items.append(
                    SearchInstructionsItem(
                        id=str(getattr(it, "id", "")),
                        title=getattr(it, "title", None),
                        # Chat mode is compact (progressive disclosure): title +
                        # snippet only; the agent calls read_instruction for the
                        # full text. Training/knowledge keep the full text.
                        text=_snippet(full_text) if chat_mode else full_text,
                        category=getattr(it, "category", None),
                        load_mode=getattr(it, "load_mode", None),
                        status=getattr(it, "status", None),
                        pending_edit=pending_by_instruction.get(
                            str(getattr(it, "id", ""))
                        ),
                    )
                )

            msg = f"Found {len(search_items)} instruction(s) (total matching: {total})"
            _other_pending = sum(
                1 for i in search_items
                if i.pending_edit and not i.pending_edit.is_current_session
            )
            if _other_pending:
                msg = (
                    f"{msg}. {_other_pending} already have an unapproved suggestion "
                    "pending review in another build — do not re-propose those."
                )
            if chat_mode and search_items:
                msg = f"{msg}. Results are snippets — call read_instruction with an id for full text."
            if pattern_errors:
                msg = f"{msg}. Invalid regex(es) skipped: {'; '.join(pattern_errors)}"

            output = SearchInstructionsOutput(
                success=True,
                instructions=search_items,
                total=total,
                message=msg,
            )

            summary = (
                f"Found {len(search_items)} instruction(s) matching "
                f"query={queries} category='{data.category or ''}'"
            )

            output_dict = output.model_dump()
            output_dict["related_instructions"] = [
                {
                    "id": i.id,
                    "title": i.title,
                    "category": i.category,
                    "load_mode": i.load_mode,
                    "source_type": "search",
                }
                for i in search_items
            ]

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output_dict,
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "instruction_search_result",
                                "count": len(search_items),
                                "total": total,
                                "items": [
                                    {
                                        "id": i.id,
                                        "title": i.title,
                                        "category": i.category,
                                    }
                                    for i in search_items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"search_instructions failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Search failed: {e}",
                    "code": "SEARCH_FAILED",
                },
            )
