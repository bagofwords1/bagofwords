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
import json
import logging
import re

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
from app.ai.tools.implementations._text_window import (
    DEFAULT_RANGE_LIMIT,
    grep_lines,
    outline_from_patterns,
)

logger = logging.getLogger(__name__)

# ─── Long-instruction windowing ──────────────────────────────────────────────
# Budgets are measured on the JSON-encoded text (ASCII-escaped), because that is
# what the transcript sizes and cuts at its per-result cap (100k chars): a
# newline costs 2 there and a non-ASCII character 6. Keeping every read under
# these budgets means a read is never silently cut on its way to the model.
FULL_READ_MAX_ENCODED_CHARS = 40_000   # plain read returns the whole body up to this
HEAD_MAX_ENCODED_CHARS = 16_000        # opening lines shown when the body is too long
WINDOW_MAX_ENCODED_CHARS = 40_000      # cap for one range read / one grep read
OUTLINE_MAX_ENCODED_CHARS = 20_000
OUTLINE_MAX_ENTRIES = 200

# Landmarks in instruction bodies: markdown headings, YAML/`key:` lines at the
# top level, nested map headers (`  some_event:`), and list entries that name
# themselves (`- name: ...`).
_OUTLINE_PATTERNS = [
    re.compile(r"^#{1,6}\s+\S"),
    re.compile(r"^[\w\"'][^:#]{0,100}:(\s|$)"),
    re.compile(r"^\s{1,4}[\w\"'.-][^:#]{0,100}:\s*$"),
    re.compile(r"^\s*-\s+(?:name|event|event_name|id|key|title)\s*:", re.IGNORECASE),
]


def _encoded_len(value: Any) -> int:
    return len(json.dumps(value, default=str))


