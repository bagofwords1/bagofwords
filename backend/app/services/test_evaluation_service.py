from typing import Any, Dict, List, Optional, Tuple
import asyncio

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eval import TestRun, TestResult, TestCase
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
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
    RuleSpec,
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
              "create_data": {"columns": [str], "rows_count": int, "code": str, "tables": [str]},
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

        # Initialize create_data info (tool output will populate this)
        create_data_info = {"columns": [], "rows_count": 0, "code": "", "tables": []}

        # Try to resolve tables/columns/code from latest create_data tool execution
        try:
            latest_cd_row = (
                await db.execute(
                    select(ToolExecution.arguments_json, ToolExecution.result_json)
                    .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                    .where(AgentExecution.report_id == str(report_id))
                    .where(ToolExecution.tool_name == "create_data")
                    .where((ToolExecution.success == True) | (ToolExecution.status == "success"))
                    .order_by(ToolExecution.started_at.desc(), ToolExecution.created_at.desc())
                    .limit(1)
                )
            ).first()
            tables_list: list[str] = []
            args = None
            resj = None
            if latest_cd_row:
                try:
                    args, resj = latest_cd_row
                except Exception:
                    args = latest_cd_row  # backward compat with scalar_one_or_none
            if isinstance(args, dict):
                tbs = args.get("tables_by_source") or args.get("tables")
                if isinstance(tbs, dict):
                    # Flatten all table names from all sources
                    for _, arr in tbs.items():
                        if isinstance(arr, list):
                            for t in arr:
                                if isinstance(t, str) and t:
                                    tables_list.append(t)
                elif isinstance(tbs, list):
                    for entry in tbs:
                        # Two possible shapes:
                        # 1) ["public.table1","public.table2", ...]
                        # 2) [{"data_source_id": "...", "tables": ["public.table1", ...]}, ...]
                        if isinstance(entry, str):
                            tables_list.append(entry)
                        elif isinstance(entry, dict):
                            arr = entry.get("tables")
                            if isinstance(arr, list):
                                for t in arr:
                                    if isinstance(t, str) and t:
                                        tables_list.append(t)
            # Also look in result_json for tables/columns/code if present
            if isinstance(resj, dict):
                try:
                    rt = resj.get("tables")
                    if isinstance(rt, list):
                        for t in rt:
                            if isinstance(t, str) and t:
                                tables_list.append(t)
                except Exception:
                    pass
            # Deduplicate while preserving order
            seen = set()
            deduped = []
            for t in tables_list:
                if t not in seen:
                    seen.add(t)
                    deduped.append(t)
            create_data_info["tables"] = deduped
            # Fallback columns from result_json if none collected from step
            if not create_data_info.get("columns"):
                try:
                    cols = None
                    if isinstance(resj, dict):
                        cols = (
                            resj.get("columns")
                            or ((resj.get("output") or {}).get("columns"))
                            or ((resj.get("data") or {}).get("columns"))
                        )
                    collected: list[str] = []
                    if isinstance(cols, list):
                        for c in cols:
                            if isinstance(c, str) and c:
                                collected.append(c)
                            elif isinstance(c, dict):
                                for k in ("field", "name", "id"):
                                    v = c.get(k)
                                    if isinstance(v, str) and v:
                                        collected.append(v)
                                        break
                    if collected:
                        create_data_info["columns"] = collected
                except Exception:
                    pass
            # Fallback rows_count from result_json.data.info.total_rows if not set
            try:
                if not create_data_info.get("rows_count"):
                    if isinstance(resj, dict):
                        info = ((resj.get("data") or {}).get("info")) or {}
                        if isinstance(info, dict) and isinstance(info.get("total_rows"), int):
                            create_data_info["rows_count"] = int(info.get("total_rows") or 0)
            except Exception:
                pass
            # Fallback code from arguments/result_json if empty
            if not create_data_info.get("code"):
                try:
                    code = ""
                    if isinstance(args, dict):
                        code = args.get("query") or args.get("code") or ""
                    if not code and isinstance(resj, dict):
                        code = resj.get("code") or resj.get("query") or ""
                    create_data_info["code"] = code or ""
                except Exception:
                    pass
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

        # Clarify latest question text (optional) - only support arguments_json.questions (array)
        clarify_info = {"question_text": ""}
        try:
            row = (
                await db.execute(
                    select(ToolExecution.arguments_json)
                    .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                    .where(AgentExecution.report_id == str(report_id))
                    .where(ToolExecution.tool_name == "clarify")
                    .order_by(ToolExecution.started_at.desc(), ToolExecution.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if isinstance(row, dict):
                qs = row.get("questions")
                if isinstance(qs, list):
                    for item in qs:
                        if isinstance(item, str) and item.strip():
                            clarify_info["question_text"] = item
                            break
        except Exception:
            pass
        snapshot["clarify"] = clarify_info

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
        skipped = 0
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
                # Enforce a timeout so judge cannot stall the run
                try:
                    jp, jreason = await asyncio.wait_for(
                        judge.judge_test_case(case_prompt_text or "", trace_payload),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    jp, jreason = False, "Judge timeout"
                judge_passed = bool(jp)
                judge_reason = jreason
            except Exception:
                judge_passed = False
                judge_reason = "Judge evaluation failed"

        # Helper to append aligned result
        def push(ok: bool, message: Optional[str] = None, actual: Any = None, evidence: Optional[RuleEvidence] = None):
            nonlocal passed, failed, rule_results
            rule_results.append(RuleResult(ok=ok, status=("pass" if ok else "fail"), message=message, actual=actual, evidence=evidence))
            if ok:
                passed += 1
            else:
                failed += 1

        def push_skipped(message: Optional[str] = None, evidence: Optional[RuleEvidence] = None):
            # Treat unmet/precondition-missing expectations as FAIL per product decision
            nonlocal failed, rule_results
            rule_results.append(RuleResult(ok=False, status="fail", message=message or "Expectation not evaluated (unmet condition)", actual=None, evidence=evidence))
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
                push_skipped("Ordering not evaluated in v1")
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
                        ok, msg = self._apply_matcher(value, rule.matcher)
                        ev = RuleEvidence(type="completion") if not ok else None
                        # Always include actual so UI can display it
                        push(ok, None if ok else msg, actual=value, evidence=ev)
                    else:
                        # reasoning and other fields not available -> skipped
                        push_skipped(f"completion.{field} not available")
                    continue

                # judge.* (Boolean support via integrated judge run)
                if cat == "judge":
                    if judge_passed is None:
                        # No judge available → skip rather than fail to avoid false negatives
                        push_skipped(judge_reason or "Judge unavailable", evidence=RuleEvidence(type="judge", reasoning=judge_reason))
                    else:
                        ok = bool(judge_passed)
                        msg = None if ok else (judge_reason or "Judge indicated failure")
                        # Always include reasoning, and include boolean actual
                        ev = RuleEvidence(type="judge", reasoning=judge_reason)
                        push(ok, msg, actual=ok, evidence=ev)
                    continue

                # tool:create_data.*
                if cat == "tool:create_data":
                    cd = snapshot.get("create_data") or {}
                    if field == "tables":
                        values = cd.get("tables")
                        if not isinstance(values, list):
                            push_skipped("create_data.tables not available", evidence=RuleEvidence(type="create_data"))
                        else:
                            ok, msg = self._apply_list_matcher(values, rule.matcher)
                            ev = RuleEvidence(type="create_data") if not ok else None
                            # Always include actual values for display
                            push(ok, None if ok else msg, actual=values, evidence=ev)
                        continue
                    if field == "columns":
                        values = cd.get("columns")
                        if not isinstance(values, list):
                            push_skipped("create_data.columns not available", evidence=RuleEvidence(type="create_data"))
                        else:
                            ok, msg = self._apply_list_matcher(values, rule.matcher)
                            ev = RuleEvidence(type="create_data") if not ok else None
                            push(ok, None if ok else msg, actual=values, evidence=ev)
                        continue
                    if field == "rows_count":
                        if not isinstance(cd.get("rows_count", None), (int, float)):
                            push_skipped("create_data.rows_count not available", evidence=RuleEvidence(type="create_data"))
                        else:
                            value = int(cd.get("rows_count") or 0)
                            ok, msg = self._apply_number_matcher(value, rule.matcher)
                            ev = RuleEvidence(type="create_data") if not ok else None
                            push(ok, None if ok else msg, actual=value, evidence=ev)
                        continue
                    if field == "code":
                        if not isinstance(cd.get("code", None), str):
                            push_skipped("create_data.code not available", evidence=RuleEvidence(type="create_data"))
                        else:
                            value = cd.get("code") or ""
                            ok, msg = self._apply_matcher(value, rule.matcher)
                            ev = RuleEvidence(type="create_data") if not ok else None
                            push(ok, None if ok else msg, actual=value, evidence=ev)
                        continue

                # tool:clarify.* (support question text checks)
                if cat == "tool:clarify":
                    cl = snapshot.get("clarify") or {}
                    value = (
                        cl.get("question_text")
                        if isinstance(cl, dict)
                        else ""
                    ) or ""
                    # Accept multiple field aliases that map to the same value
                    if field in {"question_text", "text", "question"}:
                        ok, msg = self._apply_matcher(value, rule.matcher)
                        ev = RuleEvidence(type="clarify") if not ok else None
                        push(ok, None if ok else msg, actual=value, evidence=ev)
                    else:
                        push_skipped(f"clarify.{field} not available", evidence=RuleEvidence(type="clarify"))
                    continue

                # Unsupported category/field -> pass (alignment only)
                push_skipped("Unsupported rule target (skipped)")
                continue

            # Unknown rule type -> pass (alignment only)
            push_skipped("Unknown rule type (skipped)")

        total = len(rules)
        status = "pass" if failed == 0 else "fail"
        # Coerce duration to int to satisfy schema
        try:
            duration_coerced = int(round(run_duration_ms)) if isinstance(run_duration_ms, (int, float)) else None
        except Exception:
            duration_coerced = None
        totals = TestResultTotals(total=total, passed=passed, failed=failed, skipped=skipped, duration_ms=duration_coerced)
        # Build spec snapshot from ExpectationsSpec so rule_results align with UI
        try:
            rule_dicts = []
            for rr in rules:
                if hasattr(rr, "model_dump"):
                    rule_dicts.append(rr.model_dump())
                elif isinstance(rr, dict):
                    rule_dicts.append(rr)
                else:
                    # Fallback: best-effort conversion
                    rule_dicts.append({})
            spec_snapshot = RuleSpec(
                spec_version=getattr(expectations, "spec_version", 1),
                rules=rule_dicts,
                order_mode=getattr(expectations, "order_mode", None),
            )
        except Exception:
            spec_snapshot = RuleSpec(spec_version=1, rules=[], order_mode=None)

        result_json = TestResultJsonSchema(spec=spec_snapshot, totals=totals, rule_results=rule_results)
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
                raw_pat = getattr(matcher, "pattern", None)
                if raw_pat is None:
                    raw_pat = getattr(matcher, "value", "")
                pat = str(raw_pat or "")
                flags = 0
                # Support JS-style /pattern/flags input; fall back to raw pattern
                if pat.startswith("/") and pat.count("/") >= 2:
                    last = pat.rfind("/")
                    core = pat[1:last]
                    fl = pat[last + 1 :]
                    # Interpret common flags
                    for ch in fl:
                        if ch == "i":
                            flags |= re.IGNORECASE
                        elif ch == "m":
                            flags |= re.MULTILINE
                        elif ch == "s":
                            flags |= re.DOTALL
                    pat = core
                # Treat empty or lone '*' as match-any convenience
                if pat.strip() in {"", "*"}:
                    pat = ".*"
                compiled = re.compile(pat, flags)
                return (isinstance(value, str) and compiled.search(value) is not None), f"must match /{getattr(matcher, 'pattern', '')}/"
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


