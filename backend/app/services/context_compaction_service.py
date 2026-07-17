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

# Legacy marker completions (early builds inserted one per compaction). No
# longer created — the UI divider is derived from covers_until_completion_id —
# but existing rows must stay excluded from LLM context and compaction scope.
COMPACTION_MESSAGE_TYPE = "context_compaction"

# ---------------------------------------------------------------------------
# Hermes-style geometry: every budget derives from the model's context window
# (window → conversation budget → trigger → protected tail), with count floors
# so tiny-token conversations still keep a substantial recent tail.
# ---------------------------------------------------------------------------
DEFAULT_MODEL_WINDOW = 200_000
# Share of the model window granted to detailed conversation history.
CONVERSATION_BUDGET_RATIO = 0.125
# Compact when the post-watermark window crosses this share of the
# conversation budget (Hermes fires at 50% of its context).
TRIGGER_RATIO = 0.5
# Protected tail: this share of the trigger budget in tokens…
TAIL_RATIO = 0.2
# …or this many completions, whichever protects MORE (Hermes protect_last_n).
PROTECT_LAST_MIN = 12
# The report's opening exchange is never folded into the summary
# (Hermes protect_first_n): "what was my first ask" must stay answerable.
PROTECT_FIRST_N = 2
# Count-based secondary trigger: compact when more completions than the
# planner's message window exist beyond the watermark.
MESSAGES_WINDOW = 40
# Summary size cap: min floor, share-of-window, hard ceiling (Hermes formula).
SUMMARY_MIN_TOKENS = 2_000
SUMMARY_RATIO_OF_WINDOW = 0.05
SUMMARY_MAX_TOKENS_CAP = 12_000


def _model_window(llm_model) -> int:
    try:
        w = int(getattr(llm_model, "context_window_tokens", None) or 0)
        return w if w > 0 else DEFAULT_MODEL_WINDOW
    except Exception:
        return DEFAULT_MODEL_WINDOW


def compaction_budgets(llm_model) -> dict:
    """Token budgets for one model, all derived from its context window.

    200k window → conversation 25k, trigger 12.5k, tail 2.5k, summary ≤ 10k.
    """
    window = _model_window(llm_model)
    conversation = int(window * CONVERSATION_BUDGET_RATIO)
    trigger = int(conversation * TRIGGER_RATIO)
    tail = int(trigger * TAIL_RATIO)
    summary_max = max(
        SUMMARY_MIN_TOKENS,
        min(int(window * SUMMARY_RATIO_OF_WINDOW), SUMMARY_MAX_TOKENS_CAP),
    )
    return {
        "window": window,
        "conversation_tokens": conversation,
        "trigger_tokens": trigger,
        "tail_tokens": tail,
        "summary_max_tokens": summary_max,
    }