def _window(lines: List[str], start: int, max_lines: int, budget: int) -> Tuple[str, int, bool]:
    """Lines from 1-based `start`, at most `max_lines`, within `budget`.

    Returns (text, end_line, truncated). Always returns at least one line so a
    caller paging with next_offset makes progress; a single line larger than
    the budget is clipped (rare: one giant line).
    """
    total = len(lines)
    out: List[str] = []
    used = 2  # the JSON string quotes
    idx = start - 1
    while idx < total and len(out) < max_lines:
        cost = _encoded_len(lines[idx]) - 2 + (2 if out else 0)  # + escaped newline
        if out and used + cost > budget:
            break
        line = lines[idx]
        if not out and used + cost > budget:
            # Worst case 6 encoded chars per character.
            line = line[: max(1, (budget - used) // 6)]
            cost = _encoded_len(line) - 2
        out.append(line)
        used += cost
        idx += 1
    end_line = start + len(out) - 1
    return "\n".join(out), end_line, end_line < total


def _bounded_outline(text: str) -> str:
    outline = outline_from_patterns(
        text, _OUTLINE_PATTERNS, max_entries=OUTLINE_MAX_ENTRIES, entry_clip=120
    )
    if _encoded_len(outline) <= OUTLINE_MAX_ENCODED_CHARS:
        return outline
    kept: List[str] = []
    used = 2
    for entry in outline.split("\n"):
        cost = _encoded_len(entry)
        if used + cost > OUTLINE_MAX_ENCODED_CHARS:
            kept.append("     … outline cut to fit — use grep_pattern to locate entries")
            break
        kept.append(entry)
        used += cost
    return "\n".join(kept)


def _bounded_matches(matches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Drop trailing matches past WINDOW_MAX_ENCODED_CHARS. Returns (kept, dropped_any)."""
    kept: List[Dict[str, Any]] = []
    used = 2
    for m in matches:
        cost = _encoded_len(m)
        if kept and used + cost > WINDOW_MAX_ENCODED_CHARS:
            return kept, True
        kept.append(m)
        used += cost
    return kept, False


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
                "`pending_changes` lists suggested edits already awaiting review — who "
                "proposed each, when, and what it changes. It is INFORMATIONAL: `text` is "
                "the live instruction and the only thing an anchor can match. If a pending "
                "change already does what you were about to propose, say so rather than "
                "stacking a second suggestion on top of it.\n\n"
                "LONG INSTRUCTIONS (catalogues, event lists, glossaries): a plain read of a "
                "body too long to return whole comes back with `truncated: true`, the opening "
                "lines, and a line-numbered `outline`. Never treat that as the full text. Use "
                "`grep_pattern` (regex per line, e.g. 'connect|pair|weight', with "
                "`before`/`after` context) to pull the entries you need, or `offset`/`limit` "
                "to read a line range; continue from `next_offset`. Both return verbatim "
                "lines, safe to anchor edit_instruction on."
            ),
            category="research",
            version="2.1.0",
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
                {
                    "input": {"id": "be8090f2", "grep_pattern": "connect|pair", "after": 3},
                    "description": "Pull matching entries (plus 3 lines of context) out of a long instruction",
                },
                {
                    "input": {"id": "be8090f2", "offset": 401, "limit": 400},
                    "description": "Read lines 401-800 of a long instruction",
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

        if data.grep_pattern:
            try:
                re.compile(data.grep_pattern)
            except re.error as e:
                yield self._end_error(
                    f"Invalid grep_pattern regex: {e}. Fix the pattern and retry."
                )
                return

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
                    f"Instruction {full_id[:8]} exists but is not available for this "
                    f"report's connected data (or you don't have access to its tables)."
                )
                return

            item = items[0]
            await self._record_on_demand_usage(runtime_ctx, item, full_id)
            # Hand smart instructions the agent chose to read to the tool-less
            # consumers (coder, chart step), which can't call this tool.
            context_hub = runtime_ctx.get("context_hub")
            if (
                context_hub is not None
                and (kind or "instruction") != "skill"
                and getattr(item, "load_mode", None) == "intelligent"
                and getattr(item, "id", None)
                and hasattr(context_hub, "record_instruction_read")
            ):
                context_hub.record_instruction_read(item)
            pending = await self._pending_changes(db, organization, user, full_id)

            full_text = item.text or ""
            display_title = item.title or title or full_id[:8]
            window = self._window_read(data, full_text, display_title)

            output = ReadInstructionOutput(
                success=True,
                id=full_id,
                short_id=full_id[:8],
                title=item.title or title,
                description=description,
                text=window["text"],
                category=item.category,
                kind=kind or "instruction",
                load_mode=item.load_mode,
                message=(
                    f"Read instruction {full_id[:8]}"
                    + (" (unpublished draft — awaiting review)" if row_status == "draft" else "")
                ),
                pending_changes=pending or None,
                **window["fields"],
            )

            summary = window["summary"]
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
                        "text": window["text"],
                        # Window metadata (line range, outline, grep matches)
                        # rides with the text so the model can page on.
                        **window["fields"],
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
    def _window_read(data: ReadInstructionInput, full_text: str, title: str) -> Dict[str, Any]:
        """Pick the read mode and return {"text", "fields", "summary"}.

        grep > range > full; a plain read of a body over the budget returns the
        opening lines plus an outline ('head') instead of the whole text. Full
        reads return exactly what they always did (no extra fields).
        """
        total_chars = len(full_text)
        lines = full_text.split("\n")
        total_lines = len(lines)
        base = {"total_chars": total_chars, "total_lines": total_lines}

        if data.grep_pattern:
            matches, total, more = grep_lines(
                full_text,
                data.grep_pattern,
                ignore_case=data.ignore_case,
                before=data.before,
                after=data.after,
                max_matches=data.max_matches,
            )
            matches, dropped = _bounded_matches(matches)
            fields = {
                **base,
                "read_mode": "grep",
                "matches": matches,
                "grep_total_matches": total,
                "grep_truncated": more or dropped,
            }
            if total == 0:
                fields["outline"] = _bounded_outline(full_text)
                summary = (
                    f"0 matches for {data.grep_pattern!r} in instruction '{title}' "
                    f"({total_lines:,} lines). An outline with line numbers is included — "
                    "pick a range (offset/limit) or a better pattern."
                )
            else:
                summary = f"{total} match(es) for {data.grep_pattern!r} in instruction '{title}'"
                if fields["grep_truncated"]:
                    summary += f", showing {len(matches)} — narrow the pattern or raise max_matches for the rest"
                summary += ". Matched lines and context are verbatim."
            return {"text": "", "fields": fields, "summary": summary}

        if data.offset is not None or data.limit is not None:
            start = data.offset or 1
            if start > total_lines:
                fields = {**base, "read_mode": "range", "start_line": start,
                          "end_line": start - 1, "truncated": False}
                summary = (
                    f"offset {start} is past the end of instruction '{title}' "
                    f"({total_lines:,} lines). Retry with a smaller offset."
                )
                return {"text": "", "fields": fields, "summary": summary}
            text, end, truncated = _window(
                lines, start, data.limit or DEFAULT_RANGE_LIMIT, WINDOW_MAX_ENCODED_CHARS
            )
            fields = {**base, "read_mode": "range", "start_line": start,
                      "end_line": end, "truncated": truncated}
            summary = f"Read instruction '{title}' lines {start}-{end} of {total_lines:,}"
            if truncated:
                fields["next_offset"] = end + 1
                summary += f" — continue with offset={end + 1}"
            return {"text": text, "fields": fields, "summary": summary + "."}

        if _encoded_len(full_text) <= FULL_READ_MAX_ENCODED_CHARS:
            return {"text": full_text, "fields": {}, "summary": f"Read instruction '{title}'"}

        text, end, _ = _window(lines, 1, total_lines, HEAD_MAX_ENCODED_CHARS)
        fields = {
            **base,
            "read_mode": "head",
            "start_line": 1,
            "end_line": end,
            "truncated": True,
            "next_offset": end + 1,
            "outline": _bounded_outline(full_text),
        }
        summary = (
            f"Instruction '{title}' is too long to return whole ({total_chars:,} chars, "
            f"{total_lines:,} lines): showing lines 1-{end} plus an outline. "
            "Do NOT treat this as the complete text. Use grep_pattern to pull the entries "
            f"you need, or offset={end + 1} to read on."
        )
        return {"text": text, "fields": fields, "summary": summary}

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
            fields = s.get("field_changes") or []
            out.append({
                "build_id": s.get("build_id"),
                "build_number": s.get("build_number"),
                "proposed_by": (s.get("created_by") or {}).get("name"),
                "source": s.get("source"),
                "proposed_at": s.get("created_at"),
                "evidence": s.get("evidence"),
                "change_count": len(hunks) + len(fields),
                "changes": [
                    {
                        "replaces": (h.get("before") or "")[:120],
                        "with": (h.get("after") or "")[:120],
                    }
                    for h in hunks[:5]
                ],
                # Non-text proposals, e.g. turning the instruction off.
                **({"field_changes": [
                    {"field": f.get("field"), "from": f.get("from"), "to": f.get("to")}
                    for f in fields
                ]} if fields else {}),
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
    def _end_error(message: str) -> ToolEndEvent:
        """A 'soft' failure surfaced as a normal observation so the agent can
        adjust (pass a longer/correct id) rather than treating it as a hard error."""
        output = ReadInstructionOutput(success=False, message=message)
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {"summary": message, "artifacts": []},
            },
        )
