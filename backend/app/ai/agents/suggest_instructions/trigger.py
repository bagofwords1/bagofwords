import re
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion_feedback import CompletionFeedback
from app.models.tool_execution import ToolExecution
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.data_source import DataSource
from app.models.report_data_source_association import report_data_source_association


# Maturity gating (DataSource.reliability_status: "training" | "development"
# | "ok"). AMBIENT conditions are self-generated signals that fire on routine
# healthy sessions — they are what makes a training-stage agent learn fast,
# and what makes a production agent's instruction set drift. Once EVERY agent
# attached to the session is "ok", ambient conditions stop firing. Condition
# C (user correction) is LEVELED separately: aggressive on development,
# prior-turn-gated on training, off on ok (see evaluate()). Failure-fixed
# signals (failed-then-fixed, MCP contract discoveries) and user-initiated
# flows (feedback, eval capture) still wake the harness at every maturity.
AMBIENT_CONDITIONS = {
    "clarify_then_create_data",   # A
    "retry_recovery",             # B
    "user_provided_code",         # E
    # inspect_then_create_data (F) is disabled outright — see evaluate().
}


# Keywords that suggest user is correcting/clarifying. This is a plain
# substring scan, so every entry must earn its precision:
# - Removed as noise ("no purchases", "error rate", "should be sorted",
#   "by country rather than city" are ordinary analytics vocabulary, not
#   corrections): bare "no ", "error", "should be", "rather", "not that".
#   Keep "no," — the comma variant IS corrective ("no, I meant net").
# - The exclude/remove/without family stays because from turn 2 onward it's
#   exactly how users correct ("actually, exclude cancelled") — first-turn
#   false positives are handled by the prior-turn gate in condition C, not
#   by deleting the vocabulary.
CORRECTION_KEYWORDS = [
    # Explicit negations
    "wrong", "incorrect", "mistake",
    # Corrections
    "no,", "nope", "actually", "i meant",
    "shouldn't", "shouldnt", "should not",
    "don't", "dont", "do not",
    "instead", "fix",
    # Negations
    "that's not", "thats not", "that is not",
    "isn't right", "isnt right", "is not right",
    "not correct", "not right",
    # Commands to exclude/remove
    "exclude", "remove", "without", "skip", "omit", "drop",
]

# Error text that means "try again later", not "you called this wrong".
#
# An MCP call can fail for two very different reasons, and only one of them is
# worth writing down. A rate limit, an expired token or a gateway blip says
# nothing durable about how the tool should be called — turning one into a
# permanent instruction would be actively harmful, since the rule outlives the
# outage. Anything matching here is excluded from the failed-then-fixed trigger.
TRANSIENT_ERROR_PATTERNS = [
    r"\b429\b", r"\brate.?limit", r"\btoo many requests\b",
    r"\b50[0-9]\b", r"\bserver error\b", r"\bbad gateway\b",
    r"\bservice unavailable\b", r"\bgateway timeout\b",
    r"\btimed?.?out\b", r"\btimeout\b",
    r"\b401\b", r"\b403\b", r"\bunauthorized\b", r"\bforbidden\b",
    r"\btoken (has )?expired\b", r"\bexpired token\b",
    r"\binvalid.{0,10}credential", r"\bauthentication failed\b",
    r"\bconnection (refused|reset|error)\b", r"\bnetwork\b",
    r"\btemporarily unavailable\b", r"\btry again\b",
]

# Patterns that suggest user provided code
CODE_PATTERNS = [
    r"```",                          # Markdown code blocks
    r"\bSELECT\s+.+\s+FROM\b",       # SQL SELECT
    r"\bWHERE\s+\w+\s*[=<>]",        # SQL WHERE
    r"\bJOIN\s+\w+",                 # SQL JOIN
    r"\bGROUP\s+BY\b",               # SQL GROUP BY
    r"\bORDER\s+BY\b",               # SQL ORDER BY
    r"\bdef\s+\w+\s*\(",             # Python function
    r"\bimport\s+\w+",               # Python import
    r"\bpd\.\w+",                    # Pandas
    r"\bdf\[",                       # DataFrame indexing
]


def _json_preview(value: object, limit: int = 220) -> str:
    """Compact JSON for embedding tool arguments in a trigger hint."""
    import json

    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


