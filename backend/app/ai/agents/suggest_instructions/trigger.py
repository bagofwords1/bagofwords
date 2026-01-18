import re
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_execution import ToolExecution
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion


# Keywords that suggest user is correcting/clarifying
CORRECTION_KEYWORDS = [
    # Explicit negations
    "wrong", "incorrect", "mistake", "error",
    # Corrections
    "no,", "no ", "nope", "actually", "i meant", "not that",
    "should be", "shouldn't", "shouldnt", "should not",
    "don't", "dont", "do not",
    "instead", "rather", "fix",
    # Negations
    "that's not", "thats not", "that is not",
    "isn't right", "isnt right", "is not right",
    "not correct", "not right",
    # Commands to exclude/remove
    "exclude", "remove", "without", "skip", "omit", "drop",
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


class TriggerCondition:
    """Represents a single trigger condition result."""
    
    def __init__(self, name: str, hint: str, met: bool = False):
        self.name = name
        self.hint = hint
        self.met = met
    
    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "hint": self.hint}

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
    - D) failed_then_fixed: Previous create_data failed, user message, current create_data succeeded (same tables)
    - E) user_provided_code: User provided code after a create_data
    - F) inspect_then_create_data: successful inspect_data in same execution, then create_data succeeded (with table overlap)
    - G) training_mode_complete: Training mode completed with suggested instructions in final_answer

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
    ):
        self.db = db
        self.organization_settings = organization_settings
        self.report_id = report_id
        self.current_execution_id = current_execution_id
        self.user_message = user_message or ""
        self.mode = mode

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
        # Check if suggest_instructions is enabled (default to True if not configured)
        config = self.organization_settings.get_config("suggest_instructions")
        if config and config.value is False:
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

            # Evaluate all conditions
            condition_a = await self._check_clarify_then_create_data(
                prev_tool_name_before_last_user
            )
            condition_b = await self._check_retry_recovery()
            condition_c = await self._check_user_explicit_correction()
            condition_d = await self._check_failed_then_fixed()
            condition_e = await self._check_user_provided_code(prev_tool_name_before_last_user)
            condition_f = await self._check_inspect_then_create_data()

            # Collect met conditions
            for condition in [condition_a, condition_b, condition_c, condition_d, condition_e, condition_f]:
                if condition.met:
                    met_conditions.append(condition.to_dict())

            decision = len(met_conditions) > 0
            return {"decision": decision, "conditions": met_conditions}

        except Exception:
            return {"decision": False, "conditions": []}

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

    async def _check_user_explicit_correction(self) -> TriggerCondition:
        """Condition C: User message contains correction language and create_data succeeded.
        
        Signal: User explicitly corrected something ("no", "wrong", "actually", "I meant").
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
