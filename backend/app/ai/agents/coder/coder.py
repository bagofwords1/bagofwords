import asyncio
from typing import Callable, Optional

from partialjson.json_parser import JSONParser
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLM
from app.ai.llm.types import Message, MessageStopEvent, TextDeltaEvent


# Raised message when a codegen stream stops at the model's output-token cap.
# Truncated code is a guaranteed SyntaxError downstream ("unterminated string
# literal") with feedback that misleads the retry — surfacing the cutoff here
# routes an actionable message into the executor's retry loop instead.
_TRUNCATION_ERROR = (
    "the generated code hit the model's output-token limit and was cut off "
    "mid-code. Regenerate a COMPACT version: build repetitive data "
    "programmatically (loops, ranges, io.StringIO over a CSV block) instead of "
    "inlining every row as a separate literal, and keep the row count to what "
    "the request actually needs."
)


def _is_truncation(evt) -> bool:
    return isinstance(evt, MessageStopEvent) and evt.stop_reason == "max_tokens"
from app.models.llm_model import LLMModel
import re
import json
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from app.ai.agents.planner.clock import current_time_str
from app.ai.schemas.codegen import CodeGenContext
from app.services.usage_policy_service import UsageLimitContext
from app.core.otel import get_tracer
from app.ai.code_execution.code_execution import FORBIDDEN_BUILTINS, FORBIDDEN_MODULES, ml_training_settings

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


def _ml_rules_section(enabled: bool = True, row_limit: int = 50_000) -> str:
    """Guidance for generated code that trains a model with scikit-learn.

    The sandbox is a denylist, so `import sklearn` was never *rejected* — it
    simply wasn't installed. Now that it is, the model needs to know (a) it is
    there, (b) which neighbours are NOT (statsmodels/prophet/xgboost/shap), and
    (c) the constraints the executor imposes on training code: the result must
    be a tidy DataFrame (the estimator object is never persisted — every rerun
    retrains), the code re-executes verbatim on refresh (so it must be
    deterministic), and it shares a small worker pool with a hard tool
    timeout (so it must be cheap). Shared by the codegen prompts so the
    contract cannot drift between them.
    """
    if not enabled:
        return """**Machine learning — DISABLED for this organization:** scikit-learn and scipy are not available and their imports are rejected by the sandbox. Do not train models; answer statistical asks with pandas/numpy (groupby, describe, corr, rolling, np.polyfit for a trend line)."""
    return f"""**Machine learning — scikit-learn IS available (also numpy, scipy):**
        - Import inside the function, e.g. `from sklearn.ensemble import RandomForestClassifier`. NOT installed (never import): statsmodels, prophet, xgboost, lightgbm, catboost, shap, torch, tensorflow.
        - Fit the model inside `generate_df` and return the RESULT as a tidy DataFrame of primitives — the estimator object is never returned, saved or passed between steps; a step that needs the model retrains it. Typical outputs (one shape per step, pick the one the prompt asks for):
          * feature importance: one row per feature — `feature`, `importance`, `importance_share` (importance / sum), `rank`; sorted by importance descending.
          * metrics: one row per metric — `metric`, `value` (accuracy, precision, recall, f1, roc_auc, r2, mae, rmse…).
          * predictions / residuals: one row per held-out record — the id/key columns, `actual`, `predicted`, and `residual` or `probability`.
          * confusion matrix: one row per (actual, predicted) pair — `actual`, `predicted`, `count`.
          * A single "model report" step may combine these long-form with a `section` column when the prompt asks for everything at once.
        - Deterministic: this code is re-executed verbatim on refresh — pass `random_state=42` to every estimator, `train_test_split`, `KFold`, and `permutation_importance` so charts do not change between reruns.
        - Cheap: the whole function shares a small worker pool and is killed by a hard tool timeout (~5 min). Use `n_jobs=1` (never -1), keep `n_estimators` ≤ 300 and tree `max_depth` bounded, avoid `GridSearchCV`/`RandomizedSearchCV` beyond a handful of candidates, and train on at most {row_limit:,} rows (organization limit) — `df.sample(n={row_limit}, random_state=42)` when the source is larger.
        - Feature importance: prefer `sklearn.inspection.permutation_importance(model, X_test, y_test, n_repeats=5, random_state=42, n_jobs=1).importances_mean` (model-agnostic, measured on held-out data). Use `feature_importances_` (trees) or `abs(coef_)` (linear) only when the prompt names that method or the data is too small to hold out.
        - Preprocessing: drop or impute NaNs before fitting; one-hot encode categoricals with `pd.get_dummies(..., drop_first=False)` or `OneHotEncoder(handle_unknown="ignore")` inside a `Pipeline`/`ColumnTransformer`.
        - Feature names in the output must be human-readable: build the `ColumnTransformer` with `verbose_feature_names_out=False` (otherwise `get_feature_names_out()` yields `numeric__tenure_months` / `categorical__contract_type_month-to-month`) and render one-hot columns as `"<column>=<value>"` (e.g. `contract_type=month-to-month`). A viewer should never see transformer prefixes in a chart label.
        - Sandbox-compatible idioms: `type(model).__name__` (never `model.__class__.__name__`); branch on estimator kind with `isinstance` or try/except (never `hasattr`/`getattr`); never `import pickle` or `joblib` — models are not serialized.
        - If the step declares a parameter such as `model_type` or `target`, read it via `params.get("model_type", "random_forest")` and branch on it so the viewer can switch models from the dashboard; keep the output columns identical across branches."""