class TriggerCondition:
    """Represents a single trigger condition result."""
    
    def __init__(self, name: str, hint: str, met: bool = False):
        self.name = name
        self.hint = hint
        self.met = met
    
    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "hint": self.hint}

    @staticmethod
    def format_for_prompt(conditions: List[Dict[str, str]]) -> str:
        """Format a list of trigger conditions as an XML block for the knowledge harness prompt.

        Each condition becomes a numbered line: [name] hint
        Returns an empty <trigger_conditions /> tag if conditions is empty.
        """
        if not conditions:
            return "<trigger_conditions />"
        lines = []
        for idx, c in enumerate(conditions, start=1):
            name = c.get("name", "unknown")
            hint = c.get("hint", "")
            lines.append(f"{idx}. [{name}] {hint}")
        body = "\n".join(lines)
        return f"<trigger_conditions>\n{body}\n</trigger_conditions>"

    @staticmethod
    def create_feedback_condition(feedback_direction: int, feedback_message: Optional[str] = None) -> Dict[str, str]:
        """Create a trigger condition for feedback-based suggestion generation.
        
        Args:
            feedback_direction: 1 for positive, -1 for negative feedback
            feedback_message: Optional message from the user explaining their feedback
            
        Returns:
            A condition dict with 'name' and 'hint' keys
        """
        if feedback_direction == -1:
            hint = (
                "Negative feedback flow: The user downvoted this response, indicating something went wrong. "
                f"User feedback message: {feedback_message or 'No message provided'}. "
                "Identify what failed or was incorrect. Propose instructions that would help "
                "avoid similar issues in the future (e.g., data interpretation, formatting, filtering, "
                "calculations, or terminology)."
            )
        else:
            hint = (
                "Positive feedback flow: The user upvoted this response, indicating it was helpful. "
                f"User feedback message: {feedback_message or 'No message provided'}. "
                "Identify patterns, definitions, or approaches that worked well and could be "
                "codified as reusable instructions for future similar queries."
            )
        return {"name": "feedback_triggered", "hint": hint}


