from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_suite import TestRun, TestResult, TestCase
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.models.step import Step
from app.models.completion import Completion
from app.schemas.test_expectations import (
    ExpectationsSpec,
    Rule,
    FieldRule,
    ToolCallsRule,
    OrderingRule,
    Matcher,
)
from app.schemas.test_results_schema import (
    RuleResult,
    RuleEvidence,
    TestResultTotals,
    TestResultJsonSchema,
)
from app.schemas.completion_v2_schema import CompletionsV2Response
from app.services.completion_service import CompletionService
from app.ai.agents.judge.judge import Judge


class TestEvaluationService:
    """
    End-of-run evaluator that produces a rule-aligned result_json.

    - Input spec: ExpectationsSpec (Pydantic), read from TestCase.expectations_json
    - Output: TestResultJsonSchema with rule_results aligned to expectations.rules order
    - No spec duplication in results; each rule has a corresponding RuleResult
    """

    def __init__(self) -> None:
        self.completions = CompletionService()

    async def resolve_by_run_and_report(
        self,
        db: AsyncSession,
        run_id: str,
        report_id: str,
    ) -> Tuple[TestRun, TestResult, TestCase, Dict[str, Any]]:
        # Resolve run
        run = (
            await db.execute(select(TestRun).where(TestRun.id == run_id))
        ).scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Test run not found")

        # Resolve result by (run_id, report_id)
        result = (
            await db.execute(
                select(TestResult)
                .where(TestResult.run_id == str(run.id))
                .where(TestResult.report_id == str(report_id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if not result:
            raise HTTPException(status_code=404, detail="Test result not found for this report")

        # Resolve case
        case = (
            await db.execute(select(TestCase).where(TestCase.id == str(result.case_id)))
        ).scalar_one_or_none()
        if not case:
            raise HTTPException(status_code=404, detail="Test case not found")

        # Expectations as Pydantic spec (strict but resilient)
        try:
            raw = getattr(case, "expectations_json", {}) or {}
            expectations = ExpectationsSpec.model_validate(raw)
        except Exception:
            expectations = ExpectationsSpec.model_validate({"rules": []})

        return run, result, case, expectations

    async def build_final_snapshot(self, db: AsyncSession, report_id: str) -> Dict[str, Any]:
        """
        Build a lightweight snapshot needed by the evaluator.

        Returns:
            {
              "tool_sequence": [str],
              "create_data": {"columns": [str], "rows_count": int, "code": str},
              "completion_text": str
            }
        """
        snapshot: Dict[str, Any] = {}

        # Tool sequence for the report (ordered by start time)
        try:
            rows = await db.execute(
                select(ToolExecution.tool_name)
                .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                .where(AgentExecution.report_id == str(report_id))
                .order_by(ToolExecution.started_at.asc(), ToolExecution.created_at.asc())
            )
            snapshot["tool_sequence"] = [tool for (tool,) in rows.all()]
        except Exception:
            snapshot["tool_sequence"] = []

        # Latest Step-derived fields for create_data assertions (best-effort)
        create_data_info = {"columns": [], "rows_count": 0, "code": ""}
        try:
            step = (
                await db.execute(
                    select(Step)
                    .where(Step.report_id == str(report_id))
                    .order_by(Step.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if step:
                # Columns from step.data.columns (support only dicts with 'field' per provided structure)
                columns = []
                try:
                    data = getattr(step, "data", None) or {}
                    raw_cols = data.get("columns") if isinstance(data, dict) else None
                    if isinstance(raw_cols, list):
                        for col in raw_cols:
                            if isinstance(col, dict):
                                f = col.get("field")
                                if isinstance(f, str) and f:
                                    columns.append(f)
                except Exception:
                    pass

                # Rows count (if step.data exists with rows[])
                rows_count = 0
                try:
                    data = getattr(step, "data", None) or {}
                    if isinstance(data, dict):
                        # Prefer info.total_rows when present; else len(rows)
                        info = data.get("info") or {}
                        if isinstance(info, dict) and isinstance(info.get("total_rows"), int):
                            rows_count = info.get("total_rows") or 0
                        elif isinstance(data.get("rows"), list):
                            rows_count = len(data["rows"])
                except Exception:
                    pass

                # Code, if available
                code = ""
                try:
                    code = getattr(step, "code", "") or ""
                except Exception:
                    code = ""

                create_data_info = {
                    "columns": columns,
                    "rows_count": rows_count,
                    "code": code,
                }
        except Exception:
            pass
        snapshot["create_data"] = create_data_info

        # Completion latest system text (optional)
        completion_text = ""
        try:
            comp = (
                await db.execute(
                    select(Completion)
                    .where(
                        Completion.report_id == str(report_id),
                        Completion.role == "system",
                    )
                    .order_by(Completion.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if comp and isinstance(comp.completion, dict):
                completion_text = comp.completion.get("content") or ""
        except Exception:
            completion_text = ""
        snapshot["completion_text"] = completion_text

        return snapshot

    async def _build_trace_v2(
        self,
        db: AsyncSession,
        report_id: str,
        organization,
        current_user,
        limit: int = 200,
    ) -> Optional[CompletionsV2Response]:
        try:
            trace = await self.completions.get_completions_v2(
                db=db,
                report_id=str(report_id),
                organization=organization,
                current_user=current_user,
                limit=limit,
            )
            return trace
        except Exception:
            return None

    async def evaluate_final(
        self,
        db: AsyncSession,
        expectations: ExpectationsSpec,
        snapshot: Dict[str, Any],
        report_id: str,
        case_prompt_text: str,
        judge: Optional[Judge] = None,
        organization=None,
        current_user=None,
        run_duration_ms: Optional[int] = None,
    ) -> Tuple[str, TestResultJsonSchema]:
        """
        Evaluate provided rules (Pydantic) against a minimal snapshot and return a rule-aligned result_json.
        """
        rules = expectations.rules or []
        rule_results: List[RuleResult] = []
        passed = 0
        failed = 0
        needs_judge = any(isinstance(r, FieldRule) and getattr(r.target, "category", "") == "judge" for r in rules)
        judge_passed: Optional[bool] = None
        judge_reason: Optional[str] = None

        # Run judge once if needed
        if needs_judge and judge is not None and organization is not None:
            try:
                trace_obj = await self._build_trace_v2(db, str(report_id), organization, current_user, limit=200)
                trace_payload = ""
                try:
                    # Prefer compact JSON for judge prompt embedding
                    if trace_obj is not None and hasattr(trace_obj, "model_dump_json"):
                        trace_payload = trace_obj.model_dump_json()
                    elif trace_obj is not None and hasattr(trace_obj, "model_dump"):
                        import json as _json
                        trace_payload = _json.dumps(trace_obj.model_dump())
                except Exception:
                    trace_payload = str(trace_obj) if trace_obj is not None else ""
                jp, jreason = await judge.judge_test_case(case_prompt_text or "", trace_payload)
                judge_passed = bool(jp)
                judge_reason = jreason
            except Exception:
                judge_passed = False
                judge_reason = "Judge evaluation failed"

        # Helper to append aligned result
        def push(ok: bool, message: Optional[str] = None, actual: Any = None, evidence: Optional[RuleEvidence] = None):
            nonlocal passed, failed, rule_results
            rule_results.append(RuleResult(ok=ok, message=message, actual=actual, evidence=evidence))
            if ok:
                passed += 1
            else:
                failed += 1

        # Iterate rules 1:1 and build aligned results
        for rule in rules:
            # Tool call counts
            if isinstance(rule, ToolCallsRule):
                seq = snapshot.get("tool_sequence") or []
                count = sum(1 for t in seq if t == rule.tool)
                min_calls = rule.min_calls or 0
                max_calls = rule.max_calls
                ok_min = count >= min_calls
                ok_max = True if max_calls is None else count <= max_calls
                ok = ok_min and ok_max
                ev = None
                if rule.tool == "clarify":
                    ev = RuleEvidence(type="clarify")
                msg = None if ok else f"{rule.tool} calls={count}, expected min={min_calls}, max={max_calls}"
                push(ok, msg, actual=count, evidence=ev)
                continue

            # Ordering ignored in v1
            if isinstance(rule, OrderingRule):
                push(True)  # not evaluated; treat as pass to preserve alignment
                continue

            # Field-level rules
            if isinstance(rule, FieldRule):
                cat = rule.target.category
                field = rule.target.field

                # completion.*
                if cat == "completion":
                    value = ""
                    if field == "text":
                        value = snapshot.get("completion_text") or ""
                    elif field == "reasoning":
                        value = ""  # not tracked separately
                    ok, msg = self._apply_matcher(value, rule.matcher)
                    ev = RuleEvidence(type="completion") if not ok else None
                    push(ok, None if ok else msg, actual=(None if ok else value), evidence=ev)
                    continue

                # judge.* (Boolean support via integrated judge run)
                if cat == "judge":
                    if judge_passed is None:
                        # No judge available → treat as failure with message
                        push(False, message=judge_reason or "Missing judge result", evidence=RuleEvidence(type="judge"))
                    else:
                        ok = bool(judge_passed)
                        msg = None if ok else (judge_reason or "Judge indicated failure")
                        ev = RuleEvidence(type="judge") if not ok else None
                        push(ok, msg, actual=(None if ok else "false"), evidence=ev)
                    continue

                # tool:create_data.*
                if cat == "tool:create_data":
                    cd = snapshot.get("create_data") or {}
                    if field == "columns":
                        values = cd.get("columns") or []
                        ok, msg = self._apply_list_matcher(values, rule.matcher)
                        ev = RuleEvidence(type="create_data") if not ok else None
                        push(ok, None if ok else msg, actual=(None if ok else values), evidence=ev)
                        continue
                    if field == "rows_count":
                        value = cd.get("rows_count") or 0
                        ok, msg = self._apply_number_matcher(value, rule.matcher)
                        ev = RuleEvidence(type="create_data") if not ok else None
                        push(ok, None if ok else msg, actual=(None if ok else value), evidence=ev)
                        continue
                    if field == "code":
                        value = cd.get("code") or ""
                        ok, msg = self._apply_matcher(value, rule.matcher)
                        ev = RuleEvidence(type="create_data") if not ok else None
                        push(ok, None if ok else msg, actual=(None if ok else value), evidence=ev)
                        continue

                # Unsupported category/field -> pass (alignment only)
                push(True)
                continue

            # Unknown rule type -> pass (alignment only)
            push(True)

        total = len(rules)
        status = "pass" if failed == 0 else "fail"
        totals = TestResultTotals(total=total, passed=passed, failed=failed, duration_ms=run_duration_ms)
        result_json = TestResultJsonSchema(totals=totals, rule_results=rule_results)
        return status, result_json

    async def persist_result_json(
        self,
        db: AsyncSession,
        result: TestResult,
        status: str,
        result_json: TestResultJsonSchema,
        failure_reason: Optional[str] = None,
        agent_execution_id: Optional[str] = None,
    ) -> None:
        """
        Persist status and result_json (and link execution).
        """
        result.status = status
        try:
            # Assign result_json to model if column exists
            result.result_json = result_json.model_dump()
        except Exception:
            # Best-effort; ignore if column not present
            pass
        if failure_reason is not None:
            result.failure_reason = failure_reason
        if agent_execution_id is not None:
            result.agent_execution_id = agent_execution_id
        db.add(result)
        await db.commit()

    # -------- Matcher helpers (Pydantic-based) --------
    def _apply_matcher(self, value: Any, matcher: Matcher) -> Tuple[bool, str]:
        t = getattr(matcher, "type", "")
        # Text family
        if t == "text.contains":
            return (isinstance(value, str) and matcher.value in value), f"must contain '{getattr(matcher, 'value', '')}'"
        if t == "text.not_contains":
            return (isinstance(value, str) and matcher.value not in value), f"must not contain '{getattr(matcher, 'value', '')}'"
        if t == "text.equals":
            return (isinstance(value, str) and value == matcher.value), f"must equal '{getattr(matcher, 'value', '')}'"
        if t == "text.regex":
            import re
            try:
                return (isinstance(value, str) and re.search(matcher.pattern, value) is not None), f"must match /{getattr(matcher, 'pattern', '')}/"
            except Exception:
                return False, "invalid regex"

        # Number cmp on scalar
        if t == "number.cmp":
            try:
                v = float(value)
                exp = float(matcher.value)
                op = matcher.op
                ops = {
                    "gt": v > exp,
                    "gte": v >= exp,
                    "lt": v < exp,
                    "lte": v <= exp,
                    "eq": v == exp,
                    "ne": v != exp,
                }
                return ops.get(op, True), f"{v} {op} {exp}"
            except Exception:
                return False, "invalid numeric comparison"

        # Length cmp on strings
        if t == "length.cmp":
            try:
                ln = len(value) if value is not None else 0
                # reuse number cmp semantics on length
                class _Tmp:
                    op = matcher.op
                    value = matcher.value
                return self._apply_number_matcher(ln, _Tmp())
            except Exception:
                return False, "invalid length comparison"

        # Unknown matcher type -> pass
        return True, "unsupported matcher (skipped)"

    def _apply_number_matcher(self, value: Any, matcher: Matcher) -> Tuple[bool, str]:
        if getattr(matcher, "type", "") != "number.cmp":
            # allow length.cmp on numeric by converting to string length if needed handled elsewhere
            return True, "unsupported matcher (skipped)"
        try:
            v = float(value)
            exp = float(matcher.value)
            op = matcher.op
            ops = {
                "gt": v > exp,
                "gte": v >= exp,
                "lt": v < exp,
                "lte": v <= exp,
                "eq": v == exp,
                "ne": v != exp,
            }
            return ops.get(op, True), f"{v} {op} {exp}"
        except Exception:
            return False, "invalid numeric comparison"

    def _apply_list_matcher(self, values: Any, matcher: Matcher) -> Tuple[bool, str]:
        t = getattr(matcher, "type", "")
        lst = list(values or []) if isinstance(values, list) else []
        if t == "list.contains_any":
            wants = list(getattr(matcher, "values", []) or [])
            ok = any(w in lst for w in wants)
            return ok, f"must contain any of {wants}"
        if t == "list.contains_all":
            wants = list(getattr(matcher, "values", []) or [])
            ok = all(w in lst for w in wants)
            return ok, f"must contain all of {wants}"
        if t == "length.cmp":
            return self._apply_number_matcher(len(lst), matcher)
        return True, "unsupported matcher (skipped)"


