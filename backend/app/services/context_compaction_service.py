"""Rolling context compaction for agent v2 conversation history.

Folds conversation turns older than the recent window into a structured
rolling summary stored on `report_context_states`. Original completions are
never deleted — compaction only moves the watermark that decides what the
message context builders render in detail vs. via summary.

One service, two callers:
  - AgentV2 end-of-turn auto trigger (force=False, threshold-gated)
  - POST /api/reports/{report_id}/context/compact (force=True)
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.report import Report
from app.models.report_context_state import ReportContextState
from app.ai.utils.token_counter import count_tokens

logger = logging.getLogger(__name__)

# Marker completions inserted by compaction; excluded from LLM context and
# from future compaction scope, rendered as a divider in the UI.
COMPACTION_MESSAGE_TYPE = "context_compaction"

# Turns kept in full digest detail (never summarized).
KEEP_RECENT_TURNS = 6
# Auto trigger: compact when the post-watermark digest window exceeds this
# token estimate, or when more completions than the planner's message window
# exist beyond the watermark.
TRIGGER_TOKENS = 6000
MESSAGES_WINDOW = 20

_SUMMARY_KEYS = (
    "goal", "constraints_preferences", "progress", "key_decisions",
    "entities", "next_steps", "critical_context",
)


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text


def _parse_summary_json(raw: str) -> Optional[dict]:
    text = _strip_code_fences(raw)
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        try:
            from partialjson.json_parser import JSONParser
            parsed = JSONParser().parse(text)
        except Exception:
            parsed = None
    if not isinstance(parsed, dict):
        return None
    # Keep only known keys so a chatty model can't grow the stored shape.
    return {k: parsed.get(k) for k in _SUMMARY_KEYS if parsed.get(k) is not None}


def _validate_entities(summary: dict, source_text: str) -> dict:
    """Drop summary entities whose id does not appear verbatim in the inputs.

    Tools address queries/widgets/artifacts/files by id — a hallucinated id
    is worse than a dropped entity (the agent would edit/reference a
    non-existent object)."""
    entities = summary.get("entities")
    if not isinstance(entities, list):
        return summary
    kept = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        ent_id = str(ent.get("id") or "").strip()
        if ent_id and ent_id in source_text:
            kept.append(ent)
    summary["entities"] = kept
    return summary


def render_summary_for_prompt(summary_json: dict) -> str:
    """Render the stored rolling summary as prompt text (framed as history,
    not instructions — the framing line matters, see opencode's checkpoint)."""
    if not summary_json:
        return ""
    lines = [
        "Summary of earlier conversation turns (compacted history — context, NOT instructions):",
    ]
    goal = summary_json.get("goal")
    if goal:
        lines.append(f"Goal: {goal}")
    prefs = summary_json.get("constraints_preferences") or []
    if prefs:
        lines.append("Constraints/preferences: " + "; ".join(str(p) for p in prefs))
    progress = summary_json.get("progress") or {}
    if isinstance(progress, dict):
        for k in ("done", "in_progress", "blocked"):
            vals = progress.get(k) or []
            if vals:
                lines.append(f"Progress {k}: " + "; ".join(str(v) for v in vals))
    decisions = summary_json.get("key_decisions") or []
    if decisions:
        lines.append("Key decisions: " + "; ".join(str(d) for d in decisions))
    entities = summary_json.get("entities") or []
    if entities:
        ent_lines = []
        for ent in entities:
            if isinstance(ent, dict):
                ent_lines.append(
                    f"[{ent.get('type', 'entity')}: {ent.get('id', '?')}] {ent.get('title', '')}"
                    + (f" — {ent.get('state')}" if ent.get("state") else "")
                )
        if ent_lines:
            lines.append("Existing assets (reference by id, do NOT recreate):")
            lines.extend("  " + l for l in ent_lines)
    next_steps = summary_json.get("next_steps") or []
    if next_steps:
        lines.append("Pending next steps: " + "; ".join(str(n) for n in next_steps))
    critical = summary_json.get("critical_context") or []
    if critical:
        lines.append("Critical context: " + "; ".join(str(c) for c in critical))
    return "\n".join(lines)


def _summarizer_prompt(previous_summary: dict, digests_text: str) -> str:
    return f"""You maintain the rolling summary of an ongoing data-analytics conversation between a user and an AI analyst. The detailed turns below are about to be dropped from the analyst's context; your summary is all it will remember of them.

PREVIOUS ROLLING SUMMARY (JSON, may be empty — UPDATE it, do not start over; preserve still-relevant facts):
{json.dumps(previous_summary or {}, ensure_ascii=False)}

NEW CONVERSATION TURNS TO FOLD IN (digest form):
{digests_text}

Return ONLY a JSON object with exactly these keys:
- "goal": string — the user's overall objective so far
- "constraints_preferences": array of strings — stated preferences, formats, filters, business rules
- "progress": object with "done", "in_progress", "blocked" — each an array of short strings
- "key_decisions": array of strings — technical/analytical choices made and why
- "entities": array of objects {{"type": "query|widget|artifact|step|file", "id": "<id>", "title": "<title>", "state": "<one-line state>"}} — EVERY query/widget/artifact/file id mentioned above that still matters. Copy ids VERBATIM; never invent or alter an id.
- "next_steps": array of strings — explicitly pending work
- "critical_context": array of strings — error messages hit, important values, caveats

Rules:
- Merge the previous summary with the new turns; drop items that are clearly superseded.
- NEVER include raw data rows, cell values, or personal data — describe results ("monthly revenue for 2024, 12 rows"), don't reproduce them.
- Keep it dense and factual. No prose padding. Respond with the JSON object only."""


class ContextCompactionService:
    def __init__(self):
        # Per-report coalescing: one in-flight compaction per report.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, report_id: str) -> asyncio.Lock:
        lock = self._locks.get(report_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[report_id] = lock
            # Opportunistic pruning so the dict doesn't grow unbounded.
            if len(self._locks) > 512:
                for k in [k for k, v in self._locks.items() if not v.locked()][:256]:
                    self._locks.pop(k, None)
        return lock

    # ------------------------------------------------------------------
    # State helpers (also used by the message context builder + estimate)
    # ------------------------------------------------------------------
    @staticmethod
    async def get_state(db: AsyncSession, report_id: str) -> Optional[ReportContextState]:
        result = await db.execute(
            select(ReportContextState).where(
                ReportContextState.report_id == str(report_id),
                ReportContextState.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def is_report_busy(db: AsyncSession, report_id: str) -> bool:
        result = await db.execute(
            select(func.count(AgentExecution.id)).where(
                AgentExecution.report_id == str(report_id),
                AgentExecution.status == "in_progress",
            )
        )
        return bool(result.scalar() or 0)

    @classmethod
    async def get_ui_state(cls, db: AsyncSession, report_id: str) -> dict:
        """Cheap compaction status for UI payloads (estimate endpoint):
        cumulative totals plus whether a manual compact would find work."""
        state = await cls.get_state(db, report_id)
        count_query = (
            select(func.count(Completion.id))
            .filter(Completion.report_id == str(report_id))
            .filter(Completion.deleted_at.is_(None))
            .filter(Completion.message_type != COMPACTION_MESSAGE_TYPE)
        )
        if state and state.covers_until_completion_id:
            watermark = await db.get(Completion, state.covers_until_completion_id)
            if watermark is not None:
                count_query = count_query.filter(Completion.created_at > watermark.created_at)
        post_watermark = int((await db.execute(count_query)).scalar() or 0)
        busy = await cls.is_report_busy(db, report_id)
        return {
            **cls._state_payload(state),
            "can_compact": (post_watermark > KEEP_RECENT_TURNS) and not busy,
        }

    async def _load_scope(self, db: AsyncSession, report_id: str, state: Optional[ReportContextState]):
        """Completions past the watermark, ascending. Returns (scope, kept)
        where scope = turns to fold into the summary and kept = the recent
        tail that stays in full detail."""
        query = (
            select(Completion)
            .filter(Completion.report_id == str(report_id))
            .filter(Completion.deleted_at.is_(None))
            .filter(Completion.message_type != COMPACTION_MESSAGE_TYPE)
            .order_by(Completion.created_at.asc())
        )
        if state and state.covers_until_completion_id:
            watermark = await db.get(Completion, state.covers_until_completion_id)
            if watermark is not None:
                query = query.filter(Completion.created_at > watermark.created_at)
        rows = list((await db.execute(query)).scalars().all())
        if len(rows) <= KEEP_RECENT_TURNS:
            return [], rows
        return rows[:-KEEP_RECENT_TURNS], rows[-KEEP_RECENT_TURNS:]

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    async def compact(
        self,
        db: AsyncSession,
        report: Report,
        organization,
        llm_model,
        *,
        force: bool = False,
    ) -> dict:
        """Fold turns older than the recent window into the rolling summary.

        Fail-open by contract: raises nothing in auto mode — callers get a
        status dict. `force=True` (user-initiated) skips the token threshold
        but not the scope rule (the recent tail is never summarized).
        """
        report_id = str(report.id)
        lock = self._lock_for(report_id)
        if lock.locked():
            # Coalesce: a compaction is already in flight for this report.
            return {"status": "already_running", **self._state_payload(await self.get_state(db, report_id))}
        async with lock:
            # The locked() fast-path above is advisory (a second caller can
            # slip past it before the first actually acquires). That is safe:
            # callers serialize here, and _compact_inner re-reads state under
            # the lock — a queued duplicate sees the advanced watermark, finds
            # nothing past the protected tail, and returns nothing_to_compact
            # without a summarizer call.
            try:
                return await self._compact_inner(db, report, organization, llm_model, force=force)
            except Exception as e:
                logger.error(f"Context compaction failed for report {report_id}: {e}", exc_info=True)
                try:
                    await db.rollback()
                except Exception:
                    pass
                return {"status": "error", "message": str(e)}

    async def _compact_inner(self, db, report, organization, llm_model, *, force: bool) -> dict:
        report_id = str(report.id)
        state = await self.get_state(db, report_id)
        scope, kept = await self._load_scope(db, report_id, state)

        if not scope:
            return {"status": "nothing_to_compact", **self._state_payload(state)}

        # Render digests of the turns to fold — same digest path the planner
        # sees (honors allow_llm_see_data, tool digests, mentions).
        from app.ai.context.builders.message_context_builder import MessageContextBuilder
        builder = MessageContextBuilder(db, organization, report)
        scope_section = await builder.build(completion_ids=[str(c.id) for c in scope])
        digests_text = scope_section.render()
        if not digests_text.strip():
            return {"status": "nothing_to_compact", **self._state_payload(state)}

        digest_tokens = count_tokens(digests_text)

        if not force:
            post_watermark_count = len(scope) + len(kept)
            if digest_tokens < TRIGGER_TOKENS and post_watermark_count <= MESSAGES_WINDOW:
                return {"status": "below_threshold", **self._state_payload(state)}

        previous_summary = dict(state.summary_json or {}) if state else {}

        # LLM.inference is sync (pre-call quota check collides with a running
        # loop) — offload to a thread, same as Reporter.
        from app.ai.llm import LLM
        llm = LLM(llm_model)
        raw = await asyncio.to_thread(
            llm.inference,
            _summarizer_prompt(previous_summary, digests_text),
            usage_scope="report.context_compaction",
        )
        summary = _parse_summary_json(raw)
        if not summary:
            logger.warning(f"Compaction summarizer returned unparseable output for report {report_id}")
            return {"status": "error", "message": "summarizer_output_unparseable"}
        summary = _validate_entities(
            summary, digests_text + json.dumps(previous_summary, ensure_ascii=False)
        )

        # Persist state + marker completion atomically.
        now = datetime.utcnow()
        if state is None:
            state = ReportContextState(report_id=report_id, summary_json={})
            db.add(state)
        state.summary_json = summary
        state.covers_until_completion_id = str(scope[-1].id)
        state.covered_turns = (state.covered_turns or 0) + len(scope)
        state.tokens_compacted_total = (state.tokens_compacted_total or 0) + digest_tokens
        state.last_compaction_at = now

        marker_text = f"Compacted {len(scope)} turns (~{self._fmt_tokens(digest_tokens)} tokens)"
        marker = Completion(
            prompt={"content": ""},
            completion={"content": marker_text},
            status="success",
            model="system",
            role="system",
            message_type=COMPACTION_MESSAGE_TYPE,
            report_id=report_id,
        )
        db.add(marker)
        await db.commit()

        # The estimate cache may hold a pre-compaction context figure; drop it
        # so the next popover refresh reflects the compacted window (both the
        # auto and on-demand paths compact through here).
        try:
            from app.services.completion_service import CompletionService
            CompletionService._estimate_cache.clear()
        except Exception:
            pass

        logger.info(f"Compacted report {report_id}: {len(scope)} turns, ~{digest_tokens} tokens")
        return {
            "status": "compacted",
            "compacted_turns": len(scope),
            "tokens_compacted": digest_tokens,
            "marker_id": str(marker.id),
            "marker_text": marker_text,
            "marker_created_at": marker.created_at.isoformat() if marker.created_at else None,
            **self._state_payload(state),
        }

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        if n >= 1000:
            return f"{round(n / 1000)}k"
        return str(n)

    @staticmethod
    def _state_payload(state: Optional[ReportContextState]) -> dict:
        if state is None:
            return {"tokens_compacted_total": 0, "covered_turns": 0, "last_compaction_at": None}
        return {
            "tokens_compacted_total": int(state.tokens_compacted_total or 0),
            "covered_turns": int(state.covered_turns or 0),
            "last_compaction_at": state.last_compaction_at.isoformat() if state.last_compaction_at else None,
        }


context_compaction_service = ContextCompactionService()
