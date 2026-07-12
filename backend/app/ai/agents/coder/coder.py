import asyncio
from typing import Callable, Optional

from partialjson.json_parser import JSONParser
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLM
from app.ai.llm.types import Message, TextDeltaEvent
from app.models.llm_model import LLMModel
import re
import json
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from app.ai.agents.planner.clock import current_time_str
from app.ai.schemas.codegen import CodeGenContext
from app.services.usage_policy_service import UsageLimitContext
from app.core.otel import get_tracer
from app.ai.code_execution.code_execution import FORBIDDEN_BUILTINS, FORBIDDEN_MODULES

tracer = get_tracer(__name__)


def _sandbox_rules_section() -> str:
    """Sandbox constraints for codegen prompts, derived from the validator's
    own lists so the prompt can never drift from what execute_code enforces."""
    builtins_list = ", ".join(sorted(FORBIDDEN_BUILTINS))
    modules_list = ", ".join(sorted(FORBIDDEN_MODULES))
    return f"""**Sandbox rules — the code is AST-validated BEFORE it runs; ANY of these constructs rejects the whole attempt:**
        - Forbidden function calls (never call these, in any context): {builtins_list}.
          * Use plain dot access instead of getattr/hasattr — fields on provided objects (e.g. `excel_files[N].path`, FetchedPage fields) always exist; check truthiness, not presence.
          * For messy or mixed-type Excel column labels, normalize with `str(col).strip()` — never `getattr(col, 'strip', ...)`.
        - Forbidden imports: {modules_list}.
        - Never access dunder attributes (e.g. `obj.__class__`, `obj.__dict__`).
        - SQL strings must be read-only — no INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / TRUNCATE / GRANT."""

