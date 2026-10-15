"""Sub-agents — delegated agent runs with wave fan-in.

Lifecycle (docs/design/sub-agents.md, A8):

    parent turn: delegate × N (one planner response)
      └─ record_delegation()   → sub_agents rows (status=pending) in ONE wave
         (status=collecting), keyed by the parent's plan decision
    after the batch: launch_pending()  → wave running, total/remaining set,
         deadline date job armed, a launcher task starts each child
         (CompletionService.create_completion, mode=subagent, background,
         trigger_source=sub_agent) bounded by sub_agent_concurrency
    child: finish_subtask → record_finish();  AgentV2 finally → finalize_child()
         marks the row terminal exactly once (conditional UPDATE), writes the
         record back into the parent's `delegate` ToolExecution, decrements
         the wave; the child that reads zero flips the wave to `firing` and
         spawns the fan-in
    fan-in: run_fan_in() waits for the report to be idle, then
         run_machine_turn(trigger_source=sub_agent) with the wave's records
    stop: stop_wave() (no fan-in) / stop_sub_agent() (wave continues);
         deadline: run_wave_deadline() stops stragglers and fires the fan-in

Every cross-replica decision goes through a conditional UPDATE or the
scheduler claim table, never through process memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update

from app.models.sub_agent import (
    EXIT_DEADLINE, EXIT_ERROR, EXIT_FINISH_CALL, EXIT_MAX_STEPS, EXIT_MAX_TOKENS, EXIT_NO_FINISH_CALL, EXIT_STOPPED,
    RUN_FAILED, RUN_FINISHED, RUN_INTERRUPTED, RUN_PENDING, RUN_RUNNING, RUN_WAITING, RUN_TIMED_OUT,
    TERMINAL_RUN_STATUSES,
    WAVE_COLLECTING, WAVE_DONE, WAVE_FIRING, WAVE_RUNNING, WAVE_STOPPED, WAVE_TIMED_OUT,
    SubAgentRun, SubAgentWave,
)

from app.services.sub_agent_locks import lock_report, lock_wave

logger = logging.getLogger(__name__)

_UNREAD_FINISH_CAVEAT = "Parent messages arrived after this record was prepared and were not incorporated; inspect message delivery history."

_DEADLINE_JOB_PREFIX = "sub_agent_wave:"
# How long the fan-in waits for a user turn on the report to finish before
# giving up on serialisation and running anyway.
_FAN_IN_WAIT_SECONDS = 30 * 60
_SLOT_POLL_SECONDS = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(settings, key: str, default):
    try:
        cfg = settings.get_config(key) if settings else None
        val = getattr(cfg, "value", None) if cfg is not None else None
        return default if val is None else val
    except Exception:
        return default


def _clamp(v, lo, hi, default) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def sub_agents_enabled(settings) -> bool:
    return bool(_cfg(settings, "enable_sub_agents", True))


def sub_agent_posture(settings) -> str:
    v = str(_cfg(settings, "sub_agent_posture", "proactive") or "proactive").strip().lower()
    return "explicit" if v == "explicit" else "proactive"


def sub_agent_limits(settings) -> Dict[str, Any]:
    return {
        "max_steps": _clamp(_cfg(settings, "sub_agent_max_steps", 40), 5, 200, 40),
        "max_tokens": _clamp(_cfg(settings, "sub_agent_max_tokens", 400000), 20000, 5000000, 400000),
        "deadline_minutes": _clamp(_cfg(settings, "sub_agent_deadline_minutes", 15), 2, 60, 15),
        "concurrency": _clamp(_cfg(settings, "sub_agent_concurrency", 3), 1, 6, 3),
        "max_per_wave": _clamp(_cfg(settings, "sub_agent_max_per_wave", 5), 1, 10, 5),
        "model": str(_cfg(settings, "sub_agent_model", "auto") or "auto").strip(),
        "allow_model_override": bool(_cfg(settings, "sub_agent_allow_model_override", True)),
        "allow_create_data": bool(_cfg(settings, "sub_agent_allow_create_data", False)),
    }


def _dialect_name(db) -> str:
    try:
        return str(db.get_bind().dialect.name or "").lower()
    except Exception:
        return ""


def effective_concurrency(configured: int, dialect: str) -> int:
    """SQLite has one writer: several agent runs each holding a transaction
    across an LLM call starve each other into "database is locked" and the
    children crash out. A SQLite-backed deployment (dev, the sandbox stack)
    runs its children one at a time; Postgres honours the org setting."""
    if "sqlite" in (dialect or ""):
        if configured > 1:
            logger.info("sub-agents: SQLite backend — children run one at a time (configured concurrency %s)", configured)
        return 1
    return int(configured)


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────

class PendingSubAgentMessages(ValueError):
    """A child must plan with newly accepted directions before finishing."""


class FinishConflict(ValueError):
    """A terminal finish record already exists with different content."""


async def has_free_child_slot(db, report_id, *, settings=None, concurrency=None):
    configured = concurrency if concurrency is not None else sub_agent_limits(settings)["concurrency"]
    limit = effective_concurrency(configured, _dialect_name(db))
    running = (await db.execute(select(func.count(SubAgentRun.id)).where(
        SubAgentRun.report_id == report_id, SubAgentRun.status == RUN_RUNNING,
    ))).scalar_one()
    return running < limit


class SubAgentService:

    # ── parent side ─────────────────────────────────────────────────────────

    async def record_delegation(
        self,
        db,
        *,
        organization,
        user,
        report,
        parent_head_completion,
        parent_system_completion,
        parent_execution_id: Optional[str],
        tool_execution_id: Optional[str],
        plan_decision_id: Optional[str],
        data,
        settings,
        parent_mode: Optional[str],
        parent_model,
        small_model,
    ) -> Dict[str, Any]:
        """Record one `delegate` call as a pending sub-agent in the decision's wave.

        Returns the spawn receipt (the tool's output). Children are NOT
        started here — `launch_pending` runs once the whole batch is recorded,
        so a fast child can never fire the fan-in before its siblings exist.
        """
        limits = sub_agent_limits(settings)
        wave = await self._get_or_create_wave(
            db,
            organization_id=str(organization.id),
            report_id=str(report.id),
            user_id=str(user.id) if user else None,
            parent_completion_id=str(parent_system_completion.id) if parent_system_completion else None,
            parent_execution_id=parent_execution_id,
            plan_decision_id=plan_decision_id,
            parent_mode=parent_mode,
            parent_prompt=(parent_head_completion.prompt or {}).get("content", "") if parent_head_completion and isinstance(parent_head_completion.prompt, dict) else "",
        )
        count = (await db.execute(
            select(func.count(SubAgentRun.id)).where(SubAgentRun.wave_id == wave.id)
        )).scalar() or 0
        if count >= limits["max_per_wave"]:
            return {
                "sub_agent_id": None, "wave_id": str(wave.id), "status": "refused",
                "title": data.title, "profile": data.profile,
                "refused": (
                    f"This wave already holds {count} sub-agents (org cap {limits['max_per_wave']}). "
                    "Not started. Plan a second wave after this one reports back."
                ),
            }

        # Agents: default = the parent's attached agents; override narrows to
        # a subset of them. Anything outside is refused by name, never
        # silently dropped.
        attached = {str(ds.id): ds for ds in (getattr(report, "data_sources", None) or [])}
        refused_bits: List[str] = []
        if data.agent_ids:
            chosen = [a for a in data.agent_ids if str(a) in attached]
            unknown = [a for a in data.agent_ids if str(a) not in attached]
            if unknown:
                refused_bits.append(f"agent ids not attached to this conversation ignored: {', '.join(unknown)}")
            agent_ids = chosen or list(attached.keys())
        else:
            agent_ids = list(attached.keys())
        agents = [{"id": aid, "name": getattr(attached[aid], "name", "")} for aid in agent_ids if aid in attached]

        # Model: profile/org default, with a validated per-call override.
        model_row, model_note = await self._resolve_model(
            db, organization=organization, user=user, profile=data.profile, limits=limits,
            override=data.model, parent_model=parent_model, small_model=small_model,
        )
        if model_note:
            refused_bits.append(model_note)

        max_steps = limits["max_steps"]
        if data.max_steps:
            max_steps = max(3, min(max_steps, int(data.max_steps)))

        row = SubAgentRun(
            wave_id=str(wave.id),
            organization_id=str(organization.id),
            report_id=str(report.id),
            wave_index=int(count),
            parent_tool_execution_id=tool_execution_id,
            parent_completion_id=str(parent_system_completion.id) if parent_system_completion else None,
            parent_execution_id=parent_execution_id,
            title=data.title.strip()[:200],
            profile=data.profile,
            goal=data.goal.strip(),
            context=(data.context or "").strip() or None,
            agent_ids_json=agent_ids,
            model_id=str(model_row.id) if model_row is not None else None,
            model_name=(getattr(model_row, "name", None) or getattr(model_row, "model_id", None)) if model_row is not None else None,
            max_steps=max_steps,
            status=RUN_PENDING,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "sub_agent_id": str(row.id), "wave_id": str(wave.id), "status": "running",
            "title": row.title, "profile": row.profile, "agents": agents,
            "model": row.model_name, "max_steps": max_steps,
            "refused": "; ".join(refused_bits) or None,
        }

    async def _get_or_create_wave(self, db, *, organization_id, report_id, user_id, parent_completion_id,
                                  parent_execution_id, plan_decision_id, parent_mode, parent_prompt) -> SubAgentWave:
        # The wave of THIS decision: still collecting, same parent turn. The
        # delegate tool is dispatched serially within a batch, so no two calls
        # race here; a stray second collecting wave would only split a batch.
        q = select(SubAgentWave).where(
            SubAgentWave.parent_completion_id == parent_completion_id,
            SubAgentWave.status == WAVE_COLLECTING,
        ).order_by(SubAgentWave.created_at.desc()).limit(1)
        wave = (await db.execute(q)).scalars().first()
        if wave is not None:
            return wave
        wave = SubAgentWave(
            organization_id=organization_id, report_id=report_id, user_id=user_id,
            parent_completion_id=parent_completion_id, parent_execution_id=parent_execution_id,
            status=WAVE_COLLECTING, remaining=0, total=0,
            parent_mode=parent_mode, parent_prompt=(parent_prompt or "")[:4000],
        )
        db.add(wave)
        await db.commit()
        await db.refresh(wave)
        return wave

    async def _resolve_model(self, db, *, organization, user, profile, limits, override, parent_model, small_model):
        """(LLMModel|None, note). Precedence: validated override > org setting > profile pref."""
        from app.ai.sub_agent_profiles import get_profile
        from app.models.llm_model import LLMModel
        note = None
        if override:
            if not limits["allow_model_override"]:
                note = f"model override '{override}' ignored: the organization disabled per-call overrides"
            else:
                row = await self._lookup_model(db, organization, user, override)
                if row is not None:
                    return row, None
                note = f"model override '{override}' ignored: not a model this user may use"
        setting = (limits["model"] or "auto").lower()
        if setting not in ("auto", "small", "default"):
            row = await self._lookup_model(db, organization, user, limits["model"])
            if row is not None:
                return row, note
        pref = get_profile(profile).model_pref if setting == "auto" else setting
        if pref == "small" and small_model is not None:
            return small_model, note
        return parent_model, note

    async def _lookup_model(self, db, organization, user, spec: str):
        """Resolve '<provider>/<model_id>', a model_id, or a db id — only among
        enabled models the user may use (per-model RBAC)."""
        from app.models.llm_model import LLMModel
        from app.models.llm_provider import LLMProvider
        from app.core.permission_resolver import user_can_use_model
        spec = (spec or "").strip()
        if not spec:
            return None
        provider_part, _, model_part = spec.partition("/")
        if not model_part:
            provider_part, model_part = None, spec
        rows = (await db.execute(
            select(LLMModel).where(LLMModel.organization_id == organization.id, LLMModel.is_enabled == True)  # noqa: E712
        )).scalars().all()
        for m in rows:
            if str(m.id) == spec or (m.model_id == model_part and (provider_part is None or _provider_matches(m, provider_part))):
                try:
                    if user is not None and not await user_can_use_model(db, user.id, organization.id, m):
                        return None
                except Exception:
                    return None
                return m
        return None

    async def launch_pending(self, *, report_id: str, parent_completion_id: str, settings, sigkilled: bool = False) -> Optional[str]:
        """Close the collecting wave of this parent turn and start its children.

        Called by the agent loop after the batch that held the delegate calls.
        Returns the wave id, or None when there was nothing to launch.
        """
        from app.dependencies import async_session_maker
        from app.core.fire_and_forget import spawn
        limits = sub_agent_limits(settings)
        async with async_session_maker() as db:
            wave = (await db.execute(
                select(SubAgentWave).where(
                    SubAgentWave.parent_completion_id == parent_completion_id,
                    SubAgentWave.status == WAVE_COLLECTING,
                ).order_by(SubAgentWave.created_at.desc()).limit(1)
            )).scalars().first()
            if wave is None:
                return None
            children = (await db.execute(
                select(SubAgentRun).where(SubAgentRun.wave_id == wave.id).order_by(SubAgentRun.wave_index.asc())
            )).scalars().all()
            if sigkilled or not children:
                wave.status = WAVE_STOPPED
                for c in children:
                    c.status = RUN_INTERRUPTED
                    c.exit_reason = EXIT_STOPPED
                    c.finished_at = datetime.utcnow()
                await db.commit()
                return str(wave.id)
            wave.total = len(children)
            wave.remaining = len(children)
            wave.status = WAVE_RUNNING
            wave.deadline_at = datetime.utcnow() + timedelta(minutes=limits["deadline_minutes"])
            wave.deadline_job_id = f"{_DEADLINE_JOB_PREFIX}{wave.id}"
            await db.commit()
            wave_id = str(wave.id)
            deadline_at = wave.deadline_at
            job_id = wave.deadline_job_id
        self._arm_deadline(job_id, wave_id, deadline_at)
        spawn(self._launcher(wave_id, effective_concurrency(limits["concurrency"], _dialect_name(db))))
        return wave_id

    def _arm_deadline(self, job_id: str, wave_id: str, deadline_at: datetime) -> None:
        try:
            from app.core.scheduler import scheduler
            scheduler.add_job(
                func=run_wave_deadline, trigger="date",
                run_date=deadline_at.replace(tzinfo=timezone.utc),
                id=job_id, kwargs={"job_id": job_id, "wave_id": wave_id},
                replace_existing=True, misfire_grace_time=3600,
            )
        except Exception:
            logger.warning("sub-agent wave %s: could not arm deadline job", wave_id, exc_info=True)

    def _cancel_deadline(self, job_id: Optional[str]) -> None:
        if not job_id:
            return
        try:
            from app.core.scheduler import scheduler
            scheduler.remove_job(job_id=job_id)
        except Exception:
            pass

    async def _launcher(self, wave_id: str, concurrency: int) -> None:
        """Start each pending child when a slot is free. In-process pacing:
        the DB count of running children for the report is the slot source."""
        from app.dependencies import async_session_maker
        try:
            while True:
                async with async_session_maker() as db:
                    wave = await db.get(SubAgentWave, wave_id)
                    if wave is None or wave.status != WAVE_RUNNING:
                        return
                    nxt = (await db.execute(
                        select(SubAgentRun).where(SubAgentRun.wave_id == wave_id, SubAgentRun.status == RUN_PENDING)
                        .order_by(SubAgentRun.wave_index.asc()).limit(1)
                    )).scalars().first()
                    if nxt is None:
                        return
                    if not await has_free_child_slot(db, wave.report_id, concurrency=concurrency):
                        nxt = None
                if nxt is None:
                    await asyncio.sleep(_SLOT_POLL_SECONDS)
                    continue
                await self._start_child(str(nxt.id))
        except Exception:
            logger.exception("sub-agent launcher for wave %s crashed", wave_id)

    async def _start_child(self, sub_agent_id: str) -> None:
        """Create the child's hidden completions and start its run."""
        from app.dependencies import async_session_maker
        from app.models.organization import Organization
        from app.models.report import Report
        from app.models.user import User
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        from app.services.completion_service import CompletionService

        async with async_session_maker() as db:
            row = await db.get(SubAgentRun, sub_agent_id)
            if row is None or row.status != RUN_PENDING:
                return
            wave = await db.get(SubAgentWave, row.wave_id)
            report = await db.get(Report, row.report_id)
            organization = await db.get(Organization, row.organization_id)
            user = await db.get(User, wave.user_id) if wave and wave.user_id else None
            if not (report and organization and user):
                await self._mark_terminal(db, row, RUN_FAILED, EXIT_ERROR, error="report, organization or user gone before launch")
                return
            # Share the report slot lock with children resuming after questions.
            await lock_report(db, row.report_id)
            await lock_wave(db, row.wave_id)
            await db.refresh(wave)
            if wave.status != WAVE_RUNNING:
                await db.rollback()
                return
            org_settings = await organization.get_settings(db)
            if not await has_free_child_slot(db, row.report_id, settings=org_settings):
                await db.rollback()
                return
            # Claim the row before the run can finish (finalize_child is a
            # conditional transition from running). Conditional, so a stop that
            # landed after the read above is not overwritten back to running.
            claimed = await db.execute(
                update(SubAgentRun)
                .where(SubAgentRun.id == row.id, SubAgentRun.status == RUN_PENDING)
                .values(status=RUN_RUNNING, started_at=datetime.utcnow())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if (claimed.rowcount or 0) != 1:
                return
            await db.refresh(row)
            prompt = PromptSchema(
                content=_child_prompt(row),
                mode="subagent",
                model_id=str(row.model_id) if row.model_id else None,
                sub_agent_id=str(row.id),
            )
            try:
                res = await CompletionService().create_completion(
                    db=db, report_id=str(report.id),
                    completion_data=CompletionCreate(prompt=prompt),
                    current_user=user, organization=organization,
                    background=True, trigger_source="sub_agent",
                )
            except Exception as e:
                logger.exception("sub-agent %s: could not start child", sub_agent_id)
                row = await db.get(SubAgentRun, sub_agent_id)
                await self._mark_terminal(db, row, RUN_FAILED, EXIT_ERROR, error=f"could not start: {e}")
                await self._after_terminal(db, row)
                return
            head_id = sys_id = None
            for c in getattr(res, "completions", []) or []:
                if getattr(c, "role", None) == "user":
                    head_id = str(c.id)
                elif getattr(c, "role", None) == "system":
                    sys_id = str(c.id)
            row = await db.get(SubAgentRun, sub_agent_id)
            row.child_head_completion_id = head_id
            row.child_completion_id = sys_id
            await db.commit()
            # A stop (child, wave or deadline) that closed the row while the
            # child was being created found no completion to signal. Read the
            # status after publishing the id: either the stop sees the id, or
            # this sees the stop.
            await db.refresh(row)
            if sys_id and row.status in TERMINAL_RUN_STATUSES:
                await _sigkill_completion(db, sys_id)

    # ── child side ──────────────────────────────────────────────────────────

    async def bind_child_execution(self, db, *, sub_agent_id: str, execution_id: str, parent_links: bool = True) -> None:
        """Called by AgentV2 (mode=subagent) once its AgentExecution exists."""
        from app.models.agent_execution import AgentExecution
        row = await db.get(SubAgentRun, sub_agent_id)
        if row is None:
            return
        row.child_execution_id = str(execution_id)
        if parent_links:
            ae = await db.get(AgentExecution, str(execution_id))
            if ae is not None:
                ae.parent_agent_execution_id = row.parent_execution_id
                ae.parent_tool_execution_id = row.parent_tool_execution_id
        await db.commit()

    async def fail_waiting_children_for_parent(self, db, *, report_id, user_id, reason):
        from app.services.sub_agent_message_service import sub_agent_message_service
        children = (await db.execute(select(SubAgentRun).join(
            SubAgentWave, SubAgentWave.id == SubAgentRun.wave_id,
        ).where(SubAgentRun.report_id == report_id,
                SubAgentWave.user_id == user_id,
                SubAgentRun.status.in_((RUN_PENDING, RUN_RUNNING, RUN_WAITING))))).scalars().all()
        for child in children:
            await lock_report(db, report_id)
            await lock_wave(db, child.wave_id)
            if not await sub_agent_message_service.waiting_question(db, str(child.id)):
                # Release the no-op locks without expiring other loaded children.
                await db.commit()
                continue
            if await self._mark_terminal(db, child, RUN_FAILED, EXIT_ERROR, error=reason):
                if child.child_completion_id:
                    await _sigkill_completion(db, child.child_completion_id)
                child.envelope_json = _envelope(child)
                await db.commit()
                await self._write_back(db, child)
                await self._after_terminal(db, child)

    async def record_finish(self, db, *, sub_agent_id: str, result: Dict[str, Any], can_review_messages: bool = True) -> Optional[SubAgentRun]:
        """`finish_subtask`: store the validated body; the envelope lands at finalize."""
        from app.models.note import Note
        row = await db.get(SubAgentRun, sub_agent_id)
        if row is None:
            return None
        # Serialize completion with sends. A message accepted while the final
        # model response was in flight must reach another planning step, not
        # silently become undelivered on a successful finish.
        from app.models.sub_agent_message import SubAgentMessage
        await lock_report(db, row.report_id)
        await lock_wave(db, row.wave_id)
        await db.execute(update(SubAgentRun).where(SubAgentRun.id == sub_agent_id).values(status=SubAgentRun.status, updated_at=SubAgentRun.updated_at))
        await db.refresh(row)
        note_id = result.get("report_note_id")
        if note_id:
            note = await db.get(Note, str(note_id))
            if note is None or str(note.report_id) != str(row.report_id):
                result = {**result, "report_note_id": None}
                note_id = None
        if row.status == RUN_FINISHED and row.exit_reason == EXIT_FINISH_CALL:
            stored = row.result_json or {}
            original = {**stored, "caveats": [
                cleaned for caveat in stored.get("caveats", [])
                if (cleaned := caveat.removesuffix(_UNREAD_FINISH_CAVEAT).rstrip())
            ]}
            if stored != result and original != result:
                await db.rollback()
                raise FinishConflict("This sub-agent already stored a different finish record.")
            await db.commit()
            return row
        if row.status not in (RUN_PENDING, RUN_RUNNING, RUN_WAITING):
            await db.rollback()
            raise ValueError("This sub-agent has already ended; its result cannot be replaced.")
        unread = (await db.execute(select(SubAgentMessage.id).where(
            SubAgentMessage.report_id == row.report_id,
            SubAgentMessage.recipient == sub_agent_id, SubAgentMessage.status == "sent",
        ).limit(1))).scalar_one_or_none()
        if unread:
            caveats = list(result.get("caveats", []))
            if len(caveats) >= 8:
                caveats[-1] += " " + _UNREAD_FINISH_CAVEAT
            else:
                caveats.append(_UNREAD_FINISH_CAVEAT)
            result = {**result, "caveats": caveats}
        if unread and can_review_messages:
            # Preserve a candidate before asking the model to revise. A deadline,
            # stop or failed next call must not replace complete evidence with a
            # synthesized empty result. A later accepted finish replaces it.
            row.result_json = result
            row.report_note_id = str(note_id) if note_id else row.report_note_id
            await db.commit()
            raise PendingSubAgentMessages(
                "New parent messages arrived while you were working. Review agent_messages in your next "
                "planning step, apply the updated direction to your work and notes, then finish."
            )
        row.status = RUN_FINISHED
        row.exit_reason = EXIT_FINISH_CALL
        row.finished_at = datetime.utcnow()
        from app.services.sub_agent_message_service import sub_agent_message_service
        await sub_agent_message_service.close_child(db, str(row.id), report_id=str(row.report_id))
        row.result_json = result
        row.report_note_id = str(note_id) if note_id else row.report_note_id
        await db.commit()
        return row

    async def finalize_child(
        self,
        *,
        sub_agent_id: str,
        agent_status: str,
        last_text: str,
        steps_used: int,
        tokens: Optional[Dict[str, Any]],
        execution_id: Optional[str],
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        model_name: Optional[str] = None,
        steps_exhausted: bool = False,
        tokens_exhausted: bool = False,
    ) -> None:
        """AgentV2 (mode=subagent) `finally`: close the row exactly once."""
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            row = await db.get(SubAgentRun, sub_agent_id)
            if row is None:
                return
            transitioned = False
            if row.status == RUN_FINISHED and row.exit_reason == EXIT_FINISH_CALL:
                # finish_subtask already closed it; `remaining` has not been
                # touched yet (record_finish never decrements), so do it here.
                transitioned = True
            elif row.status in (RUN_PENDING, RUN_RUNNING, RUN_WAITING):
                if agent_status == "sigkill":
                    status, exit_reason = RUN_INTERRUPTED, EXIT_STOPPED
                elif agent_status == "error" and not steps_exhausted:
                    status, exit_reason = RUN_FAILED, EXIT_ERROR
                elif tokens_exhausted:
                    status, exit_reason = RUN_FINISHED, EXIT_MAX_TOKENS
                else:
                    status, exit_reason = RUN_FINISHED, (EXIT_MAX_STEPS if steps_exhausted else EXIT_NO_FINISH_CALL)
                transitioned = await self._mark_terminal(db, row, status, exit_reason, error=error, last_text=last_text)
            # Envelope always (even when stop/deadline closed the row first).
            row.steps_used = int(steps_used or 0)
            row.tokens_json = tokens or row.tokens_json
            row.envelope_json = _envelope(row, duration_ms=duration_ms, model_name=model_name)
            if execution_id and not row.child_execution_id:
                row.child_execution_id = str(execution_id)
            await db.commit()
            await self._write_back(db, row)
            if transitioned:
                await self._after_terminal(db, row)

    async def _mark_terminal(self, db, row: SubAgentRun, status: str, exit_reason: str, *, error: Optional[str] = None,
                             last_text: str = "") -> bool:
        """Conditional transition pending/running → terminal. True if THIS call won."""
        res = await db.execute(
            update(SubAgentRun)
            .where(SubAgentRun.id == row.id, SubAgentRun.status.in_((RUN_PENDING, RUN_RUNNING, RUN_WAITING)))
            .values(status=status, exit_reason=exit_reason, finished_at=datetime.utcnow(),
                    error_message=(error or None))
            .execution_options(synchronize_session=False)
        )
        from app.services.sub_agent_message_service import sub_agent_message_service
        await sub_agent_message_service.close_child(db, str(row.id), report_id=str(row.report_id))
        await db.commit()
        won = (res.rowcount or 0) == 1
        if won:
            await db.refresh(row)
            if not row.result_json:
                row.result_json = _synthesized_result(row, status, exit_reason, last_text, error)
                await db.commit()
        return won

    async def _after_terminal(self, db, row: SubAgentRun) -> None:
        """Decrement the wave; the caller that reads zero fires the fan-in."""
        from app.core.fire_and_forget import spawn
        await db.execute(
            update(SubAgentWave)
            .where(SubAgentWave.id == row.wave_id, SubAgentWave.status == WAVE_RUNNING, SubAgentWave.remaining > 0)
            .values(remaining=SubAgentWave.remaining - 1)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        wave = await db.get(SubAgentWave, row.wave_id)
        if wave is None:
            return
        await db.refresh(wave)
        if wave.status == WAVE_RUNNING and int(wave.remaining or 0) <= 0:
            res = await db.execute(
                update(SubAgentWave)
                .where(SubAgentWave.id == wave.id, SubAgentWave.status == WAVE_RUNNING)
                .values(status=WAVE_FIRING, fired_at=datetime.utcnow())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if (res.rowcount or 0) == 1:
                self._cancel_deadline(wave.deadline_job_id)
                spawn(run_fan_in(str(wave.id)))

    async def _write_back(self, db, row: SubAgentRun) -> None:
        """Copy the record into the parent's `delegate` ToolExecution.result_json
        so cards, digests and the summary read it like any other tool result."""
        if not row.parent_tool_execution_id:
            return
        try:
            from app.models.tool_execution import ToolExecution
            te = await db.get(ToolExecution, row.parent_tool_execution_id)
            if te is None:
                return
            current = dict(te.result_json or {})
            current.update({
                "status": row.status, "exit_reason": row.exit_reason,
                "record": row.result_json, "envelope": row.envelope_json,
                "report_note_id": row.report_note_id, "steps_used": row.steps_used,
                "child_completion_id": row.child_completion_id, "child_execution_id": row.child_execution_id,
            })
            te.result_json = current
            outcome = (row.result_json or {}).get("outcome")
            if outcome:
                te.result_summary = f"{row.title}: {outcome}"
            await db.commit()
        except Exception:
            logger.warning("sub-agent %s: write-back failed", row.id, exc_info=True)

    # ── control ─────────────────────────────────────────────────────────────

    async def stop_sub_agent(self, db, sub_agent_id: str, *, reason: str = "") -> Optional[SubAgentRun]:
        row = await db.get(SubAgentRun, sub_agent_id)
        if row is None:
            return None
        if row.status in TERMINAL_RUN_STATUSES:
            return row
        won = await self._mark_terminal(db, row, RUN_INTERRUPTED, EXIT_STOPPED, error=(reason or None))
        if row.child_completion_id:
            await _sigkill_completion(db, row.child_completion_id)
        row.envelope_json = _envelope(row)
        await db.commit()
        await self._write_back(db, row)
        if won:
            await self._after_terminal(db, row)
        return row

    async def stop_wave(self, db, wave_id: str, *, reason: str = "") -> Optional[SubAgentWave]:
        wave = await db.get(SubAgentWave, wave_id)
        if wave is None:
            return None
        res = await db.execute(
            update(SubAgentWave)
            .where(SubAgentWave.id == wave.id, SubAgentWave.status.in_((WAVE_COLLECTING, WAVE_RUNNING)))
            .values(status=WAVE_STOPPED)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if (res.rowcount or 0) != 1:
            await db.refresh(wave)
            return wave
        self._cancel_deadline(wave.deadline_job_id)
        children = (await db.execute(select(SubAgentRun).where(SubAgentRun.wave_id == wave.id))).scalars().all()
        for c in children:
            if c.status in TERMINAL_RUN_STATUSES:
                continue
            await self._mark_terminal(db, c, RUN_INTERRUPTED, EXIT_STOPPED, error=(reason or "wave stopped"))
            if c.child_completion_id:
                await _sigkill_completion(db, c.child_completion_id)
            c.envelope_json = _envelope(c)
            await db.commit()
            await self._write_back(db, c)
        await db.refresh(wave)
        return wave

    # ── reads ───────────────────────────────────────────────────────────────

    async def snapshot(self, db, sub_agent_id: str, *, include_report: bool = False, report_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        from app.models.note import Note
        from app.models.plan_decision import PlanDecision
        from app.models.tool_execution import ToolExecution
        row = await db.get(SubAgentRun, sub_agent_id)
        if row is None or (report_id and str(row.report_id) != str(report_id)):
            return None
        out: Dict[str, Any] = {
            "sub_agent_id": str(row.id), "wave_id": str(row.wave_id), "title": row.title, "profile": row.profile,
            "goal": row.goal, "status": row.status, "exit_reason": row.exit_reason,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "steps_used": row.steps_used, "max_steps": row.max_steps, "model": row.model_name,
            "agent_ids": row.agent_ids_json or [],
            "report_note_id": row.report_note_id,
            "child_completion_id": row.child_completion_id, "child_execution_id": row.child_execution_id,
        }
        from app.services.sub_agent_message_service import sub_agent_message_service
        out["messages"] = await sub_agent_message_service.list_for_child(
            db, organization_id=str(row.organization_id), report_id=str(row.report_id), sub_agent_id=str(row.id),
        )
        calls: List[Dict[str, Any]] = []
        steps = row.steps_used or 0
        if row.child_execution_id:
            tes = (await db.execute(
                select(ToolExecution).where(ToolExecution.agent_execution_id == row.child_execution_id)
                .order_by(ToolExecution.created_at.asc())
            )).scalars().all()
            for te in tes:
                args = te.arguments_json if isinstance(te.arguments_json, dict) else {}
                calls.append({
                    "tool": te.tool_name,
                    "title": (args.get("title") or te.tool_name) if isinstance(args, dict) else te.tool_name,
                    "status": te.status, "duration_ms": te.duration_ms,
                    "summary": (te.result_summary or "")[:200],
                    "created_step_id": te.created_step_id,
                })
            if not steps:
                steps = (await db.execute(
                    select(func.count(PlanDecision.id)).where(PlanDecision.agent_execution_id == row.child_execution_id)
                )).scalar() or 0
        out["steps_used"] = steps
        out["calls"] = calls
        out["latest_calls"] = calls[-5:]
        out["evidence_so_far"] = [c["created_step_id"] for c in calls if c.get("created_step_id")]
        if row.status in TERMINAL_RUN_STATUSES:
            out["record"] = row.result_json
            out["envelope"] = row.envelope_json
        if row.started_at and row.status == RUN_RUNNING:
            out["elapsed_s"] = int((datetime.utcnow() - row.started_at).total_seconds())
        note = None
        if row.report_note_id:
            note = await db.get(Note, row.report_note_id)
        elif row.child_execution_id:
            note = (await db.execute(
                select(Note).where(Note.agent_execution_id == row.child_execution_id, Note.deleted_at.is_(None))
                .order_by(Note.created_at.desc()).limit(1)
            )).scalars().first()
        if note is not None:
            out["note"] = {"id": str(note.id), "title": note.title,
                           "content": note.content if include_report else (note.content or "")[:300]}
        return out

    async def list_for_report(self, db, report_id: str, *, limit_waves: int = 10) -> Dict[str, Any]:
        waves = (await db.execute(
            select(SubAgentWave).where(SubAgentWave.report_id == str(report_id))
            .order_by(SubAgentWave.created_at.desc()).limit(limit_waves)
        )).scalars().all()
        if not waves:
            return {"waves": [], "background_tasks": []}
        wave_ids = [str(w.id) for w in waves]
        runs = (await db.execute(
            select(SubAgentRun).where(SubAgentRun.wave_id.in_(wave_ids)).order_by(SubAgentRun.wave_index.asc())
        )).scalars().all()
        by_wave: Dict[str, List[SubAgentRun]] = {}
        for r in runs:
            by_wave.setdefault(str(r.wave_id), []).append(r)
        out_waves = []
        tasks = []
        for w in waves:
            items = [_run_brief(r) for r in by_wave.get(str(w.id), [])]
            out_waves.append({
                "id": str(w.id), "status": w.status, "total": w.total, "remaining": w.remaining,
                "parent_completion_id": w.parent_completion_id, "fan_in_completion_id": w.fan_in_completion_id,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "deadline_at": w.deadline_at.isoformat() if w.deadline_at else None,
                "sub_agents": items,
            })
            if w.status in (WAVE_COLLECTING, WAVE_RUNNING, WAVE_FIRING):
                for it in items:
                    tasks.append({**it, "kind": "sub_agent", "wave_id": str(w.id)})
        out_waves.reverse()
        return {"waves": out_waves, "background_tasks": tasks}

    async def context_block(self, db, report_id: str) -> str:
        """`<sub_agents>` for the parent: recent waves, one line per child."""
        data = await self.list_for_report(db, report_id, limit_waves=3)
        if not data["waves"]:
            return ""
        lines = ["<sub_agents>"]
        for w in data["waves"]:
            lines.append(f'  <wave id="{w["id"]}" status="{w["status"]}" total="{w["total"]}">')
            for r in w["sub_agents"]:
                bits = [f'id="{r["sub_agent_id"]}"', f'title="{_attr(r["title"])}"', f'status="{r["status"]}"']
                if r.get("outcome"):
                    bits.append(f'outcome="{r["outcome"]}"')
                if r.get("confidence") is not None:
                    bits.append(f'confidence="{r["confidence"]}"')
                if r.get("steps_used") is not None:
                    bits.append(f'steps="{r["steps_used"]}"')
                summary = (r.get("summary") or "")[:200]
                lines.append(f'    <sub_agent {" ".join(bits)}>{_attr(summary)}</sub_agent>')
            lines.append("  </wave>")
        lines.append("</sub_agents>")
        return "\n".join(lines)


sub_agent_service = SubAgentService()


# ─────────────────────────────────────────────────────────────────────────────
# Module-level jobs (APScheduler serialises callables by import path)
# ─────────────────────────────────────────────────────────────────────────────

async def run_wave_deadline(job_id: str, wave_id: str) -> None:
    """Deadline fired: stop stragglers, record them as timed out, fan in."""
    from app.core.scheduler import claim_scheduled_run
    from app.dependencies import async_session_maker
    if not await asyncio.to_thread(claim_scheduled_run, job_id):
        return
    svc = sub_agent_service
    async with async_session_maker() as db:
        wave = await db.get(SubAgentWave, wave_id)
        if wave is None:
            return
        res = await db.execute(
            update(SubAgentWave)
            .where(SubAgentWave.id == wave.id, SubAgentWave.status == WAVE_RUNNING)
            .values(status=WAVE_TIMED_OUT, fired_at=datetime.utcnow())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if (res.rowcount or 0) != 1:
            return
        children = (await db.execute(select(SubAgentRun).where(SubAgentRun.wave_id == wave.id))).scalars().all()
        for c in children:
            if c.status in TERMINAL_RUN_STATUSES:
                continue
            await svc._mark_terminal(db, c, RUN_TIMED_OUT, EXIT_DEADLINE, error="wave deadline reached")
            if c.child_completion_id:
                await _sigkill_completion(db, c.child_completion_id)
            c.envelope_json = _envelope(c)
            await db.commit()
            await svc._write_back(db, c)
    await run_fan_in(wave_id)


async def run_fan_in(wave_id: str) -> None:
    """One machine turn per wave, carrying every record, queued behind a running user turn.

    trigger_source is ``sub_agent_wave`` (not ``sub_agent``): children are hidden
    completions, the fan-in reply is a visible turn."""
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.machine_turn import run_machine_turn

    # Serialise behind a user turn: the fan-in must not run on top of one.
    waited = 0.0
    while waited < _FAN_IN_WAIT_SECONDS:
        async with async_session_maker() as db:
            if not await _report_busy(db, wave_id):
                break
        await asyncio.sleep(_SLOT_POLL_SECONDS)
        waited += _SLOT_POLL_SECONDS

    async with async_session_maker() as db:
        wave = await db.get(SubAgentWave, wave_id)
        if wave is None or wave.status not in (WAVE_FIRING, WAVE_TIMED_OUT):
            return
        report = await db.get(Report, wave.report_id)
        organization = await db.get(Organization, wave.organization_id)
        user = await db.get(User, wave.user_id) if wave.user_id else None
        if not (report and organization and user) or report.deleted_at:
            logger.warning("fan-in for wave %s: report/org/user gone", wave_id)
            return
        children = (await db.execute(
            select(SubAgentRun).where(SubAgentRun.wave_id == wave.id).order_by(SubAgentRun.wave_index.asc())
        )).scalars().all()
        instruction = _fan_in_prompt(wave, children)
        counts: Dict[str, int] = {}
        for c in children:
            o = (c.result_json or {}).get("outcome") or c.status
            counts[o] = counts.get(o, 0) + 1
        summary_bits = [f"{n} {k}" for k, n in counts.items()]
        summary = f"{len(children)} sub-agent{'s' if len(children) != 1 else ''} finished · " + " · ".join(summary_bits)
        if wave.status == WAVE_TIMED_OUT:
            summary += " · deadline reached"
        meta = {"wave_id": str(wave.id), "total": len(children), "counts": counts,
                "timed_out": wave.status == WAVE_TIMED_OUT}
        wave_id_str = str(wave.id)

        def _on_event(event):
            # Runs in the same session before the agent turn: bind the strip.
            try:
                wave.fan_in_completion_id = str(event.id)
                wave.status = WAVE_DONE
            except Exception:
                pass

        try:
            await run_machine_turn(
                db, report=report, user=user, organization=organization,
                summary=summary[:300], trigger_source="sub_agent_wave", message_type="sub_agent_wave_done",
                instruction=instruction, meta=meta, mode=(wave.parent_mode or None), on_event=_on_event,
            )
        except Exception:
            logger.exception("fan-in turn for wave %s failed", wave_id)
        finally:
            try:
                w2 = await db.get(SubAgentWave, wave_id)
                if w2 is not None:
                    w2.status = WAVE_DONE
                    if not w2.fan_in_completion_id and wave.fan_in_completion_id:
                        w2.fan_in_completion_id = wave.fan_in_completion_id
                    await db.commit()
            except Exception:
                pass


async def _report_busy(db, wave_id: str) -> bool:
    from app.models.completion import Completion
    wave = await db.get(SubAgentWave, wave_id)
    if wave is None:
        return False
    running = (await db.execute(
        select(Completion.id).where(
            Completion.report_id == wave.report_id,
            Completion.role == "system",
            Completion.status == "in_progress",
            (Completion.trigger_source.is_(None)) | (Completion.trigger_source != "sub_agent"),
        ).limit(1)
    )).scalar_one_or_none()
    return running is not None


async def _sigkill_completion(db, completion_id: str) -> None:
    """Stop a child run the way the stop button does, minus the user-facing
    'Run was stopped' session event (children are hidden)."""
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    comp = await db.get(Completion, completion_id)
    if comp is None:
        return
    comp.sigkill = datetime.now()
    if comp.status == "in_progress":
        comp.status = "stopped"
    db.add(comp)
    blocks = (await db.execute(
        select(CompletionBlock).where(CompletionBlock.completion_id == completion_id, CompletionBlock.status == "in_progress")
    )).scalars().all()
    for b in blocks:
        b.status = "stopped"
        if not b.completed_at:
            b.completed_at = comp.sigkill
        db.add(b)
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Rendering helpers
# ─────────────────────────────────────────────────────────────────────────────

def _provider_matches(model, provider_part: str) -> bool:
    prov = getattr(model, "provider", None)
    names = {str(getattr(prov, "provider_type", "") or "").lower(), str(getattr(prov, "name", "") or "").lower()}
    return provider_part.lower() in names


def _attr(s: Any) -> str:
    return str(s or "").replace('"', "'").replace("<", "‹").replace(">", "›")


def _child_prompt(row: SubAgentRun) -> str:
    ctx = (row.context or "").strip() or "(none given — rely on the schema and instructions in your context)"
    return (
        f"GOAL\n{row.goal.strip()}\n\n"
        f"CONTEXT FROM THE PARENT\n{ctx}\n\n"
        f"Finish by calling finish_subtask. Your final text is not delivered to anyone."
    )


def _envelope(row: SubAgentRun, *, duration_ms: Optional[float] = None, model_name: Optional[str] = None) -> Dict[str, Any]:
    dur = duration_ms
    if dur is None and row.started_at and row.finished_at:
        dur = (row.finished_at - row.started_at).total_seconds() * 1000.0
    return {
        "status": row.status, "exit_reason": row.exit_reason,
        "steps_used": row.steps_used, "max_steps": row.max_steps,
        "duration_ms": dur, "tokens": row.tokens_json,
        "model": model_name or row.model_name,
        "child_completion_id": row.child_completion_id, "child_execution_id": row.child_execution_id,
        "truncated": False,
    }


def _synthesized_result(row: SubAgentRun, status: str, exit_reason: str, last_text: str, error: Optional[str]) -> Dict[str, Any]:
    text = (last_text or "").strip()
    if exit_reason == EXIT_STOPPED:
        summary = "Stopped before finishing." + (f" Last notes: {text[:300]}" if text else "")
        outcome = "partial"
    elif exit_reason == EXIT_DEADLINE:
        summary = "Wave deadline reached before this sub-agent finished." + (f" Last notes: {text[:300]}" if text else "")
        outcome = "partial"
    elif exit_reason == EXIT_ERROR:
        summary = f"The sub-agent failed: {(error or 'unknown error')[:300]}"
        outcome = "blocked"
    elif exit_reason == EXIT_MAX_TOKENS:
        summary = "Token budget exhausted before a finish_subtask call." + (" Its note holds what it checked so far." if row.report_note_id else "")
        outcome = "partial"
    elif exit_reason == EXIT_MAX_STEPS:
        summary = "Step budget exhausted before a finish_subtask call." + (f" Last text: {text[:300]}" if text else "")
        outcome = "partial"
    elif text.startswith("Stopping:") or "called" in text[:80] and "identical" in text[:160]:
        # The repeated-call breaker ended the run; its message is for the
        # model, not a finding. Point the parent at the child's note instead.
        summary = (
            "Stopped after repeating the same tool call before writing its record; nothing was concluded. "
            + ("Its note holds what it checked so far." if row.report_note_id else "It left no note.")
        )
        outcome = "partial"
    else:
        summary = (text[:560] + ("…" if len(text) > 560 else "")) if text else "The sub-agent ended without calling finish_subtask."
        outcome = "partial"
    return {
        "outcome": outcome, "confidence": 0.2 if outcome == "partial" else 0.0,
        "summary": summary, "findings": [], "ruled_out": [], "evidence": [],
        "caveats": [f"synthesized by the runtime ({exit_reason}); not authored by the sub-agent"],
        "unknown": None, "report_note_id": row.report_note_id, "synthesized": True,
    }


def _run_brief(r: SubAgentRun) -> Dict[str, Any]:
    rec = r.result_json or {}
    return {
        "sub_agent_id": str(r.id), "title": r.title, "profile": r.profile, "status": r.status,
        "exit_reason": r.exit_reason, "outcome": rec.get("outcome"), "confidence": rec.get("confidence"),
        "summary": rec.get("summary"), "steps_used": r.steps_used, "max_steps": r.max_steps,
        "tokens": r.tokens_json, "model": r.model_name,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "parent_tool_execution_id": r.parent_tool_execution_id,
        "parent_completion_id": r.parent_completion_id,
        "child_completion_id": r.child_completion_id, "child_execution_id": r.child_execution_id,
        "report_note_id": r.report_note_id,
    }


def _fan_in_prompt(wave: SubAgentWave, children: List[SubAgentRun]) -> str:
    lines = [
        "[Sub-agent wave finished] The sub-agents you launched earlier have all reported. Their records are below, in launch order; each answered the goal you gave it. "
        "Synthesize now: answer the original ask from these records, cite evidence ids (read_query / load_step / run_query on them when a claim is surprising or you need the numbers), "
        "carry every ruled_out item and caveat into your answer, and treat an inconclusive or blocked record with a named unknown as a follow-up (or a second wave), never a guess. "
        "A record marked synthesized was written by the runtime, not the sub-agent — weigh it accordingly. "
        "Use read_sub_agent(sub_agent_id, include_report=true) for a sub-agent's full write-up when you need detail. "
        "Do not relaunch sub-agents for goals that already have a record. Never show sub-agent ids to the user.",
        "",
        f"<original_ask>{_attr(wave.parent_prompt or '')}</original_ask>",
        f'<sub_agent_results wave="{wave.id}" status="{wave.status}" total="{len(children)}">',
    ]
    for c in children:
        rec = c.result_json or {}
        env = c.envelope_json or {}
        lines.append(
            f'  <sub_agent id="{c.id}" title="{_attr(c.title)}" profile="{c.profile}" status="{c.status}" '
            f'exit_reason="{c.exit_reason or ""}" outcome="{rec.get("outcome", "")}" confidence="{rec.get("confidence", "")}" '
            f'steps="{c.steps_used or ""}"{" synthesized=\"true\"" if rec.get("synthesized") else ""}>'
        )
        lines.append(f"    <goal>{_attr(c.goal)}</goal>")
        lines.append(f"    <summary>{_attr(rec.get('summary', ''))}</summary>")
        if rec.get("findings"):
            lines.append("    <findings>")
            for f in rec["findings"]:
                if isinstance(f, dict):
                    extra = " ".join(x for x in (
                        f"[{f.get('evidence_ref')}]" if f.get("evidence_ref") else "",
                        f"({f.get('magnitude')})" if f.get("magnitude") else "",
                    ) if x)
                    lines.append(f"      - {_attr(f.get('claim', ''))} {extra}".rstrip())
            lines.append("    </findings>")
        if rec.get("ruled_out"):
            lines.append("    <ruled_out>" + " | ".join(_attr(x) for x in rec["ruled_out"]) + "</ruled_out>")
        if rec.get("evidence"):
            lines.append("    <evidence>" + " | ".join(
                f"{e.get('kind')}:{e.get('id')} {_attr(e.get('label', ''))}".strip() for e in rec["evidence"] if isinstance(e, dict)
            ) + "</evidence>")
        if rec.get("caveats"):
            lines.append("    <caveats>" + " | ".join(_attr(x) for x in rec["caveats"]) + "</caveats>")
        if rec.get("unknown"):
            lines.append(f"    <unknown>{_attr(rec['unknown'])}</unknown>")
        if c.report_note_id:
            lines.append(f"    <report_note_id>{c.report_note_id}</report_note_id>")
        if c.error_message:
            lines.append(f"    <error>{_attr(c.error_message)}</error>")
        lines.append("  </sub_agent>")
    lines.append("</sub_agent_results>")
    return "\n".join(lines)
