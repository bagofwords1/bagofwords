"""Agent reliability automation — the self-learning orchestrator.

Implements the bounded loop:

    trigger -> run evals (baseline) ->
        all pass?         -> done (passed)
        some fail?        -> remediate (reference-repair, then training) ->
                             re-run evals against the *candidate* build ->
                                 pass & no regression -> approve/promote (or leave pending)
                                 still fail           -> retry up to max_iterations
        gave up           -> apply on_repeated_failure (training | development | none)

Design notes
------------
* **Eval against the candidate build, not main.** Re-runs are scoped to the
  draft build the loop is building so we measure the *fix*, not current state.
* **Regression guard.** "Pass" means the failing cases now pass *and* no
  previously-passing case regressed — scored on the full agent suite.
* **Concurrency.** One loop per agent at a time (in-process guard + a DB check
  for an already-``running`` AgentAutomationRun).
* **Auditability.** Every firing writes one ``AgentAutomationRun`` row linking
  the candidate build and the spawned TestRuns.

The two heavy, LLM-dependent steps — running an eval suite to completion and
generating training instructions — are isolated behind ``_evaluate`` and
``_train_iteration`` so the state machine can be unit-tested deterministically
by subclassing/patching them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select, or_, cast, String as SAString

from app.models.agent_automation_run import (
    AgentAutomationRun,
    STATUS_RUNNING,
    STATUS_PASSED,
    STATUS_PASSED_PENDING,
    STATUS_GAVE_UP,
    STATUS_NO_EVALS,
    STATUS_SKIPPED,
    STATUS_ERROR,
    TRIGGER_MANUAL,
    TRIGGER_TABLE_CHANGE,
    TRIGGER_INSTRUCTION_CHANGE,
    TRIGGER_GLOBAL_CHANGE,
    TRIGGER_SUGGESTION,
)
from app.models.data_source import DataSource
from app.models.eval import TestCase, TestResult, TEST_CASE_STATUS_ACTIVE
from app.models.membership import Membership
from app.models.user import User
from app.schemas.agent_automation_schema import (
    AgentAutomationPolicy,
    resolve_policy,
    AUTONOMY_OFF,
    AUTONOMY_SUGGEST,
    AUTONOMY_AUTO,
    ON_FAILURE_DEVELOPMENT,
    ON_FAILURE_TRAINING,
    MODE_EVAL_AUTO,
)

logger = logging.getLogger(__name__)

# Maps a trigger to the policy stage that gates it.
_TRIGGER_STAGE = {
    TRIGGER_TABLE_CHANGE: "eval_on_table_change",
    TRIGGER_INSTRUCTION_CHANGE: "eval_on_change",
    TRIGGER_GLOBAL_CHANGE: "eval_on_global_change",
    TRIGGER_MANUAL: None,  # manual always runs (the human asked for it)
}

# Per-process guard so two triggers for the same agent don't run concurrently
# inside one worker. Cross-process safety is provided by the DB ``running``
# check in :meth:`_already_running`.
_INFLIGHT: Set[str] = set()


class AgentReliabilityService:
    """Orchestrates the per-agent reliability loop. Stateless except for the
    process-wide in-flight guard."""

    # ----- settings resolution ------------------------------------------------

    async def resolve_policy(
        self, db, organization, data_source: DataSource
    ) -> AgentAutomationPolicy:
        """Effective policy = org defaults merged with the per-agent override."""
        org_defaults: Optional[Dict[str, Any]] = None
        try:
            settings = await organization.get_settings(db)
            org_defaults = settings.get_config("agent_automation_defaults")
        except Exception:
            org_defaults = None
        if hasattr(org_defaults, "value"):  # tolerate FeatureConfig wrapping
            org_defaults = getattr(org_defaults, "value", None)

        agent_override = getattr(data_source, "automation_settings", None)
        return resolve_policy(
            org_defaults if isinstance(org_defaults, dict) else None,
            agent_override if isinstance(agent_override, dict) else None,
        )

    # ----- eval discovery -----------------------------------------------------

    async def list_agent_eval_case_ids(
        self, db, organization_id: str, data_source_id: str, statuses: Optional[Set[str]] = None
    ) -> List[str]:
        """Active eval cases that belong to this agent.

        Membership = the case's ``data_source_ids_json`` either **includes** this
        agent, or is **Auto/empty** (no explicit agents), which means "all agents"
        — the test-case analogue of a global instruction. Org-scoped via the
        suite chain so Auto cases from other orgs don't leak in.
        """
        from app.models.eval import TestSuite
        statuses = statuses or {TEST_CASE_STATUS_ACTIVE}
        stmt = (
            select(TestCase)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(TestSuite.organization_id == str(organization_id))
            .where(TestCase.deleted_at.is_(None))
        )
        rows = (await db.execute(stmt)).scalars().all()
        out: List[str] = []
        for c in rows:
            if c.status not in statuses:
                continue
            ds_ids = c.data_source_ids_json or []
            is_auto = not (isinstance(ds_ids, list) and len(ds_ids) > 0)
            if is_auto or str(data_source_id) in [str(x) for x in ds_ids]:
                out.append(str(c.id))
        return out

    # ----- concurrency guard --------------------------------------------------

    async def _already_running(self, db, data_source_id: str) -> bool:
        stmt = select(AgentAutomationRun.id).where(
            AgentAutomationRun.data_source_id == str(data_source_id),
            AgentAutomationRun.status == STATUS_RUNNING,
        ).limit(1)
        return (await db.execute(stmt)).first() is not None

    # ----- actor resolution ---------------------------------------------------

    async def _resolve_actor_user(
        self, db, organization_id: str, preferred: Optional[User]
    ) -> Optional[User]:
        """A user to attribute eval runs / builds to. Prefer the request user
        (manual trigger); for system triggers pick an admin/owner of the org."""
        if preferred is not None:
            return preferred
        stmt = (
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.organization_id == str(organization_id),
                Membership.role.in_(["admin", "owner"]),
            )
            .limit(1)
        )
        user = (await db.execute(stmt)).scalars().first()
        if user is not None:
            return user
        # Fall back to any member.
        stmt = (
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == str(organization_id))
            .limit(1)
        )
        return (await db.execute(stmt)).scalars().first()

    # =====================================================================
    # The state machine
    # =====================================================================

    async def run_automation(
        self,
        db,
        organization,
        data_source: DataSource,
        trigger: str,
        *,
        user: Optional[User] = None,
        changed_hint: Optional[str] = None,
        train_override: Optional[str] = None,
        brief: Optional[str] = None,
    ) -> AgentAutomationRun:
        """Run the full loop for one agent. Always returns an
        AgentAutomationRun row describing the outcome (never raises into the
        caller for expected control-flow; unexpected errors are recorded as
        ``error``)."""
        ds_id = str(data_source.id)
        org_id = str(organization.id)

        policy = await self.resolve_policy(db, organization, data_source)

        # Gate on the trigger's stage autonomy.
        stage = _TRIGGER_STAGE.get(trigger)
        autonomy = AUTONOMY_AUTO if stage is None else policy.stage(stage)
        if autonomy == AUTONOMY_OFF:
            return await self._record(
                db, org_id, ds_id, trigger, STATUS_SKIPPED, user=user,
                detail={"reason": "trigger autonomy is off", "policy_stage": stage},
            )

        if ds_id in _INFLIGHT or await self._already_running(db, ds_id):
            return await self._record(
                db, org_id, ds_id, trigger, STATUS_SKIPPED, user=user,
                detail={"reason": "an automation run is already in progress for this agent"},
            )

        case_ids = await self.list_agent_eval_case_ids(db, org_id, ds_id)
        if not case_ids:
            return await self._record(
                db, org_id, ds_id, trigger, STATUS_NO_EVALS, user=user,
                detail={"reason": "no active evals scoped to this agent", "hint": changed_hint},
            )

        actor = await self._resolve_actor_user(db, org_id, user)
        if actor is None:
            return await self._record(
                db, org_id, ds_id, trigger, STATUS_ERROR, user=user,
                detail={"reason": "could not resolve an actor user for the org"},
            )

        _INFLIGHT.add(ds_id)
        run = await self._record(
            db, org_id, ds_id, trigger, STATUS_RUNNING, user=user,
            detail={"hint": changed_hint, "policy": policy.model_dump()},
            started=True,
        )
        test_run_ids: List[str] = []
        detail: Dict[str, Any] = {"hint": changed_hint, "iterations": []}
        try:
            # --- Baseline: measure against the current main build. -----------
            baseline = await self._evaluate(
                db, organization, actor, case_ids, build_id=None, trigger=trigger,
            )
            test_run_ids.extend(baseline.get("run_ids") or ([baseline["run_id"]] if baseline.get("run_id") else []))
            detail["baseline"] = baseline["summary"]
            baseline_passing = set(baseline["passing_case_ids"])

            if baseline["failed"] == 0 and baseline["errored"] == 0:
                return await self._finish(
                    db, run, STATUS_PASSED, iterations=0, build_id=None,
                    test_run_ids=test_run_ids,
                    detail={**detail, "reason": "evals green at baseline"},
                )

            # --- Remediation gated on train_on_failure. ----------------------
            # An à-la-carte Review action can override the saved policy for this
            # one invocation: "off" => eval-only (report), "auto" => force train.
            effective_train = train_override or policy.stage("train_on_failure")
            if effective_train == AUTONOMY_OFF:
                # We measured and reported, but aren't allowed to fix.
                return await self._finish(
                    db, run, STATUS_GAVE_UP, iterations=0, build_id=None,
                    test_run_ids=test_run_ids,
                    detail={**detail, "reason": "evals failing; train_on_failure is off (report-only)"},
                    apply_outcome=policy, organization=organization, data_source=data_source,
                )

            candidate_build_id: Optional[str] = None
            passed = False
            iterations_done = 0
            for i in range(policy.max_iterations):
                iterations_done = i + 1
                step = await self._train_iteration(
                    db, organization, actor, data_source,
                    failing_case_ids=baseline["failing_case_ids"],
                    build_id=candidate_build_id,
                    trigger=trigger,
                    iteration=iterations_done,
                    brief=brief or changed_hint,
                )
                candidate_build_id = step["build_id"]

                result = await self._evaluate(
                    db, organization, actor, case_ids,
                    build_id=candidate_build_id, trigger=trigger,
                )
                test_run_ids.extend(result.get("run_ids") or ([result["run_id"]] if result.get("run_id") else []))

                now_passing = set(result["passing_case_ids"])
                regressed = sorted(baseline_passing - now_passing)
                green = result["failed"] == 0 and result["errored"] == 0

                detail["iterations"].append({
                    "iteration": iterations_done,
                    "build_id": candidate_build_id,
                    "summary": result["summary"],
                    "training": step.get("summary"),
                    "regressed_case_ids": regressed,
                })

                if green and not regressed:
                    passed = True
                    break
                # else keep iterating against the same candidate build

            if passed:
                return await self._promote_or_pend(
                    db, run, organization, actor, policy,
                    build_id=candidate_build_id, iterations=iterations_done,
                    test_run_ids=test_run_ids, detail=detail,
                )

            # Exhausted iterations without a clean pass.
            return await self._finish(
                db, run, STATUS_GAVE_UP, iterations=iterations_done,
                build_id=candidate_build_id, test_run_ids=test_run_ids,
                detail={**detail, "reason": "could not reach green within max_iterations"},
                apply_outcome=policy, organization=organization, data_source=data_source,
            )
        except Exception as e:  # noqa: BLE001 — never let a trigger crash the caller
            logger.exception("agent_reliability.loop_failed ds=%s: %s", ds_id, e)
            return await self._finish(
                db, run, STATUS_ERROR, iterations=detail.get("iterations") and len(detail["iterations"]) or 0,
                build_id=None, test_run_ids=test_run_ids,
                detail={**detail, "error": str(e)},
            )
        finally:
            _INFLIGHT.discard(ds_id)

    # ----- promotion ----------------------------------------------------------

    async def _promote_or_pend(
        self, db, run, organization, actor, policy, *, build_id, iterations, test_run_ids, detail,
    ) -> AgentAutomationRun:
        """Evals are green on the candidate build. Either auto-approve+promote it
        (approve_instructions == auto) or leave it pending human approval."""
        if not build_id:
            return await self._finish(
                db, run, STATUS_PASSED, iterations=iterations, build_id=None,
                test_run_ids=test_run_ids,
                detail={**detail, "reason": "evals green; no build changes were needed"},
            )

        if policy.stage("approve_instructions") == AUTONOMY_AUTO:
            from app.services.build_service import BuildService
            bs = BuildService()
            try:
                await bs.submit_build(db, str(build_id), user_id=str(actor.id))
                await bs.approve_build(db, str(build_id), approved_by_user_id=str(actor.id))
                await bs.promote_build(db, str(build_id), user_id=str(actor.id))
                # Healthy again.
                await self._set_reliability_status(db, run.data_source_id, "ok")
                return await self._finish(
                    db, run, STATUS_PASSED, iterations=iterations, build_id=str(build_id),
                    test_run_ids=test_run_ids,
                    detail={**detail, "reason": "evals green; candidate build auto-approved and promoted"},
                )
            except Exception as e:
                logger.exception("agent_reliability.promote_failed build=%s: %s", build_id, e)
                return await self._finish(
                    db, run, STATUS_PASSED_PENDING, iterations=iterations, build_id=str(build_id),
                    test_run_ids=test_run_ids,
                    detail={**detail, "reason": f"evals green but auto-promote failed: {e}; left for review"},
                )

        # suggest mode: submit for human approval, don't promote.
        from app.services.build_service import BuildService
        bs = BuildService()
        try:
            await bs.submit_build(db, str(build_id), user_id=str(actor.id))
        except Exception:
            pass
        return await self._finish(
            db, run, STATUS_PASSED_PENDING, iterations=iterations, build_id=str(build_id),
            test_run_ids=test_run_ids,
            detail={**detail, "reason": "evals green; candidate build submitted for human approval"},
        )

    # ----- outcome application ------------------------------------------------

    async def _apply_failure_outcome(self, db, organization, data_source, policy) -> str:
        """On give-up, apply on_repeated_failure. Returns the action taken."""
        action = policy.on_repeated_failure
        if action == ON_FAILURE_DEVELOPMENT:
            # Pull the agent from regular users (hidden from selector + AI
            # context); agent admins keep access to keep fixing it. We do NOT
            # touch publish_status — that stays a purely human-owned dial.
            data_source.reliability_status = "development"
            db.add(data_source)
            await db.commit()
            return "development"
        if action == ON_FAILURE_TRAINING:
            data_source.reliability_status = "training"
            db.add(data_source)
            await db.commit()
            return "training"
        return "none"

    async def _set_reliability_status(self, db, data_source_id: str, status: str) -> None:
        ds = await db.get(DataSource, str(data_source_id))
        if ds is not None:
            ds.reliability_status = status
            db.add(ds)
            await db.commit()

    # =====================================================================
    # Heavy steps (isolated seams) — real default implementations
    # =====================================================================

    async def _evaluate(
        self, db, organization, actor, case_ids: List[str], *, build_id: Optional[str], trigger: str,
    ) -> Dict[str, Any]:
        """Run the given eval cases against ``build_id`` (or current main when
        None) to completion and return pass/fail counts + which cases passed.

        Each case runs as its own single-case TestRun, executed **sequentially**.
        ``stream_run`` fans every non-terminal result in a run out to concurrent
        agent tasks that share setup objects bound to this session; driving a
        multi-case run inline (draining the SSE body here, outside Starlette)
        deadlocks on that shared session. One case per run sidesteps it and is
        only marginally slower for the small suites this loop targets.
        """
        from app.services.test_run_service import TestRunService
        trs = TestRunService()

        run_ids: List[str] = []
        passing: List[str] = []
        failing: List[str] = []
        passed = failed = errored = 0

        for cid in case_ids:
            run = await trs.create_run(
                db, organization, actor, case_ids=[str(cid)],
                trigger_reason=f"automation:{trigger}", build_id=build_id,
            )
            run_ids.append(str(run.id))

            # Drive this single-case run to completion by draining the SSE body.
            try:
                response = await trs.stream_run(db, organization, actor, str(run.id))
                body = getattr(response, "body_iterator", None)
                if body is not None:
                    async for _chunk in body:
                        pass
            except Exception as e:  # pragma: no cover - execution path
                logger.warning("agent_reliability._evaluate stream failed: %s", e)

            rows = (
                await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)))
            ).scalars().all()
            for r in rows:
                st = (r.status or "").lower()
                if st in ("pass", "success"):
                    passed += 1
                    passing.append(str(r.case_id))
                elif st == "fail":
                    failed += 1
                    failing.append(str(r.case_id))
                else:
                    errored += 1
                    failing.append(str(r.case_id))

        total = passed + failed + errored
        return {
            # Back-compat single id (first run) plus the full list.
            "run_id": run_ids[0] if run_ids else None,
            "run_ids": run_ids,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "passing_case_ids": passing,
            "failing_case_ids": failing,
            "summary": {"total": total, "passed": passed, "failed": failed, "errored": errored},
        }

    async def _train_iteration(
        self, db, organization, actor, data_source, *, failing_case_ids, build_id, trigger, iteration,
        brief: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Produce/extend a candidate instruction build aimed at fixing the
        failing cases.

        Step 1 (cheap, deterministic): reference-repair — re-validate the
        agent's instruction table/column references against the current schema
        and fix stale ones. This is the right first move for table-change
        triggers and harmless otherwise.

        Step 2 (LLM): training-mode instruction synthesis. Wired behind
        ``_generate_training_instructions`` which returns proposed instructions;
        the default implementation is best-effort and returns [] when a live
        training context can't be assembled, so the loop still relies on the
        deterministic repair + re-eval.
        """
        from app.services.build_service import BuildService
        bs = BuildService()
        if not build_id:
            build = await bs.create_build(
                db, str(organization.id), source="ai",
                user_id=str(actor.id), copy_from_main=True,
            )
            build_id = str(build.id)

        summary: Dict[str, Any] = {"reference_repairs": 0, "instructions_added": 0}

        repaired = await self._repair_references(db, organization, data_source, build_id)
        summary["reference_repairs"] = repaired

        proposed = await self._generate_training_instructions(
            db, organization, actor, data_source, failing_case_ids=failing_case_ids, brief=brief,
        )
        added = await self._add_instructions_to_build(
            db, organization, actor, data_source, build_id, proposed,
        )
        summary["instructions_added"] = added

        return {"build_id": str(build_id), "summary": summary}

    async def _repair_references(self, db, organization, data_source, build_id) -> int:
        """Re-validate the agent's instruction references against the live
        schema; the reference service drops/repairs stale table refs. Returns a
        best-effort count of instructions whose references were revalidated.

        Kept defensive: any failure returns 0 rather than aborting the loop.
        """
        try:
            from app.services.instruction_reference_service import InstructionReferenceService
            from app.models.instruction import Instruction
        except Exception:
            return 0

        try:
            instrs = list(getattr(data_source, "instructions", []) or [])
        except Exception:
            instrs = []
        if not instrs:
            return 0

        svc = InstructionReferenceService()
        count = 0
        for instr in instrs:
            try:
                existing = list(getattr(instr, "references", []) or [])
                if not existing:
                    continue
                # Re-run validation by replacing with the current reference set;
                # the service repairs/drops stale ids against the live schema.
                from app.schemas.instruction_reference_schema import InstructionReferenceCreate
                payload = [
                    InstructionReferenceCreate(
                        object_type=getattr(r, "object_type", "datasource_table"),
                        object_id=getattr(r, "object_id", None),
                        display_text=getattr(r, "display_text", None),
                    )
                    for r in existing
                ]
                await svc.replace_for_instruction(
                    db, str(instr.id), payload, organization,
                    data_source_ids=[str(data_source.id)],
                )
                count += 1
            except Exception:
                continue
        return count

    async def _generate_training_instructions(
        self, db, organization, actor, data_source, *, failing_case_ids,
        brief: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Hook for LLM training-mode instruction synthesis.

        Returns a list of ``{"title", "text", "category"}`` dicts. The default
        is a no-op (returns []), because the training agent's
        ``SuggestInstructions.stream_suggestions`` needs a live conversation
        context (context_view/context_hub) that isn't available from a bare
        trigger. Live training is driven through the existing knowledge harness;
        this seam lets that be injected without changing the state machine.

        ``brief`` is the per-event focus string built from the Review item
        (e.g. "fix these 5 low-confidence runs") — threaded here so a harness
        implementation can seed its training prompt with it.
        """
        return []

    async def _add_instructions_to_build(
        self, db, organization, actor, data_source, build_id, proposed: List[Dict[str, Any]],
    ) -> int:
        """Create instructions from ``proposed`` and add their versions to the
        candidate build. Returns the count added."""
        if not proposed:
            return 0
        try:
            from app.services.instruction_service import InstructionService
            from app.services.build_service import BuildService
            from app.schemas.instruction_schema import InstructionCreate
            from app.models.instruction_build import InstructionBuild
        except Exception:
            return 0
        instr_svc = InstructionService()
        build = await db.get(InstructionBuild, str(build_id))
        if build is None:
            return 0
        added = 0
        for p in proposed:
            try:
                payload = InstructionCreate(
                    text=p.get("text", ""),
                    category=p.get("category", "general"),
                    status="draft",
                    source_type="ai",
                    ai_source="reliability_automation",
                    title=p.get("title"),
                    data_source_ids=[str(data_source.id)],
                )
                await instr_svc.create_instruction(
                    db, payload, actor, organization,
                    build=build, version_status_override="published",
                )
                added += 1
            except Exception:
                continue
        return added

    # =====================================================================
    # Audit helpers
    # =====================================================================

    async def _record(
        self, db, org_id, ds_id, trigger, status, *, user=None, detail=None, started=False,
    ) -> AgentAutomationRun:
        run = AgentAutomationRun(
            organization_id=str(org_id),
            data_source_id=str(ds_id),
            trigger=trigger,
            status=status,
            iterations=0,
            detail_json=detail or {},
            requested_by_user_id=str(user.id) if user is not None else None,
            started_at=datetime.utcnow() if started else None,
            finished_at=None if status == STATUS_RUNNING else datetime.utcnow(),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    async def _finish(
        self, db, run, status, *, iterations, build_id, test_run_ids, detail,
        apply_outcome: Optional[AgentAutomationPolicy] = None,
        organization=None, data_source=None,
    ) -> AgentAutomationRun:
        if apply_outcome is not None and organization is not None and data_source is not None:
            action = await self._apply_failure_outcome(db, organization, data_source, apply_outcome)
            detail = {**(detail or {}), "outcome_action": action}
        if status == STATUS_PASSED:
            # A passing run verifies the agent is healthy — clear any prior
            # training/development flag. (New agents start in "training".)
            await self._set_reliability_status(db, run.data_source_id, "ok")
        run.status = status
        run.iterations = iterations
        run.build_id = str(build_id) if build_id else None
        run.test_run_ids_json = test_run_ids
        run.detail_json = detail or {}
        run.finished_at = datetime.utcnow()
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    # =====================================================================
    # Scheduling (fire-and-forget from triggers)
    # =====================================================================

    def schedule(self, *, organization_id: str, data_source_id: str, trigger: str, changed_hint: Optional[str] = None, user_id: Optional[str] = None) -> None:
        """Fire the loop in the background with its own DB session. Safe to call
        from request handlers / post-commit hooks. Never blocks the caller."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("agent_reliability.schedule: no running loop; skipping")
            return
        asyncio.create_task(
            self._run_scheduled(organization_id, data_source_id, trigger, changed_hint, user_id)
        )

    def schedule_for_build(self, *, organization_id: str, build_id: str) -> None:
        """Fan out a reliability check after an instruction build is promoted.

        A build whose instructions are all globally-scoped fans out to every
        agent in the org (global_change). A build touching specific agents
        triggers only those (instruction_change). Runs entirely in the
        background with its own session.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(self._run_build_fanout(organization_id, build_id))

    async def _run_build_fanout(self, organization_id: str, build_id: str) -> None:
        from app.settings.database import create_async_session_factory
        from app.models.organization import Organization
        from app.models.build_content import BuildContent
        from app.models.instruction import Instruction, instruction_data_source_association
        from app.models.agent_automation_run import (
            TRIGGER_GLOBAL_CHANGE, TRIGGER_INSTRUCTION_CHANGE,
        )
        session_factory = create_async_session_factory()
        try:
            async with session_factory() as db:
                org = await db.get(Organization, str(organization_id))
                if org is None:
                    return

                # Instructions in this build.
                instr_ids = [
                    r[0] for r in (await db.execute(
                        select(BuildContent.instruction_id).where(
                            BuildContent.build_id == str(build_id)
                        )
                    )).all()
                ]
                if not instr_ids:
                    return

                # Which data sources are these instructions scoped to?
                scoped_ds = {
                    r[0] for r in (await db.execute(
                        select(instruction_data_source_association.c.data_source_id).where(
                            instruction_data_source_association.c.instruction_id.in_(instr_ids)
                        )
                    )).all()
                }
                # An instruction with no association is global.
                has_global = len(scoped_ds) < len(instr_ids) or not scoped_ds

                if has_global:
                    # Fan out to every agent in the org; each agent's own policy
                    # decides whether the global trigger actually runs.
                    all_ds = (await db.execute(
                        select(DataSource).where(
                            DataSource.organization_id == str(organization_id),
                            DataSource.deleted_at.is_(None),
                        )
                    )).scalars().all()
                    targets = [(ds, TRIGGER_GLOBAL_CHANGE) for ds in all_ds]
                else:
                    targets = []
                    for ds_id in scoped_ds:
                        ds = await db.get(DataSource, str(ds_id))
                        if ds is not None and ds.deleted_at is None:
                            targets.append((ds, TRIGGER_INSTRUCTION_CHANGE))

                for ds, trig in targets:
                    try:
                        await self.run_automation(
                            db, org, ds, trig, user=None,
                            changed_hint=f"instruction build {build_id} promoted",
                        )
                    except Exception:
                        logger.exception("build_fanout: agent run failed ds=%s", ds.id)
        except Exception as e:  # pragma: no cover
            logger.exception("agent_reliability._run_build_fanout failed: %s", e)

    # =====================================================================
    # Self Learning: react to a brand-new suggestion build
    # =====================================================================

    def schedule_for_suggestion(self, *, organization_id: str, build_id: str, user_id: Optional[str] = None) -> None:
        """Fire the Self-Learning decision tree for a freshly-created suggestion
        build, in the background with its own session. Called from the two
        suggestion-creation sites (knowledge harness + manual add); NOT from
        report training-mode finalize."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(self._run_suggestion(organization_id, build_id, user_id))

    async def _run_suggestion(self, organization_id: str, build_id: str, user_id: Optional[str]) -> None:
        from app.settings.database import create_async_session_factory
        from app.models.organization import Organization
        session_factory = create_async_session_factory()
        try:
            async with session_factory() as db:
                org = await db.get(Organization, str(organization_id))
                if org is None:
                    return
                user = await db.get(User, str(user_id)) if user_id else None
                await self.run_for_suggestion(db, org, str(build_id), user=user)
        except Exception as e:  # pragma: no cover
            logger.exception("agent_reliability._run_suggestion failed: %s", e)

    async def _resolve_suggestion_agents(self, db, organization_id: str, build_id: str) -> List[DataSource]:
        """The agents (data sources) a suggestion build affects. Mirrors the
        build-fanout scoping: an instruction with no DS association is global, so
        a build containing any global instruction affects every agent."""
        from app.models.build_content import BuildContent
        from app.models.instruction import instruction_data_source_association

        instr_ids = [r[0] for r in (await db.execute(
            select(BuildContent.instruction_id).where(BuildContent.build_id == str(build_id))
        )).all()]
        if not instr_ids:
            return []
        scoped_ds = {r[0] for r in (await db.execute(
            select(instruction_data_source_association.c.data_source_id).where(
                instruction_data_source_association.c.instruction_id.in_(instr_ids)
            )
        )).all()}
        has_global = len(scoped_ds) < len(instr_ids) or not scoped_ds
        if has_global:
            return list((await db.execute(
                select(DataSource).where(
                    DataSource.organization_id == str(organization_id),
                    DataSource.deleted_at.is_(None),
                )
            )).scalars().all())
        out: List[DataSource] = []
        for ds_id in scoped_ds:
            ds = await db.get(DataSource, str(ds_id))
            if ds is not None and ds.deleted_at is None:
                out.append(ds)
        return out

    async def run_for_suggestion(
        self, db, organization, build_id: str, *, user: Optional[User] = None,
    ) -> List[AgentAutomationRun]:
        """Apply each affected agent's Self-Learning policy to a new suggestion
        build. Returns the AgentAutomationRun rows recorded (one per agent that
        had an opinion).

        Decision (aggregated across affected agents, because promotion is
        org-wide — a build either goes live or it doesn't):

          * Any affected agent wants evals (``auto_run_eval``) -> evaluate the
            suggestion build itself (pinned), then auto-promote iff green; a
            failed run stays in Review (or kicks the fix loop).
          * Else, if every enabled agent ``auto_approve_suggestions`` -> promote
            immediately, no evals.
          * Else -> leave it in the Review feed (no-op).

        Failure never demotes the *live* agent (main is untouched by a failed
        proposal) — we just record the failed run; the suggestion stays
        reviewable.
        """
        from app.services.build_service import BuildService
        bs = BuildService()
        org_id = str(organization.id)

        build = await bs.get_build(db, str(build_id))
        if build is None or build.is_main or build.status not in ("draft", "pending_approval"):
            return []

        agents = await self._resolve_suggestion_agents(db, org_id, str(build_id))
        if not agents:
            return []

        # Partition affected agents by their (mutually exclusive) tree choice.
        eval_agents: List[tuple] = []     # (ds, policy)
        approve_agents: List[tuple] = []  # (ds, policy)
        for ds in agents:
            policy = await self.resolve_policy(db, organization, ds)
            if not policy.enabled:
                continue
            if policy.auto_run_eval:
                eval_agents.append((ds, policy))
            elif policy.auto_approve_suggestions:
                approve_agents.append((ds, policy))

        if not eval_agents and not approve_agents:
            return []  # nobody opted in — leave it in Review

        actor = await self._resolve_actor_user(db, org_id, user)
        if actor is None:
            return []

        records: List[AgentAutomationRun] = []

        # ---- Eval path takes precedence (stricter) ----------------------------
        if eval_agents:
            # Union of the eval-gating agents' active cases.
            case_ids: List[str] = []
            seen: Set[str] = set()
            for ds, _pol in eval_agents:
                for cid in await self.list_agent_eval_case_ids(db, org_id, str(ds.id)):
                    if cid not in seen:
                        seen.add(cid); case_ids.append(cid)

            if not case_ids:
                # Wanted to measure but nothing to measure — leave for a human.
                for ds, _pol in eval_agents:
                    records.append(await self._record(
                        db, org_id, str(ds.id), TRIGGER_SUGGESTION, STATUS_NO_EVALS, user=user,
                        detail={"reason": "auto_run_eval on but no active evals scoped to this agent",
                                "build_id": str(build_id)},
                    ))
                return records

            # Evaluate the SUGGESTION build itself (pinned snapshot).
            result = await self._evaluate(
                db, organization, actor, case_ids, build_id=str(build_id),
                trigger=TRIGGER_SUGGESTION,
            )
            test_run_ids = result.get("run_ids") or ([result["run_id"]] if result.get("run_id") else [])
            green = result["failed"] == 0 and result["errored"] == 0

            if green:
                # Promote on green only if every eval-gating agent is in
                # eval_auto mode; an eval_review agent wants a human to approve.
                promote = all(pol.mode == MODE_EVAL_AUTO for _ds, pol in eval_agents)
                if promote:
                    try:
                        if build.status == "draft":
                            await bs.submit_build(db, str(build_id), user_id=str(actor.id))
                        await bs.approve_build(db, str(build_id), approved_by_user_id=str(actor.id))
                        # Don't re-fan-out: we just measured this exact build.
                        await bs.promote_build(db, str(build_id), user_id=str(actor.id), trigger_reliability=False)
                        status, reason = STATUS_PASSED, "suggestion evals green; auto-promoted"
                    except Exception as e:
                        logger.exception("run_for_suggestion.promote_failed build=%s: %s", build_id, e)
                        status, reason = STATUS_PASSED_PENDING, f"evals green but promote failed: {e}; left for review"
                else:
                    try:
                        if build.status == "draft":
                            await bs.submit_build(db, str(build_id), user_id=str(actor.id))
                    except Exception:
                        pass
                    status, reason = STATUS_PASSED_PENDING, "suggestion evals green; awaiting human approval"
            else:
                # Failed proposal: main is untouched, so nothing to revert and
                # the live agent is NOT demoted. Leave it in Review marked failed.
                status, reason = STATUS_GAVE_UP, "suggestion evals failed; left in Review (main unchanged)"

            for ds, _pol in eval_agents:
                records.append(await self._record(
                    db, org_id, str(ds.id), TRIGGER_SUGGESTION, status, user=user,
                    detail={"reason": reason, "build_id": str(build_id),
                            "eval": result["summary"], "test_run_ids": test_run_ids},
                ))
            return records

        # ---- Auto-approve path (no evals) -------------------------------------
        try:
            if build.status == "draft":
                await bs.submit_build(db, str(build_id), user_id=str(actor.id))
            await bs.approve_build(db, str(build_id), approved_by_user_id=str(actor.id))
            await bs.promote_build(db, str(build_id), user_id=str(actor.id), trigger_reliability=False)
            status, reason = STATUS_PASSED, "auto-approved without evals (Self Learning)"
        except Exception as e:
            logger.exception("run_for_suggestion.auto_promote_failed build=%s: %s", build_id, e)
            status, reason = STATUS_PASSED_PENDING, f"auto-approve failed: {e}; left for review"
        for ds, _pol in approve_agents:
            records.append(await self._record(
                db, org_id, str(ds.id), TRIGGER_SUGGESTION, status, user=user,
                detail={"reason": reason, "build_id": str(build_id)},
            ))
        return records

    async def _run_scheduled(self, organization_id, data_source_id, trigger, changed_hint, user_id=None) -> None:
        from app.settings.database import create_async_session_factory
        from app.models.organization import Organization
        session_factory = create_async_session_factory()
        try:
            async with session_factory() as db:
                org = await db.get(Organization, str(organization_id))
                ds = await db.get(DataSource, str(data_source_id))
                if org is None or ds is None:
                    return
                user = await db.get(User, str(user_id)) if user_id else None
                await self.run_automation(
                    db, org, ds, trigger, user=user, changed_hint=changed_hint,
                )
        except Exception as e:  # pragma: no cover
            logger.exception("agent_reliability._run_scheduled failed: %s", e)