def _time_filter_rules() -> str:
    """Policy for date/time filters in generated code.

    Generated code is saved and re-executed verbatim later — on dashboard
    refresh and on scheduled runs (step_service.rerun_step) — so a relative ask
    ("yesterday", "latest day") frozen into a literal date returns permanently
    stale data on every rerun. These rules keep time windows dynamic at
    execution time; they are shared across the codegen prompts so the policy
    cannot drift between them.
    """
    return """**Time filters — this code is SAVED and RE-EXECUTED verbatim later (dashboard refresh, scheduled runs), so date logic must stay correct on every rerun:**
        - NEVER freeze a relative ask into a literal date (e.g. `WHERE order_date = DATE '2026-07-27'` for "yesterday"). The literal is permanently stale on the next run.
        - "latest / most recent day (week, month) in the data" → derive it from the data itself, e.g. `WHERE order_date = (SELECT MAX(order_date) FROM sales)`. This is the default for recency asks and stays correct even when data loads late.
        - Rolling windows ("last 7 days", "this month", "yesterday") → compute them at execution time: use the engine's relative date functions (each connection's description in <connection_clients> shows its syntax), or compute the boundary dates in Python with `datetime`/`zoneinfo` in the organization's timezone from the Current Time line and interpolate them into the query string. Prefer the Python route when day boundaries matter — the database server's clock/timezone may differ from the organization's.
        - Use a literal date ONLY when the user explicitly named a fixed date or range ("on July 27", "Q1 2026").
        - This rule OUTRANKS any hardcoded-date patterns you may see in example snippets or previous code."""

# Anything the model appends after the function's final `return df…` — stray
# prose, a usage example, a second definition — is trimmed. The leading `.*` is
# greedy so this anchors on the LAST such return, leaving an early
# `return df_empty` inside a branch alone.
_FINAL_DF_RETURN = re.compile(
    r'(?s)^(.*\breturn\s+df[A-Za-z0-9_]*[^\S\n]*)(?:\n.*)?$'
)


def trim_after_final_df_return(code: str) -> str:
    """Drop whatever follows the generated function's last `return df…`.

    Keeps the returned NAME. This was previously
    ``re.sub(r'(?s)return\\s+df.*$', 'return df', code)`` — a replacement rather
    than a trim, which turned `return df_aggregated` into `return df` and
    shipped the pre-aggregation frame as the answer. Nothing errored: a request
    for a per-planner summary simply rendered as several hundred raw rows.
    """
    return _FINAL_DF_RETURN.sub(r'\1', code)