class InstructionTriggerEvaluator:
    """Evaluates whether to trigger instruction suggestions based on conversation history.

    Conditions:
    - A) clarify_then_create_data: Previous tool was 'clarify', current has create_data
    - B) retry_recovery: create_data succeeded after internal retries/errors
    - C) user_explicit_correction: User message has correction language, then create_data succeeded
         (leveled by maturity: development=any turn, training=needs a prior turn, ok=off)
    - D) failed_then_fixed: Previous create_data failed, user message, current create_data succeeded (same tables)
    - E) user_provided_code: User provided code after a create_data
    - F) inspect_then_create_data: DISABLED (see evaluate()) — fired on nearly every healthy run
    - G) training_mode_complete: Training mode completed with suggested instructions in final_answer
    - H) positive_feedback_create_data: User upvoted a completion that successfully ran create_data
        (drives the eval-as-tools path — harness uses search_evals + create_eval)

    Returns a structured result with decision and list of met conditions.
    """

    def __init__(
        self,
        db: AsyncSession,
        organization_settings,
        report_id: Optional[str],
        current_execution_id: Optional[str],
        user_message: Optional[str] = None,
        mode: Optional[str] = None,
        completion_id: Optional[str] = None,
    ):
        self.db = db
        self.organization_settings = organization_settings
        self.report_id = report_id
        self.current_execution_id = current_execution_id
        self.user_message = user_message or ""
        self.mode = mode
        # Optional — when set, the eval condition (H) checks for positive
        # feedback on this specific completion. Pass the SYSTEM completion
        # id (the AI response the user thumbs-up's), not the head.
        self.completion_id = completion_id

    async def evaluate(
        self, prev_tool_name_before_last_user: Optional[str] = None
    ) -> Dict[str, object]:
        """Evaluate all trigger conditions and return structured result.

        Returns:
            {
                "decision": bool,
                "conditions": [{"name": str, "hint": str}, ...]
            }
        """
        # Two independent gates: ``suggest_instructions`` covers conditions
        # A-G; ``auto_suggest_evals`` covers H. If both are off there's
        # nothing to do.
        si_config = self.organization_settings.get_config("suggest_instructions")
        si_on = (si_config is None) or (si_config.value is not False)
        ase_config = self.organization_settings.get_config("auto_suggest_evals")
        ase_on = (ase_config is None) or (ase_config.value is not False)

        if not si_on and not ase_on:
            return {"decision": False, "conditions": []}

        if not self.report_id:
            return {"decision": False, "conditions": []}

        # Training mode always triggers - instructions are in final_answer
        if self.mode == "training":
            return {
                "decision": True,
                "conditions": [{
                    "name": "training_mode_complete",
                    "hint": (
                        "Training mode flow: The agent completed a systematic exploration of the "
                        "data domain and produced suggested instructions in its final_answer. "
                        "Extract the instructions from the 'Suggested Instructions for Future Analysis' "
                        "section of the final_answer. These are already well-formed reusable instructions."
                    )
                }]
            }

        met_conditions: List[Dict[str, str]] = []

        try:
            # Fetch user message if not provided
            if not self.user_message:
                self.user_message = await self._get_user_message()

            # Agent maturity for this session — gates the ambient conditions.
            session_maturity = await self._resolve_session_maturity()
            include_ambient = session_maturity != "ok"

            # Instruction conditions — gated by ``suggest_instructions``.
            if si_on:
                conditions_checked: List[TriggerCondition] = []
                if include_ambient:
                    conditions_checked.append(
                        await self._check_clarify_then_create_data(prev_tool_name_before_last_user)
                    )
                    conditions_checked.append(await self._check_retry_recovery())
                    conditions_checked.append(
                        await self._check_user_provided_code(prev_tool_name_before_last_user)
                    )
                # Condition C is LEVELED by maturity rather than always-on:
                #   development → aggressive: correction keywords fire on any
                #                 turn (the builder is actively teaching);
                #   training    → standard: keywords fire only when a prior
                #                 turn exists — a first message cannot be a
                #                 correction, there is nothing to correct
                #                 (observed false positive: "customers with
                #                 no purchases" waking the harness on turn 1);
                #   ok (prod)   → off entirely.
                if session_maturity != "ok":
                    conditions_checked.append(
                        await self._check_user_explicit_correction(
                            require_prior_turn=(session_maturity != "development"),
                        )
                    )
                # Human-taught signals below run at every maturity.
                conditions_checked.append(await self._check_failed_then_fixed())
                # Condition F (inspect_then_create_data) is DISABLED for now:
                # it fired on nearly every healthy run (inspect before create is
                # the normal flow) and was the dominant source of speculative
                # captures. Re-enable behind the maturity gate if it earns its
                # keep — the check itself is kept below, unreferenced.
                # conditions_checked.append(await self._check_inspect_then_create_data())
                conditions_checked.append(await self._check_mcp_failed_then_fixed())

                for condition in conditions_checked:
                    if condition.met:
                        met_conditions.append(condition.to_dict())

            # Eval condition (H) — gated by ``auto_suggest_evals``.
            if ase_on:
                condition_h = await self._check_positive_feedback_with_create_data()
                if condition_h.met:
                    met_conditions.append(condition_h.to_dict())

            decision = len(met_conditions) > 0
            return {
                "decision": decision,
                "conditions": met_conditions,
                "session_maturity": session_maturity,
            }

        except Exception:
            return {"decision": False, "conditions": []}

    async def _resolve_session_maturity(self) -> str:
        """Least-mature reliability status among the report's agents.

        Returns "ok" only when EVERY attached data source is production-grade
        — one agent still in training keeps full trigger sensitivity, because
        its knowledge base is precisely what the harness exists to build.
        Reports with no attached data sources (file-only sessions) keep full
        sensitivity too.
        """
        try:
            if not self.report_id:
                return "training"
            rows = (
                await self.db.execute(
                    select(DataSource.reliability_status)
                    .join(
                        report_data_source_association,
                        report_data_source_association.c.data_source_id == DataSource.id,
                    )
                    .where(report_data_source_association.c.report_id == self.report_id)
                    .where(DataSource.deleted_at.is_(None))
                )
            ).scalars().all()
            statuses = [(s or "training") for s in rows]
            if not statuses:
                return "training"
            order = {"training": 0, "development": 1, "ok": 2}
            return min(statuses, key=lambda s: order.get(s, 0))
        except Exception:
            return "training"

    async def _get_user_message(self) -> str:
        """Fetch the user message that triggered the current execution."""
        try:
            if not self.current_execution_id:
                return ""
            
            # Get the agent execution to find the completion
            exec_result = await self.db.execute(
                select(AgentExecution.completion_id)
                .where(AgentExecution.id == self.current_execution_id)
            )
            row = exec_result.first()
            if not row:
                return ""
            
            completion_id = row[0]
            
            # Get the completion to find its parent (user message)
            comp_result = await self.db.execute(
                select(Completion.parent_id)
                .where(Completion.id == completion_id)
            )
            comp_row = comp_result.first()
            if not comp_row or not comp_row[0]:
                return ""
            
            parent_id = comp_row[0]
            
            # Get the parent completion (user message)
            parent_result = await self.db.execute(
                select(Completion.prompt)
                .where(Completion.id == parent_id)
            )
            parent_row = parent_result.first()
            if not parent_row:
                return ""
            
            prompt = parent_row[0]
            if isinstance(prompt, dict):
                return prompt.get("content", "")
            return str(prompt) if prompt else ""
            
        except Exception:
            return ""

    async def _check_clarify_then_create_data(
        self, prev_tool_name: Optional[str]
    ) -> TriggerCondition:
        """Condition A: Previous tool was 'clarify' and current execution has create_data.
        
        Signal: User provided a concrete definition after a clarification question.
        """
        condition = TriggerCondition(
            name="clarify_then_create_data",
            hint=(
                "Clarification flow: User provided a definition after a clarify question, "
                "then triggered a create_data tool. Extract the user's definition and convert "
                "it into a reusable instruction."
            ),
        )
        
        try:
            if not self.current_execution_id:
                return condition

            # Check if current execution has create_data
            stmt = (
                select(ToolExecution.id)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .limit(1)
            )
            result = await self.db.execute(stmt)
            ran_create_data = result.first() is not None

            condition.met = bool(ran_create_data and prev_tool_name == "clarify")
            return condition

        except Exception:
            return condition

    async def _check_retry_recovery(self) -> TriggerCondition:
        """Condition B: Current execution has successful create_data with internal retries.
        
        Signal: Code generation succeeded after 1+ internal errors/retries.
        """
        condition = TriggerCondition(
            name="retry_recovery",
            hint=(
                "Code recovery flow: A create_data action succeeded after internal "
                "retries/errors. Propose instructions that would help avoid similar "
                "failures in the future (e.g., validation, column naming, joins, filters, "
                "casting, limits)."
            ),
        )
        
        try:
            if not self.current_execution_id:
                return condition

            stmt = (
                select(ToolExecution.result_json)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .where(
                    (ToolExecution.success == True) | (ToolExecution.status == "success")
                )
                .order_by(ToolExecution.started_at.desc())
                .limit(10)
            )
            result = await self.db.execute(stmt)
            
            for (result_json,) in result.all():
                try:
                    errors = (result_json or {}).get("errors", [])
                    if isinstance(errors, list) and len(errors) >= 1:
                        condition.met = True
                        return condition
                except Exception:
                    continue

            return condition

        except Exception:
            return condition

    async def _has_prior_turn(self) -> bool:
        """True when this report already has an earlier agent execution —
        i.e. there is a previous answer a correction could refer to."""
        try:
            if not self.report_id or not self.current_execution_id:
                return False
            row = (
                await self.db.execute(
                    select(AgentExecution.id)
                    .where(AgentExecution.report_id == self.report_id)
                    .where(AgentExecution.id != self.current_execution_id)
                    .limit(1)
                )
            ).first()
            return row is not None
        except Exception:
            return False

    async def _check_user_explicit_correction(self, require_prior_turn: bool = True) -> TriggerCondition:
        """Condition C: User message contains correction language and create_data succeeded.

        Signal: User explicitly corrected something ("wrong", "actually", "I meant").
        ``require_prior_turn`` (standard mode) additionally demands an earlier
        agent execution in the report — correction vocabulary overlaps with
        ordinary spec vocabulary ("exclude refunds", "without cancelled"), and
        position is what disambiguates: a first turn has nothing to correct.
        """
        condition = TriggerCondition(
            name="user_explicit_correction",
            hint=(
                "User correction flow: The user's message contained correction language "
                "(e.g., 'wrong', 'actually', 'I meant'), suggesting they are teaching "
                "the system what they really meant. Extract the corrected definition or rule."
            ),
        )
        
        try:
            if not self.current_execution_id or not self.user_message:
                return condition

            # Check if user message contains correction keywords
            user_msg_lower = self.user_message.lower()
            has_correction = any(kw in user_msg_lower for kw in CORRECTION_KEYWORDS)

            if not has_correction:
                return condition

            # Standard mode: a correction needs something to correct.
            if require_prior_turn and not await self._has_prior_turn():
                return condition

            # Check if current execution has successful create_data
            stmt = (
                select(ToolExecution.id)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .where(
                    (ToolExecution.success == True) | (ToolExecution.status == "success")
                )
                .limit(1)
            )
            result = await self.db.execute(stmt)
            has_successful_create_data = result.first() is not None

            condition.met = has_successful_create_data
            return condition

        except Exception:
            return condition

    async def _check_failed_then_fixed(self) -> TriggerCondition:
        """Condition D: Previous create_data failed, user message, current create_data succeeded.
        
        Signal: User feedback fixed a failed attempt. Optionally checks for same/similar tables.
        """
        condition = TriggerCondition(
            name="failed_then_fixed",
            hint=(
                "Failed-then-fixed flow: A previous create_data failed, the user provided "
                "feedback, and the next create_data succeeded. The user's feedback likely "
                "contains the fix or clarification needed. Extract the learning."
            ),
        )
        
        try:
            if not self.current_execution_id or not self.report_id:
                return condition

            # Check if current execution has successful create_data
            stmt_current = (
                select(ToolExecution.arguments_json)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .where(
                    (ToolExecution.success == True) | (ToolExecution.status == "success")
                )
                .limit(1)
            )
            result_current = await self.db.execute(stmt_current)
            current_row = result_current.first()
            
            if not current_row:
                return condition
            
            current_tables = self._extract_tables_from_input(current_row[0])

            # Check for a PREVIOUS failed create_data in this report (different execution)
            stmt_prev_failed = (
                select(ToolExecution.arguments_json)
                .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                .where(AgentExecution.report_id == self.report_id)
                .where(AgentExecution.id != self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .where(
                    (ToolExecution.success == False) | (ToolExecution.status == "error")
                )
                .order_by(ToolExecution.started_at.desc())
                .limit(5)
            )
            result_prev = await self.db.execute(stmt_prev_failed)
            
            for (prev_input,) in result_prev.all():
                prev_tables = self._extract_tables_from_input(prev_input)
                # Check if there's any overlap in tables (same data being queried)
                if prev_tables and current_tables:
                    overlap = prev_tables & current_tables
                    if overlap:
                        condition.met = True
                        condition.hint = (
                            f"Failed-then-fixed flow: A previous create_data failed on tables "
                            f"{list(overlap)}, the user provided feedback, and the current "
                            f"create_data succeeded. Extract what the user taught to fix the issue."
                        )
                        return condition
                elif prev_tables or current_tables:
                    # If we can't compare tables, still trigger if there was a recent failure
                    condition.met = True
                    return condition

            return condition

        except Exception:
            return condition

    @staticmethod
    def _is_transient_error(error_message: Optional[str]) -> bool:
        """True when an error says 'retry later' rather than 'you called it wrong'."""
        if not error_message:
            return True  # No detail to learn from — treat as not worth capturing.
        text = error_message.lower()
        return any(re.search(p, text) for p in TRANSIENT_ERROR_PATTERNS)

    async def _check_mcp_failed_then_fixed(self) -> TriggerCondition:
        """Condition I: an execute_mcp call failed against a connection, and a
        later call to that same connection succeeded.

        This is the MCP twin of ``_check_failed_then_fixed``, with two
        deliberate differences.

        First, it matches WITHIN one execution as well as across executions. The
        SQL version requires a user turn between the failure and the fix, because
        a human normally supplies the missing knowledge. MCP failures are not
        like that: the server states its own constraint in the error, and the
        agent recovers unaided in the same run. Requiring a user turn would miss
        the common case entirely.

        Second, it filters transient errors. A rate limit or expired token is not
        a fact about how the tool should be called, and recording it as an
        instruction would leave a stale rule behind once the outage clears.

        The learning is the delta between the rejected arguments and the accepted
        ones, explained by the server's own error text — a durable fact about a
        server whose declared schema under-specifies its real contract.
        """
        condition = TriggerCondition(
            name="mcp_failed_then_fixed",
            hint=(
                "An MCP/API tool call failed and a later call to the same connection "
                "succeeded. Capture what the server actually requires."
            ),
        )

        try:
            if not self.current_execution_id or not self.report_id:
                return condition

            # All execute_mcp calls for this report, oldest first, so a failure
            # can be paired with a later success.
            stmt = (
                select(
                    ToolExecution.arguments_json,
                    ToolExecution.success,
                    ToolExecution.status,
                    ToolExecution.error_message,
                    ToolExecution.started_at,
                )
                .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                .where(AgentExecution.report_id == self.report_id)
                .where(ToolExecution.tool_name == "execute_mcp")
                .order_by(ToolExecution.started_at.asc())
                .limit(40)
            )
            rows = (await self.db.execute(stmt)).all()
            if len(rows) < 2:
                return condition

            def _conn_of(args: Optional[dict]) -> Optional[str]:
                return (args or {}).get("connection_id") if isinstance(args, dict) else None

            def _tool_of(args: Optional[dict]) -> Optional[str]:
                return (args or {}).get("tool_name") if isinstance(args, dict) else None

            for i, (args, success, status, error_message, _ts) in enumerate(rows):
                failed = (success is False) or (status == "error")
                if not failed:
                    continue
                if self._is_transient_error(error_message):
                    continue
                conn = _conn_of(args)
                if not conn:
                    continue

                # Any LATER successful call on the same connection is the fix —
                # whether the agent retried the same tool with corrected
                # arguments or switched to a sibling tool that does the job.
                for later_args, later_success, later_status, _e, _t in rows[i + 1:]:
                    if not ((later_success is True) or (later_status == "success")):
                        continue
                    if _conn_of(later_args) != conn:
                        continue

                    failed_tool = _tool_of(args) or "unknown"
                    fixed_tool = _tool_of(later_args) or "unknown"
                    same_tool = failed_tool == fixed_tool
                    err = (error_message or "").strip()
                    if len(err) > 300:
                        err = err[:300] + "…"

                    condition.met = True
                    condition.hint = (
                        "MCP failed-then-fixed flow: the tool "
                        f"'{failed_tool}' was called and the server REJECTED it with: "
                        f"\"{err}\". A later call "
                        + (
                            f"to the same tool with different arguments succeeded."
                            if same_tool
                            else f"to '{fixed_tool}' succeeded instead."
                        )
                        + " The rejected arguments were: "
                        f"{_json_preview(args)}. The accepted arguments were: "
                        f"{_json_preview(later_args)}.\n"
                        "Capture the GENERAL RULE this reveals about the server — a requirement "
                        "its declared input schema does not express (e.g. 'this tool needs at "
                        "least one filter argument', 'this field must be a JSON-encoded string', "
                        "'use tool X rather than Y to list all records'). "
                        "Write it as a reusable rule about the tool, naming the tool and the "
                        "requirement. Do NOT record the specific ids, emails or values used in "
                        "this run — those are record-level facts, not rules. "
                        "FIRST call search_instructions to check whether this rule is already "
                        "captured; if it is, do nothing rather than creating a near-duplicate."
                    )
                    return condition

            return condition

        except Exception:
            return condition

    def _extract_tables_from_input(self, tool_input: Optional[dict]) -> set:
        """Extract table names from tool_input.tables_by_source or similar fields."""
        tables = set()
        try:
            if not tool_input or not isinstance(tool_input, dict):
                return tables
            
            # Check tables_by_source - can be List[Dict] or Dict
            tables_by_source = tool_input.get("tables_by_source")
            
            if isinstance(tables_by_source, list):
                # List format: [{data_source_id, tables: [...]}, ...]
                for source_entry in tables_by_source:
                    if isinstance(source_entry, dict):
                        table_list = source_entry.get("tables", [])
                        if isinstance(table_list, list):
                            for t in table_list:
                                if isinstance(t, str):
                                    tables.add(t.lower())
                                elif isinstance(t, dict) and "name" in t:
                                    tables.add(t["name"].lower())
            elif isinstance(tables_by_source, dict):
                # Dict format: {source_id: [...], ...} (legacy)
                for source, table_list in tables_by_source.items():
                    if isinstance(table_list, list):
                        for t in table_list:
                            if isinstance(t, str):
                                tables.add(t.lower())
                            elif isinstance(t, dict) and "name" in t:
                                tables.add(t["name"].lower())
            
            # Check tables field directly
            direct_tables = tool_input.get("tables", [])
            if isinstance(direct_tables, list):
                for t in direct_tables:
                    if isinstance(t, str):
                        tables.add(t.lower())
                        
        except Exception:
            pass
        return tables

    async def _check_user_provided_code(
        self, prev_tool_name: Optional[str]
    ) -> TriggerCondition:
        """Condition E: User provided code after a create_data (success or fail).
        
        Signal: User is showing how to do something correctly with code.
        """
        condition = TriggerCondition(
            name="user_provided_code",
            hint=(
                "User provided code: The user included SQL or Python code in their message "
                "after a create_data attempt. They may be showing the correct approach. "
                "Summarize the key pattern or rule from their code as an instruction."
            ),
        )
        
        try:
            if not self.user_message:
                return condition

            # Check if user message contains code patterns
            has_code = any(
                re.search(pattern, self.user_message, re.IGNORECASE)
                for pattern in CODE_PATTERNS 
            )
            
            if not has_code:
                return condition

            # Check if previous tool was create_data (success or fail)
            if prev_tool_name == "create_data":
                condition.met = True
                # Enhance hint with detected code type
                code_summary = self._summarize_code_intent(self.user_message)
                if code_summary:
                    condition.hint = (
                        f"User provided code: The user included code in their message after "
                        f"a create_data attempt. Detected pattern: {code_summary}. "
                        f"Extract the key rule or approach they are demonstrating."
                    )
                return condition

            # Also check if current execution had create_data before user's next message
            # This handles: create_data -> user provides code in same turn
            if self.current_execution_id:
                stmt = (
                    select(ToolExecution.id)
                    .where(ToolExecution.agent_execution_id == self.current_execution_id)
                    .where(ToolExecution.tool_name == "create_data")
                    .limit(1)
                )
                result = await self.db.execute(stmt)
                if result.first() is not None:
                    condition.met = True
                    code_summary = self._summarize_code_intent(self.user_message)
                    if code_summary:
                        condition.hint = (
                            f"User provided code: The user included code in their message. "
                            f"Detected pattern: {code_summary}. "
                            f"Extract the key rule or approach they are demonstrating."
                        )

            return condition

        except Exception:
            return condition

    def _summarize_code_intent(self, message: str) -> str:
        """Summarize what kind of code the user provided (not the code itself)."""
        summaries = []
        
        msg_upper = message.upper()
        
        if "SELECT" in msg_upper and "FROM" in msg_upper:
            summaries.append("SQL query")
            if "JOIN" in msg_upper:
                summaries.append("with JOIN")
            if "WHERE" in msg_upper:
                summaries.append("with filtering")
            if "GROUP BY" in msg_upper:
                summaries.append("with aggregation")
            if "ORDER BY" in msg_upper:
                summaries.append("with sorting")
        
        if re.search(r"\bdef\s+\w+", message):
            summaries.append("Python function definition")
        
        if "pd." in message or "df[" in message or "DataFrame" in message:
            summaries.append("Pandas data manipulation")
        
        if "```" in message:
            summaries.append("code block")
        
        return " ".join(summaries) if summaries else ""

    async def _check_inspect_then_create_data(self) -> TriggerCondition:
        """Condition F: successful inspect_data in the same execution, then create_data succeeded.

        CURRENTLY DISABLED — not called from evaluate(). Inspect-before-create
        is the normal flow of a healthy run, so this fired almost every
        session and drove speculative captures. Kept for possible re-enable
        behind the maturity gate.

        Signal: Agent examined data structure before successfully creating data.
        Requires successful inspect_data and at least some table overlap with create_data.
        """
        condition = TriggerCondition(
            name="inspect_then_create_data",
            hint=(
                "Inspection-guided flow: An inspect_data tool was used to examine "
                "data structure before create_data succeeded. The inspection likely "
                "revealed important details about column names, data types, formats, "
                "relationships, or sample values not apparent from schema alone. "
                "Extract the key insight(s) as a reusable instruction."
            ),
        )
        
        try:
            if not self.current_execution_id:
                return condition

            # Get all successful inspect_data executions in the current execution
            stmt_inspect = (
                select(ToolExecution.arguments_json)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "inspect_data")
                .where(
                    (ToolExecution.success == True) | (ToolExecution.status == "success")
                )
            )
            result_inspect = await self.db.execute(stmt_inspect)
            inspect_rows = result_inspect.all()
            
            if not inspect_rows:
                return condition

            # Get successful create_data executions in the current execution
            stmt_create = (
                select(ToolExecution.arguments_json)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .where(
                    (ToolExecution.success == True) | (ToolExecution.status == "success")
                )
            )
            result_create = await self.db.execute(stmt_create)
            create_rows = result_create.all()
            
            if not create_rows:
                return condition

            # Check for table overlap between any inspect_data and any successful create_data
            for (inspect_input,) in inspect_rows:
                inspect_tables = self._extract_tables_from_input(inspect_input)
                if not inspect_tables:
                    continue
                
                for (create_input,) in create_rows:
                    create_tables = self._extract_tables_from_input(create_input)
                    if not create_tables:
                        continue
                    
                    # Check for overlap
                    overlap = inspect_tables & create_tables
                    if overlap:
                        condition.met = True
                        condition.hint = (
                            f"Inspection-guided flow: inspect_data examined tables "
                            f"{list(overlap)} before create_data succeeded on the same tables. "
                            f"The inspection revealed details about column names, data types, "
                            f"formats, relationships, or join keys. Extract the key insight "
                            f"learned from the inspection as a reusable instruction."
                        )
                        return condition

            return condition

        except Exception:
            return condition

    async def _check_positive_feedback_with_create_data(self) -> TriggerCondition:
        """Condition H: a user upvoted this completion AND the execution
        had at least one successful ``create_data``.

        Drives the eval-as-tools path: when this fires, the knowledge
        harness sees ``search_evals`` and ``create_eval`` in its catalog
        and the prompt nudges it to dedupe-then-draft.
        """
        condition = TriggerCondition(
            name="positive_feedback_create_data",
            hint=(
                "Eval-capture flow: A user upvoted a completion that "
                "successfully ran create_data. Use search_evals to check "
                "whether a similar test case already exists; if not, use "
                "create_eval to draft a new case (it will land as a draft "
                "in the org's drafts suite for review). Build the case "
                "around tool.calls (set membership) and a judge rule with "
                "a rubric extracted from the conversation. Do NOT assert "
                "on raw SQL/data."
            ),
        )

        try:
            if not self.current_execution_id:
                return condition

            # Need a successful create_data in this execution.
            te_stmt = (
                select(ToolExecution.id)
                .where(ToolExecution.agent_execution_id == self.current_execution_id)
                .where(ToolExecution.tool_name == "create_data")
                .where(
                    (ToolExecution.success == True) | (ToolExecution.status == "success")
                )
                .limit(1)
            )
            if (await self.db.execute(te_stmt)).first() is None:
                return condition

            # Resolve the system completion id we should look for feedback on.
            target_completion_id = self.completion_id
            if not target_completion_id:
                ae_stmt = (
                    select(AgentExecution.completion_id)
                    .where(AgentExecution.id == self.current_execution_id)
                )
                ae_row = (await self.db.execute(ae_stmt)).first()
                if ae_row:
                    target_completion_id = ae_row[0]
            if not target_completion_id:
                return condition

            fb_stmt = (
                select(CompletionFeedback.id)
                .where(CompletionFeedback.completion_id == str(target_completion_id))
                .where(CompletionFeedback.direction == 1)
                .limit(1)
            )
            if (await self.db.execute(fb_stmt)).first() is None:
                return condition

            condition.met = True
            return condition

        except Exception:
            return condition