def _estimate_completion_tokens(c) -> int:
    """Fast per-completion token estimate (~4 chars/token over the stored
    JSON). Digests add tool detail on top, so this under-counts — acceptable
    for trigger/tail decisions, same tradeoff as Hermes' gateway estimates."""
    try:
        text = json.dumps(c.prompt or {}, ensure_ascii=False) + json.dumps(c.completion or {}, ensure_ascii=False)
        return max(len(text) // 4, 1)
    except Exception:
        return 50

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
    opening = summary_json.get("opening_request")
    if opening:
        lines.append(f'Opening request (the first ask in this report): "{opening}"')
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


def _enforce_summary_budget(summary: dict, max_tokens: int) -> dict:
    """Trim list fields until the rendered summary fits the budget.

    Entities are trimmed last — they carry the ids the agent needs to avoid
    recreating assets. opening_request/goal are never trimmed."""
    def _rendered_tokens() -> int:
        try:
            return count_tokens(render_summary_for_prompt(summary))
        except Exception:
            return len(render_summary_for_prompt(summary)) // 4

    if _rendered_tokens() <= max_tokens:
        return summary
    trim_order = ("critical_context", "next_steps", "constraints_preferences", "key_decisions")
    for field in trim_order:
        vals = summary.get(field)
        while isinstance(vals, list) and len(vals) > 1 and _rendered_tokens() > max_tokens:
            vals.pop()
    progress = summary.get("progress")
    if isinstance(progress, dict):
        for key in ("done", "in_progress", "blocked"):
            vals = progress.get(key)
            while isinstance(vals, list) and len(vals) > 1 and _rendered_tokens() > max_tokens:
                vals.pop()
    entities = summary.get("entities")
    while isinstance(entities, list) and len(entities) > 10 and _rendered_tokens() > max_tokens:
        entities.pop()
    return summary


def _summarizer_prompt(previous_summary: dict, digests_text: str, max_tokens: int = SUMMARY_MIN_TOKENS) -> str:
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
- Keep the whole summary under ~{max_tokens} tokens.
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
        # Before the first compaction the protected opening exchange also sits
        # inside the post-watermark window; afterwards it lives behind the
        # watermark (rendered separately by the builder).
        protected = PROTECT_LAST_MIN + (0 if state else PROTECT_FIRST_N)
        return {
            **cls._state_payload(state),
            "can_compact": (post_watermark > protected) and not busy,
        }

    @staticmethod
    async def _opening_request(db: AsyncSession, report_id: str) -> Optional[str]:
        """The report's first user ask, verbatim (truncated) — stored in the
        summary programmatically so recall never depends on the summarizer."""
        row = (await db.execute(
            select(Completion)
            .filter(Completion.report_id == str(report_id))
            .filter(Completion.deleted_at.is_(None))
            .filter(Completion.role == 'user')
            .order_by(Completion.created_at.asc())
            .limit(1)
        )).scalars().first()
        if row is None:
            return None
        try:
            content = (row.prompt or {}).get("content", "") if isinstance(row.prompt, dict) else str(row.prompt or "")
        except Exception:
            content = ""
        content = (content or "").strip()
        if not content:
            return None
        return content[:300] + ("…" if len(content) > 300 else "")

    @staticmethod
    async def _head_completion_ids(db: AsyncSession, report_id: str) -> list:
        """Ids of the report's protected opening exchange (never folded)."""
        rows = (await db.execute(
            select(Completion.id)
            .filter(Completion.report_id == str(report_id))
            .filter(Completion.deleted_at.is_(None))
            .filter(Completion.message_type != COMPACTION_MESSAGE_TYPE)
            .order_by(Completion.created_at.asc())
            .limit(PROTECT_FIRST_N)
        )).scalars().all()
        return [str(r) for r in rows]

    async def _load_scope(self, db: AsyncSession, report_id: str, state: Optional[ReportContextState], budgets: dict):
        """Completions past the watermark, ascending. Returns (scope, kept, rows)
        where scope = turns to fold into the summary, kept = the protected
        recent tail, rows = the full post-watermark window (threshold input).

        The tail is token-measured (budgets["tail_tokens"]) with a
        PROTECT_LAST_MIN completion floor — whichever protects more. The
        report's opening exchange (PROTECT_FIRST_N) is never foldable."""
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

        head_ids = set(await self._head_completion_ids(db, report_id))
        foldable = [r for r in rows if str(r.id) not in head_ids]

        # Walk newest→oldest keeping the tail until BOTH floors are satisfied.
        kept_rev, tail_tokens = [], 0
        for r in reversed(foldable):
            if len(kept_rev) < PROTECT_LAST_MIN or tail_tokens < budgets["tail_tokens"]:
                kept_rev.append(r)
                tail_tokens += _estimate_completion_tokens(r)
            else:
                break
        kept = list(reversed(kept_rev))
        scope = foldable[: len(foldable) - len(kept)]
        return scope, kept, rows

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
        budgets = compaction_budgets(llm_model)
        state = await self.get_state(db, report_id)
        scope, kept, rows = await self._load_scope(db, report_id, state, budgets)

        if not scope:
            return {"status": "nothing_to_compact", **self._state_payload(state)}

        if not force:
            window_estimate = sum(_estimate_completion_tokens(r) for r in rows)
            if window_estimate < budgets["trigger_tokens"] and len(rows) <= MESSAGES_WINDOW:
                return {"status": "below_threshold", **self._state_payload(state)}

        # Render digests of the turns to fold — same digest path the planner
        # sees (honors allow_llm_see_data, tool digests, mentions).
        from app.ai.context.builders.message_context_builder import MessageContextBuilder
        builder = MessageContextBuilder(db, organization, report)
        scope_section = await builder.build(completion_ids=[str(c.id) for c in scope])
        digests_text = scope_section.render()
        if not digests_text.strip():
            return {"status": "nothing_to_compact", **self._state_payload(state)}

        digest_tokens = count_tokens(digests_text)

        previous_summary = dict(state.summary_json or {}) if state else {}

        # LLM.inference is sync (pre-call quota check collides with a running
        # loop) — offload to a thread, same as Reporter.
        from app.ai.llm import LLM
        llm = LLM(llm_model)
        raw = await asyncio.to_thread(
            llm.inference,
            _summarizer_prompt(previous_summary, digests_text, max_tokens=budgets["summary_max_tokens"]),
            usage_scope="report.context_compaction",
        )
        summary = _parse_summary_json(raw)
        if not summary:
            logger.warning(f"Compaction summarizer returned unparseable output for report {report_id}")
            return {"status": "error", "message": "summarizer_output_unparseable"}
        summary = _validate_entities(
            summary, digests_text + json.dumps(previous_summary, ensure_ascii=False)
        )

        # Programmatic recall guarantees: the opening request is set from the
        # DB (never trusted to the summarizer), and the summary is trimmed to
        # its token budget with entities sacrificed last.
        opening = await self._opening_request(db, report_id)
        if opening:
            summary["opening_request"] = opening
        summary = _enforce_summary_budget(summary, budgets["summary_max_tokens"])

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
            **self._state_payload(state),
        }

    @staticmethod
    def _state_payload(state: Optional[ReportContextState]) -> dict:
        if state is None:
            return {
                "tokens_compacted_total": 0,
                "covered_turns": 0,
                "last_compaction_at": None,
                "covers_until_completion_id": None,
            }
        return {
            "tokens_compacted_total": int(state.tokens_compacted_total or 0),
            "covered_turns": int(state.covered_turns or 0),
            "last_compaction_at": state.last_compaction_at.isoformat() if state.last_compaction_at else None,
            # The fold boundary: the UI renders the compaction divider right
            # after this completion (state-derived, never an event row).
            "covers_until_completion_id": str(state.covers_until_completion_id) if state.covers_until_completion_id else None,
        }


context_compaction_service = ContextCompactionService()