def _file_access_rules(indent: str = "") -> str:
    """How to read an entry in `excel_files`.

    The list is named for its original Excel-only use, but it now also carries
    CSV and JSON files (tool results materialized during the run). Reaching for
    `pd.read_excel` on those raises, and a model that catches the error and
    returns an empty frame ships a 0-row result that looks like a success — so
    spell out both the reader per extension and the no-swallowing rule.
    """
    lines = [
        "- `excel_files` is NOT Excel-only — it also carries CSV and JSON files (e.g. results saved from a connected tool).",
        "  Pick the reader from the file's extension in the <excel_files> index; `excel_files[INDEX]` is a File object, so use `.path` (dot access, not `['path']`).",
        "  * `.xlsx` / `.xls` → `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)`",
        "  * `.csv` / `.tsv` → `pd.read_csv(excel_files[INDEX].path)`",
        "  * `.ndjson` / `.jsonl` → `pd.read_json(excel_files[INDEX].path, lines=True)`",
        "  * `.json` holding a top-level array of records → `pd.read_json(excel_files[INDEX].path)`",
        "  * `.json` holding records nested under a key → `payload = pd.read_json(excel_files[INDEX].path, typ=\"series\").to_dict()`, then `df = pd.json_normalize(payload[\"<key>\"])`.",
        "    The index line names that key (e.g. \"150 records at 'data'\"). `open()` is sandbox-forbidden, so read JSON through pandas, never `json.load(open(...))`.",
        "  * `.txt` / `.log` / `.md` → `read_text(excel_files[INDEX])` returns the file's text as a string. This is the ONLY way to read a text file — `open()` is forbidden.",
        "    NEVER use `pd.read_csv` on prose: it does not raise, it returns a plausible-looking frame built from wherever commas happened to fall. Only use `pd.read_csv` on text when the file is genuinely delimited.",
        "  * `.pdf` / `.docx` / `.pptx` / images → NOT readable from generated code at all. Do not attempt it; the planner must use the `read_file` tool instead.",
        "  * NEVER call `pd.read_excel` on a `.json` or `.csv` path — it raises `Excel file format cannot be determined` and the data is lost.",
        "- Let a failing read raise. Do NOT wrap a source read in `try/except` that falls through to an empty list or empty DataFrame:",
        "  a silently empty result is returned as a successful 0-row answer, whereas a raised error comes back to you with the message so you can fix the reader.",
    ]
    # The first line inherits the placeholder's own indentation in the template.
    return f"\n{indent}".join(lines)


# Formats with no reader inside the sandbox — read_file handles them instead.
_CODEGEN_UNREADABLE_EXTS = {"pdf", "docx", "pptx", "png", "jpg", "jpeg", "gif", "webp"}


