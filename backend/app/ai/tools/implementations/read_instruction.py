"""Read Instruction Tool — pull the full text of a single instruction or skill on demand.

Instructions and skills are advertised in the prompt (<available_skills> and
<available_instructions>) as a compact catalog of short id + title + description,
with their full text withheld. When the agent decides an entry is relevant to
the user's request, it calls this tool with the short id prefix to load the
full text.

Design constraints (chat-only progressive disclosure):
  - Available in chat AND the editing modes (training / knowledge): an
    anchored edit_instruction can only match text the agent has actually
    read, so the read tool has to exist where editing happens.
  - Resolves any published instruction (kind='instruction' or 'skill').
  - Accepts the SHORT id prefix (first part of the UUID), not just the full id.
  - HARD-SCOPED to the current report's agents: the id is verified against the
    report's data sources (or global instructions) and per-user table
    accessibility before any text is returned. When no report scope can be
    resolved, the tool refuses instead of falling back to org-wide reads.
  - Records an 'on_demand' usage event so agent-pulled instructions climb the
    usage-based ranking of the fill/catalog ordering.
"""

from typing import AsyncIterator, Dict, Any, Type, List, Optional, Tuple
from pydantic import BaseModel
import logging

from sqlalchemy import select, and_, func

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.read_instruction import (
    ReadInstructionInput,
    ReadInstructionOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.models.instruction import Instruction
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

logger = logging.getLogger(__name__)

# How the UI labels each stored status. The agent is asked about "the status"
# in the user's words ("is it active?"), so the tool answers in the user's
# vocabulary rather than making the model guess the mapping.
STATUS_LABELS = {
    "published": "active",
    "draft": "inactive",
    "archived": "archived",
}


def _status_label(status: Optional[str]) -> str:
    return STATUS_LABELS.get(status or "", status or "unknown")


class ReadInstructionTool(Tool):
    """Read the full text of one instruction or skill by its short id prefix."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_instruction",
            description=(
                "RESEARCH: Read the full text of a single instruction or skill by "
                "the SHORT id prefix shown in <available_skills> or "
                "<available_instructions>. Entries are listed there with only a "
                "title + short description — their full content is withheld to keep "
                "the prompt small. When a listed entry is relevant to the user's "
                "request, call this with its short id (e.g. 'be8090f2') to load the "
                "full text BEFORE acting on it. Only instructions for this report's "
                "connected data (or global ones) are readable.\n\n"
                "READ BEFORE YOU EDIT. edit_instruction anchors must match the CURRENT "
                "text exactly, so read the instruction here first unless you already have "
                "its text from your own last edit's `new_text`.\n\n"
                "STATUS: `status` ('published' / 'draft' / 'archived', shown in the UI as "
                "Active / Inactive / Archived) and the `active` boolean are the ONLY source "
                "of truth for whether the rule is in effect. A successful read does NOT mean "
                "the instruction is active. If the user asks about an instruction's status, "
                "call this tool NOW rather than answering from an earlier turn — status is "
                "changed outside the conversation and an earlier read goes stale.\n\n"
                "`pending_changes` lists suggested edits already awaiting review — who "
                "proposed each, when, and what it changes. It is INFORMATIONAL: `text` is "
                "the live instruction and the only thing an anchor can match. If a pending "
                "change already does what you were about to propose, say so rather than "
                "stacking a second suggestion on top of it."
            ),
            category="research",
            version="2.0.0",
            input_schema=ReadInstructionInput.model_json_schema(),
            output_schema=ReadInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=True,
            required_permissions=[],
            tags=["instruction", "skill", "read", "knowledge"],
            allowed_modes=["chat", "training", "knowledge"],
            examples=[
                {
                    "input": {"id": "be8090f2"},
                    "description": (
                        "Read an instruction/skill by its short id prefix from "
                        "<available_skills> or <available_instructions>"
                    ),
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadInstructionOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = ReadInstructionInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        prefix = (data.id or "").strip().lower()
        yield ToolStartEvent(type="tool.start", payload={"id": prefix})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")

        if not all([db, organization]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            # --- Resolve the scope: this report's data sources (+ global) ---
            # Guard: without a report context this tool does NOT fall back to
            # org-wide reads — the readable set must equal what could have been
            # advertised to THIS report's agents.
            scope_resolved, data_source_ids = self._resolve_scope(runtime_ctx)
            if not scope_resolved:
                yield self._end_error(
                    "read_instruction is only available within a report session."
                )
                return

            # --- Step 1: resolve the short id prefix to a unique instruction ---
            # Case-insensitive prefix match over published, non-deleted rows of
            # any kind (instruction or skill). In the editing modes the caller's
            # OWN drafts resolve too: an instruction created earlier in this
            # session is status='draft' until its build is promoted, and telling
            # its author "no instruction found" turns every follow-up edit into
            # a dead end (the agent concludes the instruction is gone). Other
            # users' drafts stay invisible — this widens nothing org-wide.
            status_clause = Instruction.status == "published"
            if user is not None and runtime_ctx.get("mode") in ("training", "knowledge"):
                from sqlalchemy import or_
                status_clause = or_(
                    status_clause,
                    and_(
                        Instruction.status == "draft",
                        Instruction.user_id == str(user.id),
                    ),
                )
            stmt = (
                select(
                    Instruction.id,
                    Instruction.title,
                    Instruction.description,
                    Instruction.kind,
                    Instruction.status,
                )
                .where(
                    and_(
                        Instruction.organization_id == organization.id,
                        status_clause,
                        Instruction.deleted_at.is_(None),
                        func.lower(Instruction.id).like(prefix + "%"),
                    )
                )
            )
            result = await db.execute(stmt)
            candidates: List[Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]] = [
                (str(row[0]), row[1], row[2], row[3], row[4]) for row in result.all()
            ]

            if not candidates:
                # The row may exist and simply not be published — that is a
                # different answer from "no such instruction", and conflating
                # them is what makes the agent report a deactivated rule as
                # missing (or, worse, guess that it is still live). Probe for it
                # and name the status. Deliberately no title/text: the caller
                # was not cleared to read a non-published row, only to be told
                # why the read failed.
                probe = await self._probe_non_published(db, organization, prefix)
                if probe:
                    probe_id, probe_status = probe
                    yield self._end_error(
                        f"Instruction {probe_id[:8]} exists but its status is "
                        f"'{probe_status}' ({_status_label(probe_status)}) — it is NOT "
                        f"in effect and its text is not readable here.",
                        status=probe_status,
                    )
                    return
                yield self._end_error(
                    f"No instruction found with id starting '{prefix}'. "
                    f"Use a short id exactly as shown in <available_skills> or "
                    f"<available_instructions>."
                )
                return

            if len(candidates) > 1:
                listing = ", ".join(
                    f"{cid[:8]} ({title or 'untitled'})" for cid, title, _d, _k, _s in candidates[:10]
                )
                yield self._end_error(
                    f"Ambiguous id prefix '{prefix}' matched {len(candidates)} entries: "
                    f"{listing}. Pass a longer prefix to disambiguate."
                )
                return

            full_id, title, description, kind, row_status = candidates[0]

            # --- Step 2: load the full (versioned) text, scoped to this report's
            # data sources, with per-user table accessibility applied. ---
            builder = InstructionContextBuilder(
                db,
                organization,
                current_user=user,
                data_source_ids=data_source_ids,
            )
            items = await builder.load_instructions_by_ids([full_id], load_mode_filter=None)

            if not items and row_status == "draft":
                # A draft is not in the main build, so the builder can't see it.
                # Serve the author's own draft straight from the row (the resolve
                # step already proved ownership) so a session can read back what
                # it created a turn ago.
                row = (await db.execute(
                    select(Instruction).where(Instruction.id == full_id)
                )).unique().scalars().first()
                if row is not None:
                    from types import SimpleNamespace
                    items = [SimpleNamespace(
                        title=row.title, text=row.text or "",
                        category=row.category, load_mode=row.load_mode,
                        source_type="draft",
                    )]

            if not items:
                yield self._end_error(
                    f"Instruction {full_id[:8]} (status '{row_status}') exists but is "
                    f"not available for this report's connected data (or you don't have "
                    f"access to its tables).",
                    status=row_status,
                )
                return

            item = items[0]
            await self._record_on_demand_usage(runtime_ctx, item, full_id)
            pending = await self._pending_changes(db, organization, user, full_id)

            is_active = row_status == "published"
            output = ReadInstructionOutput(
                success=True,
                id=full_id,
                short_id=full_id[:8],
                title=item.title or title,
                description=description,
                text=item.text or "",
                category=item.category,
                kind=kind or "instruction",
                load_mode=item.load_mode,
                status=row_status,
                active=is_active,
                message=(
                    f"Read instruction {full_id[:8]}"
                    + (" (unpublished draft — awaiting review)" if row_status == "draft" else "")
                ),
                pending_changes=pending or None,
            )

            # Status rides in the SUMMARY, not only in the output. The planner is
            # handed the observation, never the output dict, so anything kept out
            # of here is invisible to the model — which is how a deactivated rule
            # came back reading exactly like a live one and got reported as
            # "active" on the strength of the read having succeeded.
            summary = (
                f"Read instruction '{item.title or title or full_id[:8]}' "
                f"[status={row_status or 'unknown'} ({_status_label(row_status)}), "
                f"load_mode={item.load_mode or 'intelligent'}, "
                f"category={item.category or 'general'}]"
            )
            if not is_active:
                summary += " — NOT in effect; its text is shown for reference only"
            if pending:
                summary += (
                    f" — {len(pending)} suggested edit(s) awaiting review "
                    "(informational; anchor on the live text above)"
                )
            output_dict = output.model_dump()
            # Surface the loaded instruction to the UI (instructions.context SSE
            # + completion hydration) via the shared related_instructions hook.
            output_dict["related_instructions"] = [
                {
                    "id": full_id,
                    "title": item.title or title,
                    "category": item.category,
                    "load_mode": item.load_mode,
                    "source_type": "on_demand",
                }
            ]
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output_dict,
                    "observation": {
                        "summary": summary,
                        "text": item.text or "",
                        "status": row_status,
                        "active": is_active,
                        "load_mode": item.load_mode,
                        "category": item.category,
                        # Nested, never inlined with the body: pending text is
                        # NOT part of the instruction and an anchor matched
                        # against it will fail. Keeping it under its own key is
                        # what stops it reading as more instruction prose.
                        **({"pending_changes": pending} if pending else {}),
                        "artifacts": [
                            {
                                "type": "instruction_read_result",
                                "id": full_id,
                                "title": item.title or title,
                                "kind": kind or "instruction",
                                "status": row_status,
                                "active": is_active,
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"read_instruction failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Read failed: {e}", "code": "READ_FAILED"},
            )

    @staticmethod
    async def _pending_changes(db, organization, user, instruction_id: str) -> list:
        """Suggested edits awaiting review on this instruction, newest first.

        Goes through InstructionService.review_hunks, which applies the same
        user_can_view_instruction check the review UI does — deliberately, so
        the agent can describe exactly what the user could already see in the
        Knowledge Explorer, and nothing more. Querying draft builds directly
        would leak: builds are org-scoped while instruction visibility is not.

        Summarised, not dumped: an instruction can carry several suggestions and
        the full proposed document for each would swamp the observation. Each
        entry carries who/when/why plus a short preview per hunk.

        Best-effort — a failure here must not fail the read.
        """
        if user is None:
            return []
        try:
            from app.services.instruction_service import InstructionService

            review = await InstructionService().review_hunks(
                db, instruction_id, organization=organization, current_user=user
            )
        except Exception:
            logger.warning("read_instruction: pending-changes lookup failed", exc_info=True)
            return []
        if not review:
            return []

        out = []
        for s in review.get("suggestions", []):
            hunks = s.get("hunks") or []
            out.append({
                "build_id": s.get("build_id"),
                "build_number": s.get("build_number"),
                "proposed_by": (s.get("created_by") or {}).get("name"),
                "source": s.get("source"),
                "proposed_at": s.get("created_at"),
                "evidence": s.get("evidence"),
                "change_count": len(hunks),
                "changes": [
                    {
                        "replaces": (h.get("before") or "")[:120],
                        "with": (h.get("after") or "")[:120],
                    }
                    for h in hunks[:5]
                ],
            })
        return out

    @staticmethod
    async def _record_on_demand_usage(runtime_ctx: Dict[str, Any], item, full_id: str) -> None:
        """Record an 'on_demand' usage event (best-effort) so pulled
        instructions climb the usage-based catalog/fill ordering."""
        try:
            from app.services.instruction_usage_service import InstructionUsageService
            from app.schemas.instruction_usage_schema import InstructionUsageEventCreate

            db = runtime_ctx.get("db")
            organization = runtime_ctx.get("organization")
            user = runtime_ctx.get("user")
            report = runtime_ctx.get("report")
            await InstructionUsageService().record_usage_event(
                db,
                InstructionUsageEventCreate(
                    org_id=str(organization.id),
                    report_id=str(report.id) if report is not None else None,
                    instruction_id=full_id,
                    user_id=str(user.id) if user is not None else None,
                    load_mode=item.load_mode or "intelligent",
                    load_reason="on_demand",
                    source_type=item.source_type,
                    category=item.category,
                    title=item.title,
                ),
            )
        except Exception:
            logger.debug("read_instruction: usage event failed", exc_info=True)

    @staticmethod
    def _resolve_scope(runtime_ctx: Dict[str, Any]) -> Tuple[bool, Optional[List[str]]]:
        """Derive the report's data-source scope for per-id verification.

        Returns (resolved, data_source_ids):
        - resolved=False → no report context at all; the caller REFUSES the
          read (no org-wide fallback).
        - resolved=True, ds_ids=list → readable = instructions on those data
          sources or global ones (mirrors what the catalog advertised).
        - resolved=True, ds_ids=None → the report/hub itself is unscoped (its
          catalog advertised org-wide), so reads mirror that.

        Prefer the context hub's instruction builder (already constructed with
        the report's data sources); fall back to the report's own data sources.
        """
        context_hub = runtime_ctx.get("context_hub")
        if context_hub is not None:
            ib = getattr(context_hub, "instruction_builder", None)
            if ib is not None:
                return True, getattr(ib, "data_source_ids", None)
            data_sources = getattr(context_hub, "data_sources", None)
            if data_sources is not None:
                return True, [str(ds.id) for ds in data_sources]

        report = runtime_ctx.get("report")
        if report is not None:
            try:
                return True, [str(ds.id) for ds in (report.data_sources or [])]
            except Exception:
                # Data sources not loadable — default to global-only, never org-wide.
                return True, []
        return False, None

    @staticmethod
    async def _probe_non_published(
        db, organization, prefix: str
    ) -> Optional[Tuple[str, str]]:
        """Resolve `prefix` against rows the main query deliberately excluded.

        Only ever used to explain a failed read. "No instruction found" and
        "the instruction is switched off" are opposite answers for the user,
        and returning the first for the second is what let a deactivated rule
        be described as if it were still live.

        Returns (id, status) for a UNIQUE non-published, non-deleted match, else
        None. Ambiguous prefixes fall through to the generic message. Nothing
        about the row's content is returned — the caller was not cleared to read
        it, only to be told why it could not.
        """
        try:
            rows = (await db.execute(
                select(Instruction.id, Instruction.status).where(
                    and_(
                        Instruction.organization_id == organization.id,
                        Instruction.status != "published",
                        Instruction.deleted_at.is_(None),
                        func.lower(Instruction.id).like(prefix + "%"),
                    )
                )
            )).all()
        except Exception:
            logger.warning("read_instruction: status probe failed", exc_info=True)
            return None
        if len(rows) != 1:
            return None
        return str(rows[0][0]), rows[0][1] or "unknown"

    @staticmethod
    def _end_error(message: str, *, status: Optional[str] = None) -> ToolEndEvent:
        """A 'soft' failure surfaced as a normal observation so the agent can
        adjust (pass a longer/correct id) rather than treating it as a hard error."""
        active = (status == "published") if status else None
        output = ReadInstructionOutput(
            success=False, message=message, status=status, active=active,
        )
        observation = {"summary": message, "artifacts": []}
        if status:
            observation["status"] = status
            observation["active"] = active
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": observation,
            },
        )