class Coder:
    def __init__(
        self,
        model: LLMModel,
        organization_settings: OrganizationSettingsConfig,
        instruction_context_builder=None,
        context_hub=None,
        usage_session_maker: Optional[Callable[[], AsyncSession]] = None,
        usage_context: Optional[UsageLimitContext] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker, usage_context=usage_context)
        self.organization_settings = organization_settings
        self.enable_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
        # Back-compat: accept either legacy builder or new context hub
        self.instruction_context_builder = instruction_context_builder
        self.context_hub = context_hub

    def _time_context(self) -> str:
        """Current-time line for codegen prompts, same clock the planner sees.

        Rendered in the org's timezone/week-start/locale settings so relative
        date phrases ("today", "last week") resolve consistently between the
        planner and the generated code; server-local time when unset.
        """
        def _get(key):
            try:
                value = self.organization_settings.get_config(key)
            except Exception:
                value = getattr(self.organization_settings, key, None)
            return value if isinstance(value, str) else None
        return current_time_str(_get("timezone"), _get("week_start"), _get("locale"))

    def _lazy_query_instruction(self) -> str:
        """Codegen prompt line for the opt-in lazy query path, or "" when the
        org hasn't enabled it. Resolved through the same helper the execution
        wrapper uses, so the prompt never advertises a method the sandbox
        would then refuse (and a missing settings object means disabled, not
        a crash)."""
        from app.ai.code_execution.code_execution import resolve_lazy_enabled

        if not resolve_lazy_enabled(self.organization_settings):
            return ""
        return (
            "- OUT-OF-CORE (LAZY) QUERIES ENABLED: for potentially large result sets "
            "(full-table scans, or queries without aggregation/LIMIT), PREFER "
            "`ds_clients[\"<client_key>\"].execute_query_lazy(\"SOME QUERY\")` over `execute_query`. "
            "It streams results to disk and returns a LazyFrame exposing the same interface plus "
            "lazy transforms (`.sql(...)`, `.limit(n)`). Push filters/aggregation into the SQL. "
            "You may return the LazyFrame directly (it is materialized under a bounded cap) or reduce "
            "then materialize with `.to_df()` (e.g. `lf.limit(n).to_df()`). For small or already-"
            "aggregated results, `execute_query` is fine."
        )

    async def execute(self, schemas, persona, prompt, memories, previous_messages):
        # Implementation left out as not requested.
        pass

    async def data_model_to_code(
        self,
        data_model,
        prompt,
        schemas,
        ds_clients,
        excel_files,
        code_and_error_messages,
        memories,
        previous_messages,
        retries,
        prev_data_model_code_pair,
        sigkill_event=None,
        code_context_builder=None
    ):
        # Optional early exit if a cancellation was requested before generation
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            return "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame()"
        # Resolve instructions from context hub when available; otherwise fallback to legacy builder
        instructions_context = ""
        mentions_context = "<mentions>No mentions for this turn</mentions>"
        entities_context = ""
        # Defaults for additional context
        resources_context = ""
        files_context = ""
        messages_context = ""
        platform = None
        past_observations = []
        last_observation = None
        history_summary = ""
        if self.context_hub is not None:
            try:
                view = self.context_hub.get_view()
                inst_obj = getattr(view.static, "instructions", None)
                instructions_context = inst_obj.render() if inst_obj else ""
                mentions_obj = getattr(view.static, "mentions", None)
                mentions_context = mentions_obj.render() if mentions_obj else mentions_context
                entities_obj = getattr(view.warm, "entities", None)
                entities_context = entities_obj.render() if entities_obj else entities_context
                # Additional context sections aligned with create_data/create_widget
                resources_obj = getattr(view.static, "resources", None)
                resources_context = resources_obj.render() if resources_obj else ""
                files_obj = getattr(view.static, "files", None)
                files_context = files_obj.render() if files_obj else ""
                messages_obj = getattr(view.warm, "messages", None)
                messages_context = messages_obj.render() if messages_obj else ""
                try:
                    platform = (getattr(view, "meta", {}) or {}).get("external_platform")
                except Exception:
                    platform = None
                # Observations and history
                past_observations = []
                last_observation = None
                try:
                    if getattr(self.context_hub, "observation_builder", None):
                        past_observations = self.context_hub.observation_builder.tool_observations or []
                        last_observation = self.context_hub.observation_builder.get_latest_observation()
                except Exception:
                    past_observations = []
                    last_observation = None
                try:
                    history_summary = self.context_hub.get_history_summary()
                except Exception:
                    history_summary = ""
            except Exception:
                instructions_context = ""
                mentions_context = mentions_context
                entities_context = entities_context
                resources_context = ""
                files_context = ""
                messages_context = ""
                platform = None
                past_observations = []
                last_observation = None
                history_summary = ""
        elif self.instruction_context_builder is not None:
            # Legacy compatibility
            if hasattr(self.instruction_context_builder, "get_instructions_context"):
                instructions_context = await self.instruction_context_builder.get_instructions_context()
            else:
                try:
                    inst_section = await self.instruction_context_builder.build()
                    instructions_context = inst_section.render()
                except Exception:
                    instructions_context = ""
            # Legacy fallbacks when ContextHub is not available
            resources_context = ""
            files_context = ""
            messages_context = "\n".join(previous_messages) if isinstance(previous_messages, list) else str(previous_messages or "")
            platform = None
            past_observations = []
            last_observation = None
            history_summary = ""

        # Build a section with existing widget data if applicable
        modify_existing_widget_text = ""
        if prev_data_model_code_pair:
            modify_existing_widget_text = f"""
            There is an existing data model and its code implementation:

            <existing_data_model>
            {prev_data_model_code_pair['data_model']}
            </existing_data_model>

            <existing_code>
            {prev_data_model_code_pair['code']}
            </existing_code>

            You can reference the existing code and data model to adapt or improve the new code for the NEW data model.
            """
        # Prepare code and error messages section if any
        code_error_section = ""
        if code_and_error_messages:
            combined = []
            for code, error in code_and_error_messages:
                combined.append(f"CODE:\n{code}\n\nERROR:\n{error}")
            code_error_section = "\n".join(combined)

        # Prepare data sources description
        # ds_clients is a dict: {domain_name:connection_name: client_object}
        # client_object has a 'description' attribute that explains how to query that client
        data_source_descriptions = []
        for client_key, client in ds_clients.items():
            data_source_descriptions.append(
                f"client_key: {client_key}\ndescription: {getattr(client, 'description', 'N/A')}"
            )
        data_source_section = "\n".join(data_source_descriptions)

        # Prepare excel files description
        excel_files_description = []
        for index, file in enumerate(excel_files):
            # Assuming file has a 'description' and 'path'
            excel_files_description.append(f"{index}: {file.description}")
        excel_files_section = "\n".join(excel_files_description)

        # Define data preview instruction based on enable_llm_see_data flag
        data_preview_instruction = f"- Also, after each query or DataFrame creation, print the data using: print('df head:', df.head())" if self.enable_llm_see_data else ""

        lazy_query_instruction = self._lazy_query_instruction()

        similar_successful_code_snippets = await code_context_builder.get_top_successful_snippets_for_data_model(data_model)
        similar_failed_code_snippets = await code_context_builder.get_top_failed_snippets_for_data_model(data_model)
        text = f"""
        Role: data engineer and data scientist working on the user's analytics request.

        Goal: Given a data model and context, generate a Python function named `generate_df(ds_clients, excel_files)`
        that produces a Pandas DataFrame according to the data model specifications only.
        Use the previous messages to understand the user's intent/context and the data model to generate the correct dataframe.

        **Organization Instructions** (authored by the user; apply them):
        {instructions_context}

        **Context and Inputs**:
        - Current Time: {self._time_context()}
          Resolve relative date expressions ("today", "last week", "this month") against this time, not the database server's clock.

        - Data Model (newly generated):
        <data_model>
        {data_model}
        </data_model>

        - User Prompt:
        <user_prompt>
        {prompt}
        </user_prompt>

        - Provided Schemas (Ground Truth):
        <ground_truth_schemas>
        {schemas}
        </ground_truth_schemas>

        - Mentions:
        {mentions_context}

        - Entities:
        {entities_context}

        - Previous Messages:
        <previous_messages>
        {previous_messages}
        </previous_messages>

        - Memories:
        <memories>
        {memories}
        </memories>

        {modify_existing_widget_text}

        - Connection Clients:
        Each connection client may be SQL, document DB, service API, or Excel.
        You have a `ds_clients` dict where each key identifies a specific database connection.
        Each ds_client has a method `execute_query("QUERY")` that returns data.
        The 'QUERY' depends on the data source type. The connection descriptions are:
        <connection_clients>
        {data_source_section}
        </connection_clients>

        - Excel Files:
        <excel_files>
        {excel_files_section}
        </excel_files>

        - Previous Code Attempts and Errors:
        <code_retries>
        {retries}
        </code_retries>

        <code_and_error_messages>
        {code_error_section}
        </code_and_error_messages>


        - Similar successful code snippets (for reference on what is working):
        <similar_successful_code_snippets>
        {similar_successful_code_snippets}
        </similar_successful_code_snippets>

        - Similar failed code snippets (for reference on what is not working):
        <similar_failed_code_snippets>
        {similar_failed_code_snippets}
        </similar_failed_code_snippets>

        **Guidelines and Requirements**:

        1. **Function Signature**: Implement exactly:
           `def generate_df(ds_clients, excel_files):`
           - The function should return the main dataframe that will answer the user prompt.

        2. **Data Source Usage**:
           - Use `ds_clients["<client_key>"].execute_query("SOME QUERY")` to query non-Excel data sources.
             * Use the exact `client_key` string from the <connection_clients> section — it is a literal string, not a variable.
             * Example: `ds_clients["Sales Analytics:snowflake_prod"].execute_query("SELECT * FROM orders")`
           - **Connection-Table Mapping**: Each client_key corresponds to a specific database connection. The `<connection name="...">` tags in <ground_truth_schemas> show which tables belong to which connection. Match the connection name to the client_key suffix (e.g., `<connection name="postgresql-1">` → `ds_clients["...:postgresql-1"]`). Only query tables listed under that connection.
           - **Cross-Connection Queries**: Tables from different connections cannot be joined in SQL. Query each connection separately and merge the results in Python using pandas (e.g., `pd.merge(df1, df2, on="shared_key")`).
           - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
           {data_preview_instruction}
           {lazy_query_instruction}
           - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
           - For Excel files, use `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)` to read data.
             * Decide the correct INDEX and SHEET_INDEX based on prompt and data model.
             * Print the dict/df preview to help ensure indices and positions are correct.
           - After any operation that changes DataFrame columns (merge, join, add/remove columns), print a preview using: print("df Preview:", df.head())
           - Output schema contract: The final DataFrame should contain only primitives (str/int/float/bool/None). Do not return dict/list objects. If a column is JSON/MAP/STRUCT or a JSON-looking string, extract/flatten to readable scalar columns (e.g., owner, repo_full_name) using pandas.json_normalize or by selecting key paths; otherwise stringify compactly. Prefer clear label/value columns for charting.
           - Use read-only operations on the data sources (no insert/delete/add/update/put/drop).
           - Prefer data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.

        3. **Schema and Data Model Adherence**:
           - Use only columns and relationships that exist in the provided schemas.
           - If the data model suggests derived columns or aggregations, derive them from existing schema fields.
           - Do not invent columns that do not exist or cannot be derived.
           - Do not include client names or non-relevant info inside queries. The data source queries should be generic and directly usable by the ds_clients.

        4. **Handling Previous Code and Errors**:
           - If `retries` ≥ 1, review the code_and_error_messages:
             * Understand the error.
             * If it's related to a missing column or invalid query, fix it by removing or correcting that column/query.
           - If `retries` ≥ 2 and still failing due to a specific column or measure, remove that problematic part and return a reduced but valid DataFrame.
           - Ensure you produce some output even if reduced. Not returning anything is worse than returning partial data.

        5. **Sorting and Final Output**:
           - Sort the DataFrame by the most relevant key column.
             * If it's a time or date column, sort descending.
             * If it's a count or sum, also sort descending.
             * Otherwise, sort ascending.

        6. **Data Formatting**:
           - Make sure the DataFrame is two-dimensional, with well-defined rows and columns.
           - Handle missing values gracefully.

        7. **No Extra Formatting**:
           - Return the code for the `generate_df` function as plain text only.
           - No Markdown, no extra comments beyond necessary Python code comments.
           - Do not wrap code in triple backticks or any markup.
        
        8. **End of code**:
           - At the end of the function, before returning the df — print the df preview last time using: print("Final df Preview:", {data_preview_instruction})
           - Return the df as the final output. Make sure the df name is the right one and reflects the main dataframe.

        **Approach**:
        - Start from scratch or modify the existing code if `prev_data_model_code_pair` is provided.
        - Integrate data from `ds_clients` and `excel_files` as needed. Print the dict/df preview to help the LLM ensure indices and positions are correct.
        - Carefully build queries.
        - Test logic in your mind to avoid errors.
        - If error hints are provided (from previous retries), address them directly.

        Now produce ONLY the Python function code as described. Do not output anything else besides the function python code. No markdown, no comments, no triple backticks, no triple quotes, no triple anything, no text, no anything.
        """

        result = await asyncio.to_thread(
            self.llm.inference, text, usage_scope="create_data.code_gen"
        )

        # Remove markdown code fence (with optional language tag) if present
        result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
        # Remove any closing fence lines that are just ```
        result = re.sub(r'(?m)^\s*```\s*$', '', result)
        # Defensive: remove a leading standalone language tag line (e.g., "python" or "json")
        result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
        # Remove any code after return df
        result = re.sub(r'(?s)return\s+df.*$', 'return df', result)
        return result
    
    _SINGLE_VALUE_VIZ_TYPES = {"count", "metric_card"}

    @classmethod
    def _build_viz_directive(cls, target_visualization_type: str | None) -> str:
        """Data-shape contract for the visualization the result will render as.

        Measures must stay raw numeric dtypes: the renderer parses cells as
        numbers, so a display-formatted string ("₪29,134,139") breaks cards,
        chart axes and aggregations. For single-value cards the result must be
        a single row so the card never has to guess which row is the answer.
        These rules outrank org instructions asking for formatted output — the
        UI applies currency symbols and separators at display time.
        """
        t = (target_visualization_type or "").strip().lower()
        if not t or t == "table":
            return ""
        base = (
            "**Visualization data contract (takes precedence over any conflicting "
            "formatting instructions):**\n"
            f"            - The result will be rendered as a `{t}` visualization.\n"
            "            - Keep every measure column as a raw numeric dtype (int/float). Never "
            "format numbers into display strings — no currency symbols, no thousands "
            "separators, no units inside values. Presentation formatting is applied by the "
            "visualization layer, not the data.\n"
        )
        if t in cls._SINGLE_VALUE_VIZ_TYPES:
            base += (
                "            - This is a single-value KPI card: return exactly ONE row, with the "
                "metric the user asked for as a numeric column (plus optional numeric "
                "comparison columns). Do NOT return a label/value summary table with one row "
                "per metric.\n"
            )
        return base

    @staticmethod
    def _build_reuse_directive(loadables_context: str, prompt_text: str) -> str:
        """Force load_step reuse when the user refers to an available step.

        Detected programmatically (not left to the model) so a weak model can't
        drift back to writing SQL from scratch. Returns "" when nothing matches.
        """
        if not loadables_context:
            return ""
        import re as _re
        titles = _re.findall(r'<step\b[^>]*\btitle="([^"]+)"', loadables_context)
        if not titles:
            return ""
        low = (prompt_text or "").lower()
        referenced = [t for t in titles if t and t.lower() in low]
        reuse_words = (
            "load_step", "reuse", "re-use", "the step", "that step", "you built",
            "you just", "previous", "earlier", "existing", "already built",
        )
        has_reuse_language = any(w in low for w in reuse_words)
        if referenced:
            name = referenced[0]
            return (
                "**REUSE REQUIRED (do not write SQL from scratch):** The user is referring to the "
                f'existing step "{name}" listed in <available_steps>. You MUST add `load_step` to your '
                f'signature and start from `load_step("{name}")`, then transform that DataFrame to '
                "answer the request. Do NOT re-query the database or fabricate/hardcode data to reconstruct it."
            )
        if has_reuse_language:
            return (
                "**PREFER REUSE:** The user appears to be referring to data already built in "
                '<available_steps>. Prefer loading it with `load_step("<name>")` over re-querying or '
                "rebuilding from scratch. Do NOT fabricate data."
            )
        return ""

    @staticmethod
    def _render_error_feedback(code_and_error_messages, limit: int = 2) -> str:
        """Render the failing (code, error) pairs of THIS request's earlier
        attempts for prompt inclusion, so a retry can actually correct the
        failure instead of re-rolling blind. Bounded to the most recent
        `limit` attempts to keep the prompt small."""
        if not code_and_error_messages:
            return "None"
        parts = []
        for code, error in code_and_error_messages[-limit:]:
            parts.append(
                f"<failed_attempt>\n<code>\n{code or ''}\n</code>\n<error>\n{error or ''}\n</error>\n</failed_attempt>"
            )
        return "\n".join(parts)

    @staticmethod
    def _render_last_failed_observation(last_observation) -> str:
        """When the previous tool call failed (e.g. the planner re-invoked
        inspect_data after a sandbox violation), surface that failure so the
        fresh codegen doesn't repeat it. Successful observations return ""
        — a prior success is noise for a quick inspection."""
        if not isinstance(last_observation, dict):
            return ""
        obs = last_observation.get("observation") or {}
        if not isinstance(obs, dict) or obs.get("success") is not False:
            return ""
        err = obs.get("error")
        if isinstance(err, dict):
            err_text = err.get("detail") or err.get("message") or ""
        else:
            err_text = str(err or "")
        err_text = err_text or obs.get("summary") or ""
        if not err_text:
            return ""
        section = (
            "- The previous attempt at this task FAILED — do not repeat its mistake:\n"
            f"<previous_failed_attempt>\n<error>\n{err_text}\n</error>"
        )
        code = obs.get("code") or ""
        if code:
            section += f"\n<code>\n{str(code)[:1500]}\n</code>"
        return section + "\n</previous_failed_attempt>"

    async def generate_code(
        self,
        data_model,  # kept for signature compatibility; ignored
        prompt,
        interpreted_prompt,
        schemas,
        ds_clients,
        excel_files,
        code_and_error_messages,
        memories,
        previous_messages,
        retries,
        prev_data_model_code_pair=None,
        sigkill_event=None,
        code_context_builder=None,
        context: CodeGenContext | None = None,
    ):
        # Optional early exit if a cancellation was requested before generation
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            return "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame()"
        # If a typed context is provided, use it exclusively (no ContextHub reads)
        if context is not None:
            instructions_context = context.instructions_context or ""
            mentions_context = context.mentions_context or "<mentions>No mentions for this turn</mentions>"
            entities_context = context.entities_context or ""
            loadables_context = context.loadables_context or ""
            messages_context = context.messages_context or ""
            resources_context = context.resources_context or ""
            files_context = context.files_context or ""
            platform = context.platform
            history_summary = context.history_summary or ""
            past_observations = context.past_observations or []
            last_observation = context.last_observation
            # Override schemas/prompt with curated ones from context
            schemas = context.schemas_excerpt or schemas
            prompt = context.interpreted_prompt or context.user_prompt or prompt
            data_preview_instruction = f"- Also, after each query or DataFrame creation, print the data using: print('df head:', df.head())" if self.enable_llm_see_data else ""
            lazy_query_instruction = self._lazy_query_instruction()
            # If the user is clearly referring to a step we can load, force reuse
            # via load_step instead of writing SQL from scratch. Detected here (not
            # left to the model) so a weak model can't drift back to re-querying.
            reuse_directive = self._build_reuse_directive(
                loadables_context,
                f"{context.user_prompt or ''}\n{context.interpreted_prompt or ''}",
            )
            viz_directive = self._build_viz_directive(getattr(context, "target_visualization_type", None))
            # Retrieve top successful snippets based on targeted tables if provided
            similar_successful_code_snippets = ""
            try:
                if getattr(context, "tables_by_source", None):
                    builder = None
                    try:
                        # Prefer explicit code_context_builder param when provided
                        if code_context_builder is not None:
                            builder = code_context_builder
                        elif self.context_hub is not None:
                            from app.ai.context.builders.code_context_builder import CodeContextBuilder
                            # ContextHub is initialized with db and organization
                            db = getattr(self.context_hub, "db", None)
                            organization = getattr(self.context_hub, "organization", None)
                            current_user = getattr(self.context_hub, "user", None)
                            if db is not None and organization is not None:
                                builder = CodeContextBuilder(db=db, organization=organization, current_user=current_user)
                    except Exception:
                        builder = None
                    if builder is not None and hasattr(builder, "get_top_successful_snippets_for_tables"):
                        try:
                            top_success = await builder.get_top_successful_snippets_for_tables(context.tables_by_source, top_k=2)
                            if isinstance(top_success, list) and top_success:
                                lines = ["=== SUCCESSFUL EXAMPLES (by targeted tables) ==="]
                                for idx, s in enumerate(top_success, start=1):
                                    lines.append(f"[{idx}] step_id={s.get('step_id')} score={s.get('score')} success_rate={s.get('success_rate')}")
                                    code = s.get("code") or ""
                                    lines.append(code)
                                    lines.append("")
                                similar_successful_code_snippets = "\n".join(lines).strip()
                        except Exception as e:
                            similar_successful_code_snippets = ""
            except Exception:
                similar_successful_code_snippets = ""
            text = f"""
            Role: data engineer and data scientist working on the user's analytics request.

            Goal: Given the user's prompt and the provided context, generate a Python function named `generate_df(ds_clients, excel_files)`
            that produces a Pandas DataFrame grounded only in the provided schemas and resources.
            {reuse_directive}
            {viz_directive}

            **Organization Instructions** (authored by the user; apply them):
            {instructions_context}

            **Context and Inputs**:
            - Current Time: {self._time_context()}
              Resolve relative date expressions ("today", "last week", "this month") against this time, not the database server's clock.

            - User Prompt:
            <user_prompt>
            {prompt}
            </user_prompt>

            - Interpreted Prompt:
            <interpreted_prompt>
            {interpreted_prompt}
            </interpreted_prompt>

            - Provided Schemas (Ground Truth):
            <ground_truth_schemas>
            {schemas}
            </ground_truth_schemas>

            - Resources:
            {resources_context}

            - Files:
            {files_context}

            - Connection Clients:
            <connection_clients>
            {context.data_sources_context or ""}
            </connection_clients>

            - Mentions:
            {mentions_context}

            - Entities:
            {entities_context}

            - Available steps (loadable via load_step):
            {loadables_context}

            - Messages (recent):
            <messages>
            {messages_context}
            </messages>

            - Past Observations:
            <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>

            - Last Observation:
            <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

            - Previous code attempts for THIS request that FAILED (retry #{retries}; fix these errors, do not repeat them):
            <code_and_error_messages>
            {self._render_error_feedback(code_and_error_messages)}
            </code_and_error_messages>

            - Similar successful code snippets (for reference on what is working):
            <similar_successful_code_snippets>
            {similar_successful_code_snippets}
            </similar_successful_code_snippets>

            {_sandbox_rules_section()}

            **Guidelines and Requirements**:

            0. **Data Modeling**:
                - The data structure should answer the user prompt and be feasible given the schemas and data sources.
                - Bias for a master table: include additional columns that are relevant for filtering and slicing in the visualization layer, even if not explicitly requested by the user. For example, if the user asks for total sales by region, also include date and product category columns if available.
                - The interpreted_prompt may list specific tables, target columns, and additional columns for filtering. Include all of them in your SELECT.
                - **Data granularity:** When the interpreted_prompt says "return granular rows" or "do not pre-aggregate", do not add GROUP BY or aggregate functions (SUM/COUNT/AVG) in SQL. Return one row per record — the visualization layer handles aggregation. Only pre-aggregate when the interpreted_prompt explicitly requires SQL-level computation (window functions, rolling averages, CTEs, complex calculations).

            1. **Function Signature**: Implement either:
               `def generate_df(ds_clients, excel_files):` — when no web fetching is needed.
               `def generate_df(ds_clients, excel_files, http):` — when fetching URLs (see HTTP section below).
               - You may also add `load_step` and/or `load_entity` parameters to reuse existing results (see section 2a), e.g. `def generate_df(ds_clients, excel_files, load_step):`.
               - The function should return the main dataframe that answers the user prompt.

            1a. **HTTP client (when the task involves URLs)**:
               - When fetching web pages, accept a third parameter `http` in your signature. It is a pre-built sync client; do NOT `import httpx`, `requests`, `urllib`, `asyncio`, `socket`, or `threading` (all forbidden by the sandbox).
               - **Do NOT import `bs4`, `lxml`, `html.parser`, or any HTML parser.** The pages returned by `http.get`/`http.batch_get` are ALREADY parsed for you — see the field list below.
               - `http.get(url, timeout=15) -> FetchedPage` for a single URL.
               - `http.batch_get(urls, concurrency=20, timeout=15) -> list[FetchedPage]` for many URLs in parallel. Prefer this over a Python loop of `http.get` whenever you have more than ~5 URLs.
               - **Access `FetchedPage` fields with dot notation directly — do NOT use `getattr` or `hasattr` (both are forbidden by the sandbox). The fields always exist; check truthiness (`if page.text:`) rather than presence.**
               - `FetchedPage` is a dataclass with these pre-extracted fields — read them directly, don't re-parse:
                 * `.url`, `.final_url`, `.status`, `.success`
                 * `.title` — already extracted from `<title>` (or `og:title` via `.meta`)
                 * `.description` — already extracted from meta description / `og:description`
                 * `.text` — **already the visible text content** with `<script>`, `<style>`, `<nav>`, `<footer>` etc. stripped and whitespace collapsed. Use `len(page.text)` directly for "text length"; do NOT pipe it through BeautifulSoup.
                 * `.meta` — dict of all meta tags (`og:*`, `twitter:*`, `product:price:amount`, etc.)
                 * `.json_ld` — list of parsed JSON-LD dicts (common for Product/Offer/Article schemas on retail sites)
                 * `.headings` — list of h1/h2 text
                 * `.truncated` — bool; True if content was capped
                 * `.error` — str when the fetch failed; `.success` is False in that case
               - Failures never raise — they appear as pages with `.error` set. Filter them: `good = [p for p in pages if p.success and not p.error]`.
               - For HTML pages, prefer structured fields in this order when extracting prices/ratings/stock/etc.: (1) `json_ld`, (2) `meta`, (3) regex on `.text`. Always fall back gracefully — write the value as `None` for rows you can't parse rather than crashing.
               - For non-HTML responses (JSON, XML, plain text — check `.content_type`), `.text` contains the raw body; parse it directly (e.g. `json.loads(page.text)`).
               - The `http` parameter will be `None` if the organization disabled web fetch. Guard with `if http is None: raise RuntimeError("web fetch is disabled for this organization")` and return an empty DataFrame.

            2. **Data Source Usage**:
               - Use `ds_clients["<client_key>"].execute_query("SOME QUERY")` to query non-Excel data sources.
                 * Use the exact `client_key` string from the <connection_clients> section — it is a literal string, not a variable.
                 * Example: `ds_clients["Sales Analytics:snowflake_prod"].execute_query("SELECT * FROM orders")`
               - **Connection-Table Mapping**: Each client_key corresponds to a specific database connection. The `<connection name="...">` tags in <ground_truth_schemas> show which tables belong to which connection. Match the connection name to the client_key suffix (e.g., `<connection name="postgresql-1">` → `ds_clients["...:postgresql-1"]`). Only query tables listed under that connection.
               - **Cross-Connection Queries**: Tables from different connections cannot be joined in SQL. Query each connection separately and merge the results in Python using pandas (e.g., `pd.merge(df1, df2, on="shared_key")`).
               - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
               {data_preview_instruction}
               {lazy_query_instruction}
               - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
               - For Excel files, use `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)`.
                 * Decide the correct INDEX and SHEET_INDEX based on prompt and schemas.
                 * Use prints to help validate indices and positions.
               - After any operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Info:", df.info())
               - Output schema contract: The final DataFrame should contain only primitives (str/int/float/bool/None). Do not return dict/list objects. If a column is JSON/MAP/STRUCT or a JSON-looking string, extract/flatten to readable scalar columns (e.g., owner, repo_full_name) using pandas.json_normalize or by selecting key paths; otherwise stringify compactly. Prefer clear label/value columns for charting.
               - Use read-only operations on the data sources (no insert/delete/add/update/put/drop).
               - Prefer data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.

            2a. **Reusing existing results (load_step / load_entity)** — IMPORTANT:
               - When the user refers to data they already built (e.g. "the Customer Sales step", "the step you just built", "reuse ...") or asks you NOT to re-query, you MUST load that data with `load_step` rather than re-querying or inventing it. **NEVER fabricate, hardcode, or randomly generate rows** to stand in for real data — load the real step/entity instead.
               - To use them, add the parameters to your signature, e.g. `def generate_df(ds_clients, excel_files, load_step, load_entity):` (any subset, in any order after `excel_files`).
               - `load_step("<id or name>")` returns a pandas DataFrame for a prior step in THIS report. Choose one from the `<available_steps>` section above (match its `id`, `slug`, or `title` exactly).
               - Do NOT reload a prior step's data with `pd.read_csv(...)` or by reading from `excel_files` — those are for user-uploaded files only. To reuse a previous step, use `load_step(...)`.
               - `load_entity("<id or name>")` returns a pandas DataFrame for a published catalog entity from the `<entities>` section.
               - **The argument MUST be a string literal** (e.g. `load_step("Customer Sales")`), not a variable — it is pre-resolved before your code runs.
               - Returned data is a **cached snapshot**: it may be row-capped (~1000 rows) and date/decimal columns arrive as strings. Treat it as a reference/lookup table; use `pd.to_datetime(...)`/`.astype(...)` if you need typed values before joining.
               - Example — add a column to a prior step without touching the database:
                 `def generate_df(ds_clients, excel_files, load_step):`
                 `    df = load_step("Customer Sales")`
                 `    df["tier"] = df["TotalSales"].astype(float).apply(lambda v: "High" if v >= 40 else "Low")`
                 `    return df`

            3. **Schema Adherence**:
               - Use only columns and relationships that exist in the provided schemas.
               - Do not invent columns that do not exist or cannot be derived.
               - Use metadata resources for tables/cols enrichments, code examples, etc.
               - Do not use tables/cols that exist in instructions but are not in the provided schemas.

            4. **Handling Previous Code and Errors**:
               - If the <code_and_error_messages> section above is not "None", review each failed attempt:
                 * Understand the error and write code that cannot fail the same way.
                 * If it's related to a missing column or invalid query, fix it by removing or correcting that column/query.
                 * If it's a "Security violation" from the sandbox, rewrite the code without the forbidden construct (see the sandbox rules above).
               - If `retries` ≥ 2 and still failing due to a specific column or measure, remove that problematic part and return a reduced but valid DataFrame.
               - Ensure you produce some output even if reduced.
               - If the error is related to size of the query, try to use partitions when available in context/metadata resources.

            5. **Sorting and Final Output**:
               - If not mentioned by user, sort by the most relevant key column.

            6. **Data Formatting**:
               - Ensure the DataFrame is two-dimensional and handle missing values.
               - Keep numeric measures as numeric dtypes (int/float). Do not format numbers
                 into display strings (no currency symbols or thousands separators inside
                 values) — the visualization layer handles presentation formatting.

            7. **No Extra Formatting**:
               - Return ONLY the Python function code for `generate_df`.

            8. **End of code**:
               - Before returning the df — print("Final df Info:", df.info())
               {data_preview_instruction}
               - Return the df.

            Now produce ONLY the Python function code as described. No markdown or extra text.
            """

            chunks: list[str] = []
            with tracer.start_as_current_span("coder.generate_code_stream") as span:
                span.set_attribute("coder.retry", retries)
                span.set_attribute("coder.prompt_chars", len(text))
                span.set_attribute("coder.has_typed_context", context is not None)
                span.set_attribute("coder.allow_llm_see_data", bool(self.enable_llm_see_data))
                async for evt in self.llm.inference_stream_v2(
                    messages=[Message(role="user", content=text)],
                    usage_scope="create_data.code_gen",
                ):
                    if isinstance(evt, TextDeltaEvent):
                        chunks.append(evt.text)
                span.set_attribute("coder.chunks", len(chunks))
                span.set_attribute("coder.output_chars", sum(len(chunk) for chunk in chunks))
            result = "".join(chunks)
            result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
            result = re.sub(r'(?m)^\s*```\s*$', '', result)
            result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
            result = re.sub(r'(?s)return\s+df.*$', 'return df', result)

            return result

    async def generate_inspection_code(
        self,
        prompt,
        schemas,
        ds_clients,
        excel_files,
        code_and_error_messages,
        memories,
        previous_messages,
        retries,
        prev_data_model_code_pair=None,
        sigkill_event=None,
        code_context_builder=None,
        context: CodeGenContext | None = None,
        **kwargs  # Absorb any extra args from the executor
    ):
        # Optional early exit
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            return "def generate_df(ds_clients, excel_files):\n    return None"

        # Resolve context (similar to generate_code)
        if context is not None:
            instructions_context = context.instructions_context or ""
            resources_context = context.resources_context or ""
            files_context = context.files_context or ""
            schemas = context.schemas_excerpt or schemas
            prompt = context.interpreted_prompt or context.user_prompt or prompt
        else:
            # Fallback (minimal)
            instructions_context = ""
            resources_context = ""
            files_context = ""

        # Prepare data source descriptions
        data_source_descriptions = []
        for client_key, client in ds_clients.items():
            data_source_descriptions.append(
                f"client_key: {client_key}\ndescription: {getattr(client, 'description', 'N/A')}"
            )
        data_source_section = "\n".join(data_source_descriptions)

        # Prepare excel files
        excel_files_description = []
        for index, file in enumerate(excel_files):
            excel_files_description.append(f"{index}: {file.description}")
        excel_files_section = "\n".join(excel_files_description)

        # Cross-call memory: when the planner re-invokes inspection after a
        # failed attempt, surface that failure instead of regenerating blind.
        prev_failure_section = self._render_last_failed_observation(
            context.last_observation if context is not None else None
        )

        text = f"""
        Role: data investigator doing a quick hypothesis validation.

        Goal: Write a Python function `generate_df(ds_clients, excel_files)` that validates assumptions about data before creating tracked widgets.
        This is not for generating insights — insights come from create_data. This is just a quick peek.

        **Context and Inputs**:
        - Current Time: {self._time_context()}

        - User Prompt (Validation Goal):
        <user_prompt>
        {prompt}
        </user_prompt>

        - Schemas (already available; do not query information_schema):
        <schemas>
        {schemas}
        </schemas>

        - Files:
        {files_context}

        - Connection Clients:
        <connection_clients>
        {data_source_section}
        </connection_clients>

        - Excel Files (available via `excel_files` list):
        {excel_files_section}

        {prev_failure_section}

        - Previous code attempts for THIS request that FAILED (retry #{retries}; fix the error, do not repeat it):
        <code_and_error_messages>
        {self._render_error_feedback(code_and_error_messages)}
        </code_and_error_messages>

        {_sandbox_rules_section()}

        **Excel File Access**: Use `pd.read_excel(excel_files[INDEX].path, sheet_name=0)` to read Excel files.
        - `excel_files` is a list of File objects with `.path` attribute (NOT a dict, use `.path` not `['path']`)
        - Example: `df = pd.read_excel(excel_files[0].path, sheet_name=0)`

        **HTTP inspection (when the task involves URLs)**:
        - Signature becomes `def generate_df(ds_clients, excel_files, http):` — accept `http` as the third parameter.
        - Use `http.get(url, timeout=15)` on 1–3 sample URLs to learn what the page returns. Do NOT import `httpx`/`requests`/`urllib`/`asyncio`/`socket`/`threading`/`bs4`/`lxml` — all forbidden.
        - `FetchedPage` has exactly these fields (a dataclass, guaranteed to exist on every result): `.status`, `.success`, `.error`, `.content_type`, `.url`, `.final_url`, `.text` (raw body for non-HTML; cleaned visible text for HTML), `.title`, `.description`, `.meta` (dict), `.json_ld` (list), `.headings` (list), `.truncated`. **Access them with dot notation directly — do NOT use `getattr` or `hasattr` (both are forbidden by the sandbox). The fields always exist; check truthiness (`if page.text:`) rather than presence.**
        - Print whatever helps the next step decide how to parse — sample fields, content_type, errors, a slice of `.text`. Keep it short.
        - If `http` is `None`, web fetch is disabled — print a clear message and return `None`.

        **Constraints**:
        1. **Keep it to 2-3 queries** — this is a quick validation, not a full analysis.
        2. **Limit rows** — use `LIMIT 3` in SQL and `.head(3)` on DataFrames.
        3. **Joins within one connection are fine**, but cross-connection joins do not work in SQL. If tables are under different `<connection>` tags, query each connection separately.
        4. **Connection-Table Mapping**: Match `<connection name="...">` in schemas to the client_key suffix (e.g., `<connection name="postgresql-1">` → `ds_clients["...:postgresql-1"]`). Only query tables listed under that connection.
        5. **Do not query information_schema** — schemas are already provided above.

        **What to validate**:
        - Sample rows to see data structure
        - Distinct values for a specific column (e.g., status codes, categories)
        - Check for nulls in key columns
        - Verify join keys match between tables
        - Check date formats or value ranges

        **Print everything**: the user only sees what you `print()`.
        - `print(df.head(3))`
        - `print(df['col'].unique()[:10])`
        - `print(df['col'].isna().sum())`

        **Function Signature**: `def generate_df(ds_clients, excel_files):`

        **Return**: The inspected dataframe or `None`. The `print()` output is the primary deliverable.

        Return only the Python function code. No markdown. Keep it short.
        """

        chunks: list[str] = []
        async for evt in self.llm.inference_stream_v2(
            messages=[Message(role="user", content=text)],
            usage_scope="create_data.inspection",
        ):
            if isinstance(evt, TextDeltaEvent):
                chunks.append(evt.text)
        result = "".join(chunks)

        # Clean up code fences
        result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
        result = re.sub(r'(?m)^\s*```\s*$', '', result)
        result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)

        return result