def _excel_files_mapping(excel_files) -> str:
    """Compact index→file mapping for <excel_files>. Rich sample previews live
    once in the tiered <files> section (or in inspect_data observations) — this
    section only anchors `excel_files[INDEX]` to a concrete file and its sheet
    order, so indices stay stable without re-inlining every file's preview."""
    from app.services.file_preview import render_file_index_line
    lines = []
    for index, f in enumerate(excel_files):
        preview = getattr(f, "preview", None)
        try:
            line = render_file_index_line(preview, f.path or "", filename=f.filename)
        except Exception:
            line = getattr(f, "filename", None) or "unknown"
        if not preview:
            # Without a preview the rendered line says only "no preview" — name
            # the type so the model still picks the right reader.
            content_type = getattr(f, "content_type", None)
            if content_type:
                line = f"{line} (content_type: {content_type})"
        # `excel_files` carries every non-image report file, including formats
        # generated code cannot open. Unmarked, a PDF sits in this list looking
        # exactly as loadable as a CSV and invites an attempt that can only fail.
        name = str(getattr(f, "filename", "") or "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _CODEGEN_UNREADABLE_EXTS:
            line = f"{line} [NOT loadable in code — use the read_file tool]"
        lines.append(f"{index}: (file_id={getattr(f, 'id', '')}) {line}")
    return "\n".join(lines)


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
        from app.ai.prompt_formatters import render_ds_client_entry
        data_source_section = "\n".join(
            render_ds_client_entry(client_key, client)
            for client_key, client in ds_clients.items()
        )

        # Prepare excel files mapping (previews live in {files_context} below)
        excel_files_section = _excel_files_mapping(excel_files)
        file_access_rules = _file_access_rules(" " * 11)

        # Define data preview instruction based on enable_llm_see_data flag
        data_preview_instruction = f"- Also, after each query or DataFrame creation, print the data using: print('df head:', df.head())" if self.enable_llm_see_data else ""

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
          Use this to understand what relative phrases ("today", "last week", "this month") refer to — but do NOT bake the resolved dates into the code as literals; follow the Time filters rules below.

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

        - Files (uploaded/attached; detail="index" entries need inspect_data/read_file before use):
        {files_context}

        - Excel Files (index→file mapping for `excel_files[INDEX]`):
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

        {_time_filter_rules()}

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
           - **Power BI connections**: `execute_query` needs the target semantic model — pass the schema table name (format `Dataset/Table`, exactly as shown in the schema) as the SECOND argument: `execute_query("EVALUATE Customers", "SalesModel/Customers")`. Alternatively pass `dataset_id=`/`workspace_id=` from the table's `<powerbi .../>` metadata. Never ask the user for these IDs.
           - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
           {data_preview_instruction}
           - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
           {file_access_rules}
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
        result = trim_after_final_df_return(result)
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
    def _build_reuse_prompt_sections(load_step_enabled: bool) -> tuple[str, str]:
        """Return (signature_hint, section_2a) for the reuse guidance.

        When `load_step_enabled` is False the copy describes `load_entity` only,
        so the model is never told about a call that would raise at runtime.
        `load_entity` is org-independent and always documented.
        """
        if load_step_enabled:
            signature_hint = (
                "- You may also add `load_step` and/or `load_entity` parameters to reuse existing "
                "results (see section 2a), e.g. `def generate_df(ds_clients, excel_files, load_step):`."
            )
            section = (
                "2a. **Reusing existing results (load_step / load_entity)** — IMPORTANT:\n"
                '               - When the user refers to data they already built (e.g. "the Customer Sales step", "the step you just built", "reuse ...") or asks you NOT to re-query, you MUST load that data with `load_step` rather than re-querying or inventing it. **NEVER fabricate, hardcode, or randomly generate rows** to stand in for real data — load the real step/entity instead.\n'
                "               - To use them, add the parameters to your signature, e.g. `def generate_df(ds_clients, excel_files, load_step, load_entity):` (any subset, in any order after `excel_files`).\n"
                "               - `load_step(\"<id or name>\")` returns a pandas DataFrame for a prior step in THIS report. Choose one from the `<available_steps>` section above (match its `id`, `slug`, or `title` exactly).\n"
                "               - Do NOT reload a prior step's data with `pd.read_csv(...)` or by reading from `excel_files` — those are for user-uploaded files only. To reuse a previous step, use `load_step(...)`.\n"
                "               - `load_entity(\"<id or name>\")` returns a pandas DataFrame for a published catalog entity from the `<entities>` section.\n"
                "               - **The argument MUST be a string literal** (e.g. `load_step(\"Customer Sales\")`), not a variable — it is pre-resolved before your code runs.\n"
                "               - Returned data is a **cached snapshot**: it may be row-capped (~1000 rows) and date/decimal columns arrive as strings. Treat it as a reference/lookup table; use `pd.to_datetime(...)`/`.astype(...)` if you need typed values before joining.\n"
                "               - Example — add a column to a prior step without touching the database:\n"
                '                 `def generate_df(ds_clients, excel_files, load_step):`\n'
                '                 `    df = load_step("Customer Sales")`\n'
                '                 `    df["tier"] = df["TotalSales"].astype(float).apply(lambda v: "High" if v >= 40 else "Low")`\n'
                "                 `    return df`"
            )
            return signature_hint, section

        signature_hint = (
            "- You may also add a `load_entity` parameter to reuse a published catalog entity "
            "(see section 2a), e.g. `def generate_df(ds_clients, excel_files, load_entity):`."
        )
        section = (
            "2a. **Reusing a published entity (load_entity)** — IMPORTANT:\n"
            "               - When the user refers to a published catalog entity from the `<entities>` section, you MUST load it with `load_entity` rather than re-querying or inventing it. **NEVER fabricate, hardcode, or randomly generate rows** to stand in for real data.\n"
            "               - To use it, add the parameter to your signature, e.g. `def generate_df(ds_clients, excel_files, load_entity):`.\n"
            "               - `load_entity(\"<id or name>\")` returns a pandas DataFrame for a published catalog entity from the `<entities>` section.\n"
            "               - **The argument MUST be a string literal** (e.g. `load_entity(\"Monthly Revenue Model\")`), not a variable — it is pre-resolved before your code runs.\n"
            "               - Returned data is a **cached snapshot**: it may be row-capped (~1000 rows) and date/decimal columns arrive as strings. Treat it as a reference/lookup table; use `pd.to_datetime(...)`/`.astype(...)` if you need typed values before joining."
        )
        return signature_hint, section

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
            file_access_rules = _file_access_rules(" " * 15)
            # If the user is clearly referring to a step we can load, force reuse
            # via load_step instead of writing SQL from scratch. Detected here (not
            # left to the model) so a weak model can't drift back to re-querying.
            reuse_directive = self._build_reuse_directive(
                loadables_context,
                f"{context.user_prompt or ''}\n{context.interpreted_prompt or ''}",
            )
            viz_directive = self._build_viz_directive(getattr(context, "target_visualization_type", None))
            # load_step is org-gated (default off). When disabled we advertise
            # neither the signature parameter nor its reuse section, so the model
            # never reaches for a call that would raise at runtime. load_entity
            # is independent and always described.
            from app.ai.code_execution.loadables import load_step_settings
            _load_step_enabled, _ = load_step_settings(self.organization_settings)
            signature_reuse_hint, reuse_section = self._build_reuse_prompt_sections(_load_step_enabled)
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
              Use this to understand what relative phrases ("today", "last week", "this month") refer to — but do NOT bake the resolved dates into the code as literals; follow the Time filters rules below.

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

            {_ml_rules_section(*ml_training_settings(self.organization_settings))}

            {_time_filter_rules()}

            **Guidelines and Requirements**:

            0. **Data Modeling**:
                - The data structure should answer the user prompt and be feasible given the schemas and data sources.
                - Bias for a master table: include additional columns that are relevant for filtering and slicing in the visualization layer, even if not explicitly requested by the user. For example, if the user asks for total sales by region, also include date and product category columns if available.
                - The interpreted_prompt may list specific tables, target columns, and additional columns for filtering. Include all of them in your SELECT.
                - **Data granularity:** When the interpreted_prompt says "return granular rows" or "do not pre-aggregate", do not add GROUP BY or aggregate functions (SUM/COUNT/AVG) in SQL. Return one row per record — the visualization layer handles aggregation. Only pre-aggregate when the interpreted_prompt explicitly requires SQL-level computation (window functions, rolling averages, CTEs, complex calculations).

            1. **Function Signature**: Implement either:
               `def generate_df(ds_clients, excel_files):` — when no web fetching is needed.
               `def generate_df(ds_clients, excel_files, http):` — when fetching URLs (see HTTP section below).
               {signature_reuse_hint}
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
               - **Power BI connections**: `execute_query` needs the target semantic model — pass the schema table name (format `Dataset/Table`, exactly as shown in the schema) as the SECOND argument: `execute_query("EVALUATE Customers", "SalesModel/Customers")`. Alternatively pass `dataset_id=`/`workspace_id=` from the table's `<powerbi .../>` metadata. Never ask the user for these IDs.
               - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
               {data_preview_instruction}
               - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
               {file_access_rules}
                 * Decide the correct INDEX and SHEET_INDEX based on prompt and schemas.
                 * Use prints to help validate indices and positions.
               - After any operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Info:", df.info())
               - Output schema contract: The final DataFrame should contain only primitives (str/int/float/bool/None). Do not return dict/list objects. If a column is JSON/MAP/STRUCT or a JSON-looking string, extract/flatten to readable scalar columns (e.g., owner, repo_full_name) using pandas.json_normalize or by selecting key paths; otherwise stringify compactly. Prefer clear label/value columns for charting.
               - Use read-only operations on the data sources (no insert/delete/add/update/put/drop).
               - Prefer data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.

            {reuse_section}

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
            truncated = False
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
                    elif _is_truncation(evt):
                        truncated = True
                span.set_attribute("coder.chunks", len(chunks))
                span.set_attribute("coder.output_chars", sum(len(chunk) for chunk in chunks))
                span.set_attribute("coder.truncated", truncated)
            if truncated:
                raise RuntimeError(_TRUNCATION_ERROR)
            result = "".join(chunks)
            result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
            result = re.sub(r'(?m)^\s*```\s*$', '', result)
            result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
            result = trim_after_final_df_return(result)

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
        from app.ai.prompt_formatters import render_ds_client_entry
        data_source_section = "\n".join(
            render_ds_client_entry(client_key, client)
            for client_key, client in ds_clients.items()
        )

        # Prepare excel files mapping (previews live in {files_context} above)
        excel_files_section = _excel_files_mapping(excel_files)
        file_access_rules = _file_access_rules(" " * 8)

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

        {_ml_rules_section(*ml_training_settings(self.organization_settings))}

        **File Access**:
        {file_access_rules}

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
        6. **Power BI connections**: pass the schema table name (`Dataset/Table`, exactly as shown) as the SECOND argument to `execute_query`, or `dataset_id=`/`workspace_id=` from the `<powerbi .../>` metadata. Never ask the user for these IDs.

        **What to validate**:
        - Sample rows to see data structure
        - Distinct values for a specific column (e.g., status codes, categories)
        - Check for nulls in key columns
        - Verify join keys match between tables
        - Check date formats or value ranges
        - Date coverage and freshness: when the ask involves recency ("latest", "yesterday", "this week"), print `MAX(date_col)` / `MIN(date_col)` so the next step can choose a relative filter instead of hardcoding a discovered date

        **Relative dates**: prefer relative date expressions (the engine's date functions, or a `(SELECT MAX(date_col) ...)` subquery) over resolved literal dates in queries — inspection queries often get copied into tracked steps that are re-executed later.

        **Print everything**: the user only sees what you `print()`.
        - `print(df.head(3))`
        - `print(df['col'].unique()[:10])`
        - `print(df['col'].isna().sum())`

        **Function Signature**: `def generate_df(ds_clients, excel_files):`

        **Return**: The inspected dataframe or `None`. The `print()` output is the primary deliverable.

        Return only the Python function code. No markdown. Keep it short.
        """

        chunks: list[str] = []
        truncated = False
        async for evt in self.llm.inference_stream_v2(
            messages=[Message(role="user", content=text)],
            usage_scope="create_data.inspection",
        ):
            if isinstance(evt, TextDeltaEvent):
                chunks.append(evt.text)
            elif _is_truncation(evt):
                truncated = True
        if truncated:
            raise RuntimeError(_TRUNCATION_ERROR)
        result = "".join(chunks)

        # Clean up code fences
        result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
        result = re.sub(r'(?m)^\s*```\s*$', '', result)
        result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)

        return result

    async def generate_transform_code(
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
        **kwargs,
    ):
        """Codegen for write_csv: reshape a source into a COMPLETE table.

        write_csv used to borrow `generate_inspection_code`, whose prompt is
        written for a quick peek — "keep it to 2-3 queries", `LIMIT 3`,
        `.head(3)`, "the print() output is the primary deliverable". Every one
        of those instructions is wrong for a tool whose entire job is to emit a
        full dataset, and they were being handed to the model directly above
        write_csv's own "save the whole DataFrame" instruction.
        """
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            return "def generate_df(ds_clients, excel_files):\n    return None"

        if context is not None:
            instructions_context = context.instructions_context or ""
            files_context = context.files_context or ""
            schemas = context.schemas_excerpt or schemas
            prompt = context.interpreted_prompt or context.user_prompt or prompt
        else:
            instructions_context = ""
            files_context = ""

        from app.ai.prompt_formatters import render_ds_client_entry
        data_source_section = "\n".join(
            render_ds_client_entry(client_key, client)
            for client_key, client in ds_clients.items()
        )
        excel_files_section = _excel_files_mapping(excel_files)
        file_access_rules = _file_access_rules(" " * 8)
        prev_failure_section = self._render_last_failed_observation(
            context.last_observation if context is not None else None
        )

        text = f"""
        Role: data engineer producing a finished table.

        Goal: Write a Python function `generate_df(ds_clients, excel_files)` that builds the
        COMPLETE dataset the request asks for and returns it as a pandas DataFrame.

        **Organization Instructions** (authored by the user; apply them):
        {instructions_context}

        **Context and Inputs**:
        - Current Time: {self._time_context()}

        - Request:
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

        - Excel Files (index→file mapping for `excel_files[INDEX]`):
        <excel_files>
        {excel_files_section}
        </excel_files>

        {prev_failure_section}

        - Previous code attempts for THIS request that FAILED (retry #{retries}; fix the error, do not repeat it):
        <code_and_error_messages>
        {self._render_error_feedback(code_and_error_messages)}
        </code_and_error_messages>

        {_sandbox_rules_section()}

        **File Access**:
        {file_access_rules}

        {_time_filter_rules()}

        **Constraints**:
        1. **Return every row.** This output IS the deliverable — it gets saved and charted.
           Do NOT sample, do NOT `.head(n)`, do NOT add a `LIMIT` unless the request asks for a top-N.
        2. **Aggregate only when asked.** If the request says "per X" / "total by X" / "summary",
           group and aggregate. Otherwise return the rows as they are.
        3. **Return the frame you actually built.** If you name it `df_summary`, return `df_summary` —
           returning an earlier, unaggregated frame silently ships the wrong answer.
        4. **Let a failing read raise.** Never wrap a source read in try/except that falls through to
           an empty DataFrame: a silent 0-row result is reported as a successful empty answer.
        5. **Flatten to primitives.** The final DataFrame must contain only str/int/float/bool/None.
           Flatten nested JSON with `pd.json_normalize` or select key paths; never leave dict/list objects
           in a column — they serialize as unreadable Python reprs.
        6. Cross-connection joins do not work in SQL; query each connection separately and merge in pandas.
        7. Use read-only operations on data sources (no insert/update/delete/drop).
        8. **Embedding literal text values**: Hebrew/Arabic text embeds the ASCII double-quote
           INSIDE words as an abbreviation mark (e.g. ארה"ב, צה"ל, מנכ"ל), so quote-bearing
           values are the norm there, not the exception. Wrap every text literal in SINGLE
           quotes ('ארה"ב' — no escaping needed) or triple quotes; NEVER write double-quoted
           literals with backslash-escaped quotes (\"...\") — mixed-direction text makes those
           escapes easy to misplace and the whole function fails to parse. Avoid inline CSV
           blocks for such text (embedded quotes break CSV parsing too); a list of
           single-quoted tuples is the safe shape.

        **Print for verification**: `print(df.head())` and `print(f'Shape: {{df.shape}}')` so the shape
        is visible in the log. Printing is for verification only — the DataFrame is the deliverable.

        **Function Signature**: `def generate_df(ds_clients, excel_files):`

        Return only the Python function code. No markdown, no backticks, no commentary.
        """

        chunks: list[str] = []
        truncated = False
        async for evt in self.llm.inference_stream_v2(
            messages=[Message(role="user", content=text)],
            usage_scope="write_csv.transform",
        ):
            if isinstance(evt, TextDeltaEvent):
                chunks.append(evt.text)
            elif _is_truncation(evt):
                truncated = True
        if truncated:
            raise RuntimeError(_TRUNCATION_ERROR)
        result = "".join(chunks)

        result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
        result = re.sub(r'(?m)^\s*```\s*$', '', result)
        result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
        result = trim_after_final_df_return(result)

        return result
