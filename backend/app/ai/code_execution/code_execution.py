import io
import sys
import ast
import re
import pandas as pd
import numpy as np
import datetime
import json
import uuid
from contextlib import redirect_stdout
from typing import Dict, Any, Tuple, List, Optional, Callable, Coroutine
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.ai.context.builders.code_context_builder import CodeContextBuilder
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest


# =============================================================================
# Security Exceptions
# =============================================================================

class CodeSecurityError(Exception):
    """Base exception for code security violations."""
    pass


class UnsafePythonError(CodeSecurityError):
    """Raised when Python code contains dangerous constructs."""
    pass


class UnsafeSQLError(CodeSecurityError):
    """Raised when SQL query contains dangerous operations."""
    pass


# =============================================================================
# AST-based Python Code Validation
# =============================================================================

# Modules that should never be imported
FORBIDDEN_MODULES = frozenset({
    'os', 'subprocess', 'sys', 'shutil', 'importlib', 'builtins',
    'code', 'pty', 'socket', 'requests', 'urllib', 'http', 'ftplib',
    'telnetlib', 'smtplib', 'poplib', 'imaplib', 'nntplib',
    'multiprocessing', 'threading', 'concurrent', 'asyncio',
    'ctypes', 'cffi', 'pickle', 'shelve', 'marshal',
    'tempfile', 'pathlib', 'glob', 'fnmatch',
    'signal', 'resource', 'sysconfig', 'platform',
    'webbrowser', 'antigravity', 'this',
})

# Built-in functions that should never be called
FORBIDDEN_BUILTINS = frozenset({
    'eval', 'exec', 'compile', 'open', 'input',
    '__import__', 'globals', 'locals', 'vars',
    'getattr', 'setattr', 'delattr', 'hasattr',
    'breakpoint', 'exit', 'quit',
    'memoryview', 'bytearray',
})

# Attribute access patterns that indicate sandbox escape attempts
FORBIDDEN_ATTRIBUTES = frozenset({
    '__class__', '__bases__', '__mro__', '__subclasses__',
    '__globals__', '__code__', '__closure__', '__func__',
    '__self__', '__dict__', '__builtins__', '__import__',
    '__loader__', '__spec__', '__path__', '__file__',
    '__cached__', '__annotations__',
})


class CodeSecurityVisitor(ast.NodeVisitor):
    """AST visitor that checks for dangerous code patterns."""

    def __init__(self):
        self.errors: List[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            module_name = alias.name.split('.')[0]
            if module_name in FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden import: '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            module_name = node.module.split('.')[0]
            if module_name in FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden import: 'from {node.module}'")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        # Check for forbidden built-in calls like eval(), exec(), open()
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_BUILTINS:
                self.errors.append(f"Forbidden function call: '{node.func.id}()'")

        # Check for __import__('os') style calls
        if isinstance(node.func, ast.Name) and node.func.id == '__import__':
            self.errors.append("Forbidden function call: '__import__()'")

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        # Check for direct access to forbidden attributes like obj.__class__
        if node.attr in FORBIDDEN_ATTRIBUTES:
            self.errors.append(f"Forbidden attribute access: '{node.attr}'")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        # Check string literals for dangerous SQL operations
        if isinstance(node.value, str) and len(node.value) > 5:
            match = _FORBIDDEN_SQL_REGEX.search(node.value)
            if match:
                # Extract a snippet for context (first 50 chars)
                snippet = node.value[:50].replace('\n', ' ')
                self.errors.append(
                    f"Forbidden SQL operation '{match.group()}' in string: \"{snippet}...\""
                )
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr):
        # Check f-string parts for dangerous SQL
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                match = _FORBIDDEN_SQL_REGEX.search(part.value)
                if match:
                    snippet = part.value[:50].replace('\n', ' ')
                    self.errors.append(
                        f"Forbidden SQL operation '{match.group()}' in f-string: \"{snippet}...\""
                    )
        self.generic_visit(node)


def validate_python_code(code: str) -> None:
    """
    Validate Python code for security issues using AST analysis.

    Raises:
        UnsafePythonError: If the code contains dangerous constructs.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        # Let syntax errors pass through - they'll fail at exec() time
        # with a more descriptive error
        return

    visitor = CodeSecurityVisitor()
    visitor.visit(tree)

    if visitor.errors:
        raise UnsafePythonError(
            f"Code contains forbidden constructs: {'; '.join(visitor.errors)}"
        )


# =============================================================================
# SQL Query Validation
# =============================================================================

# SQL keywords that indicate write/modify operations
FORBIDDEN_SQL_PATTERNS = [
    r'\bINSERT\b',
    r'\bUPDATE\b',
    r'\bDELETE\b',
    r'\bDROP\b',
    r'\bTRUNCATE\b',
    r'\bALTER\b',
    r'\bCREATE\b',
    r'\bGRANT\b',
    r'\bREVOKE\b',
    r'\bEXEC\b',
    r'\bEXECUTE\b',
    r'\bMERGE\b',
    r'\bCALL\b',
    r'\bREPLACE\b',
    r'\bLOAD\b',
    r'\bINTO\s+OUTFILE\b',
    r'\bINTO\s+DUMPFILE\b',
]

# Pre-compile regex for performance
_FORBIDDEN_SQL_REGEX = re.compile(
    '|'.join(FORBIDDEN_SQL_PATTERNS),
    re.IGNORECASE
)


def validate_sql_query(query: str) -> None:
    """
    Validate SQL query to ensure it's read-only.

    Raises:
        UnsafeSQLError: If the query contains write/modify operations.
    """
    if not isinstance(query, str):
        return

    match = _FORBIDDEN_SQL_REGEX.search(query)
    if match:
        raise UnsafeSQLError(
            f"SQL query contains forbidden operation: '{match.group()}'. "
            "Only SELECT queries are allowed."
        )


class CodeExecutionManager:
    """
    Deprecated shim. Use StreamingCodeExecutor instead.
    Provides only minimal helpers to preserve imports.
    """
    def __init__(self, logger=None, project_manager=None, db=None, report=None, head_completion=None, widget=None, step=None, organization_settings: OrganizationSettingsConfig = None):
        self.logger = logger
        self.organization_settings = organization_settings
        # Other params are ignored; legacy API compatibility only

    async def generate_and_execute_with_retries(self, *args, **kwargs):
        raise RuntimeError("CodeExecutionManager.generate_and_execute_with_retries is deprecated. Use StreamingCodeExecutor.generate_and_execute_stream.")

    def execute_code(self, code: str, db_clients: Dict, excel_files: List):
        executor = StreamingCodeExecutor(organization_settings=self.organization_settings, logger=self.logger)
        return executor.execute_code(code=code, ds_clients=db_clients, excel_files=excel_files)

    def format_df_for_widget(self, df: pd.DataFrame, max_rows: int = 1000) -> Dict:
        executor = StreamingCodeExecutor(organization_settings=self.organization_settings, logger=self.logger)
        return executor.format_df_for_widget(df=df, max_rows=max_rows)


class StreamingCodeExecutor:
    """
    Pure, tool-first streaming executor with retries. No project_manager/DB side-effects.
    """
    def __init__(self, organization_settings: OrganizationSettingsConfig = None, logger=None, context_hub=None):
        self.organization_settings = organization_settings
        self.logger = logger
        self.context_hub = context_hub

    def execute_code(self, *, code: str, ds_clients: Dict, excel_files: List) -> Tuple[pd.DataFrame, str]:
        """Execute Python code and return the resulting DataFrame and captured stdout log.

        Security:
            - Validates Python code via AST analysis before execution
            - Checks all string literals for dangerous SQL operations (INSERT, DELETE, DROP, etc.)

        Raises:
            UnsafePythonError: If code contains forbidden imports, calls, or attributes
            UnsafeSQLError: If code contains SQL strings with write/modify operations
        """
        # Security: Validate Python code and SQL strings before execution
        validate_python_code(code)

        output_log = ""
        local_namespace = {
            'pd': pd,
            'np': np,
            'db_clients': ds_clients,
            'excel_files': excel_files,
        }
        if self.logger:
            self.logger.debug(f"Executing code:\n{code}")
        with io.StringIO() as stdout_capture:
            with redirect_stdout(stdout_capture):
                exec(code, local_namespace)
                generate_df = local_namespace.get('generate_df')
                if not generate_df:
                    raise Exception("No generate_df function found in code")
                df = generate_df(ds_clients, excel_files)
            output_log = stdout_capture.getvalue()
        return df, output_log

    def get_df_info(self, df: pd.DataFrame) -> Dict:
        """Extract comprehensive information from a DataFrame."""
        def convert_to_native(obj):
            if isinstance(obj, (np.int64, np.int32, np.int16, np.int8)):
                return int(obj)
            if isinstance(obj, (np.float64, np.float32, np.float16)):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, (np.datetime64, datetime.datetime, datetime.date)):
                return pd.Timestamp(obj).isoformat()
            if isinstance(obj, pd.Timestamp):
                return obj.isoformat()
            if isinstance(obj, datetime.time):
                return obj.isoformat()
            if isinstance(obj, (datetime.timedelta, pd.Timedelta)):
                return str(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, uuid.UUID):
                return str(obj)
            # Fallback for any other non-JSON-serializable types
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)
        def make_hashable(value: Any) -> Any:
            """
            Convert potentially unhashable values (dict, list, set, ndarray, Timestamp)
            into a hashable representation so nunique/value_counts won't crash.
            """
            try:
                # Fast path: already hashable
                hash(value)
                return value
            except Exception:
                pass
            # Normalize common container types
            if isinstance(value, (pd.Timestamp, datetime.date)):
                return pd.Timestamp(value).isoformat()
            if isinstance(value, np.ndarray):
                return tuple(value.tolist())
            if isinstance(value, (list, tuple)):
                try:
                    return tuple(make_hashable(v) for v in value)
                except Exception:
                    return tuple(str(v) for v in value)
            if isinstance(value, set):
                try:
                    return tuple(sorted(make_hashable(v) for v in value))
                except Exception:
                    return tuple(sorted(str(v) for v in value))
            if isinstance(value, dict):
                try:
                    # Stable, readable representation
                    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                except Exception:
                    # Fallback to tuple of items
                    try:
                        return tuple(sorted((str(k), str(v)) for k, v in value.items()))
                    except Exception:
                        return str(value)
            # Final fallback
            try:
                return str(value)
            except Exception:
                return None

        info_dict = {
            "total_rows": int(len(df)),
            "total_columns": int(len(df.columns)),
            "column_info": {},
            "memory_usage": int(df.memory_usage(deep=True).sum()),
            "dtypes_count": {str(k): int(v) for k, v in df.dtypes.value_counts().items()},
        }
        # describe(include='all') may fail on unhashable objects (e.g., dict cells). Guard it.
        try:
            desc_dict = df.describe(include='all').to_dict()
        except Exception:
            desc_dict = {}
        for column in df.columns:
            column_info = {
                "dtype": str(df[column].dtype),
                "non_null_count": int(df[column].count()),
                "memory_usage": int(df[column].memory_usage(deep=True)),
                "null_count": int(df[column].isna().sum()),
                # nunique may fail for unhashable objects; fall back to a hashable projection
                "unique_count": 0,
            }
            try:
                column_info["unique_count"] = int(df[column].nunique(dropna=True))
            except Exception:
                try:
                    projected = df[column].map(make_hashable)
                    column_info["unique_count"] = int(projected.nunique(dropna=True))
                except Exception:
                    column_info["unique_count"] = 0
            if column in desc_dict:
                try:
                    stats = {stat: convert_to_native(value) for stat, value in desc_dict[column].items() if pd.notna(value)}
                    column_info.update(stats)
                except Exception:
                    # Best-effort; skip stats if conversion fails
                    pass
            info_dict["column_info"][column] = column_info
        return info_dict

    def format_df_for_widget(self, df: pd.DataFrame, max_rows: int = 1000) -> Dict:
        """Format a DataFrame into a widget-compatible structure.
        
        Uses pandas' native JSON serialization which handles datetime, time,
        timedelta, numpy types, NaN/NaT, and other edge cases robustly.
        """
        columns = [{"headerName": str(col), "field": str(col)} for col in df.columns]
        if df.empty:
            rows = []
            df_info = {
                "total_rows": 0,
                "total_columns": int(len(df.columns)),
                "column_info": {str(col): {
                    "dtype": str(df[col].dtype),
                    "non_null_count": 0,
                    "memory_usage": 0,
                    "null_count": 0,
                    "unique_count": 0,
                } for col in df.columns},
                "memory_usage": int(df.memory_usage(deep=True).sum()),
                "dtypes_count": {str(k): int(v) for k, v in df.dtypes.value_counts().items()},
            }
        else:
            # Use pandas' native JSON serialization for robust type handling:
            # - date_format='iso' handles datetime, date, time, Timestamp
            # - default_handler=str catches anything else (UUID, Decimal, etc.)
            rows = json.loads(
                df.head(max_rows).to_json(orient='records', date_format='iso', default_handler=str)
            )
            df_info = self.get_df_info(df)
        return {
            "rows": rows,
            "columns": columns,
            "loadingColumn": False,
            "info": df_info,
        }

    async def generate_and_execute_stream(
        self,
        *,
        data_model: Dict,
        prompt: str,
        schemas: str,
        ds_clients: Dict,
        excel_files: List,
        code_context_builder: 'CodeContextBuilder',
        code_generator_fn: Callable,
        max_retries: int = 2,
        sigkill_event=None,
    ):
        """
        Async generator that yields dict events:
          { "type": "progress"|"stdout", "payload": {...} }
        At the end, returns (df, code, code_and_error_messages, execution_log)
        """
        retries = 0
        code_and_error_messages: List[Tuple[str, str]] = []
        final_code = ""
        exec_df = pd.DataFrame()
        execution_log = ""
        executed_successfully = False
        while retries < max_retries:
            # Cooperative cancellation check at loop start
            if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                break

            yield {"type": "progress", "payload": {"stage": "generating_code", "attempt": retries}}
            try:
                # Cancellation before expensive LLM call
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                final_code = await code_generator_fn(
                    data_model=data_model,
                    prompt=prompt,
                    schemas=schemas,
                    ds_clients=ds_clients,
                    excel_files=excel_files,
                    code_and_error_messages=code_and_error_messages,
                    memories="",
                    previous_messages="",
                    retries=retries,
                    prev_data_model_code_pair=None,
                    sigkill_event=sigkill_event,
                    code_context_builder=code_context_builder,
                )
                yield {"type": "progress", "payload": {"stage": "generated_code", "attempt": retries}}
            except Exception as e:
                msg = f"Code generation error: {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries}}
                continue

            # Executing code
            yield {"type": "progress", "payload": {"stage": "executing_code", "attempt": retries}}
            try:
                # Cancellation before executing user code
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                exec_df, execution_log = self.execute_code(code=final_code, ds_clients=ds_clients, excel_files=excel_files)
                executed_successfully = True
                break
            except Exception as e:
                import traceback
                trace = traceback.format_exc()
                msg = f"Execution error: {str(e)}\n{trace}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries}}
                continue

        # If cancelled, emit a final done with empty results to let caller stop cleanly
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            yield {
                "type": "done",
                "payload": {
                    "df": pd.DataFrame(),
                    "code": final_code,
                    "errors": code_and_error_messages,
                    "execution_log": execution_log,
                },
            }
            return
        else:
            # If we never executed successfully (e.g., validation failed up to max retries),
            # signal failure by returning df=None so callers can treat as error.
            if not executed_successfully and code_and_error_messages:
                yield {
                    "type": "done",
                    "payload": {
                        "df": None,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                    },
                }
            else:
                # Emit a final done event carrying the results instead of returning values
                yield {
                    "type": "done",
                    "payload": {
                        "df": exec_df,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                    },
                }

    async def generate_and_execute_stream_v2(
        self,
        *,
        request: CodeGenRequest,
        ds_clients: Dict,
        excel_files: List,
        code_context_builder: Optional['CodeContextBuilder'] = None,
        code_generator_fn: Callable = None,
        sigkill_event=None,
    ):
        """
        V2: Typed context-based generator. Yields the same event shapes as v1.
        """
        retries = 0
        max_retries = int(getattr(request, "retries", 2) or 2)
        code_and_error_messages: List[Tuple[str, str]] = []
        final_code = ""
        exec_df = pd.DataFrame()
        execution_log = ""
        executed_successfully = False
        ctx: CodeGenContext = request.context
        # Derive prompt/schemas for legacy generator signature
        derived_prompt = ctx.user_prompt
        derived_interpreted_prompt = ctx.interpreted_prompt
        derived_schemas = ctx.schemas_excerpt

        while retries < max_retries:
            if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                break
            yield {"type": "progress", "payload": {"stage": "generating_code", "attempt": retries}}
            try:
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                # Call code generator with typed context and legacy params populated from context
                final_code = await code_generator_fn(
                    data_model={},
                    prompt=derived_prompt,
                    interpreted_prompt=derived_interpreted_prompt,
                    schemas=derived_schemas,
                    ds_clients=ds_clients,
                    excel_files=excel_files,
                    code_and_error_messages=code_and_error_messages,
                    memories="",
                    previous_messages="",
                    retries=retries,
                    prev_data_model_code_pair=None,
                    sigkill_event=sigkill_event,
                    code_context_builder=None,
                    context=ctx,
                )
                yield {"type": "progress", "payload": {"stage": "generated_code", "attempt": retries}}
            except Exception as e:
                msg = f"Code generation error: {str(e)}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries}}
                continue

            yield {"type": "progress", "payload": {"stage": "executing_code", "attempt": retries}}
            try:
                if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
                    break
                exec_df, execution_log = self.execute_code(code=final_code, ds_clients=ds_clients, excel_files=excel_files)
                executed_successfully = True
                break
            except Exception as e:
                import traceback
                trace = traceback.format_exc()
                msg = f"Execution error: {str(e)}\n{trace}"
                code_and_error_messages.append((final_code, msg))
                yield {"type": "stdout", "payload": msg}
                retries += 1
                if retries < max_retries:
                    yield {"type": "progress", "payload": {"stage": "retry", "attempt": retries}}
                continue

        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            yield {
                "type": "done",
                "payload": {
                    "df": pd.DataFrame(),
                    "code": final_code,
                    "errors": code_and_error_messages,
                    "execution_log": execution_log,
                },
            }
            return
        else:
            if not executed_successfully and code_and_error_messages:
                yield {
                    "type": "done",
                    "payload": {
                        "df": None,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                    },
                }
            else:
                yield {
                    "type": "done",
                    "payload": {
                        "df": exec_df,
                        "code": final_code,
                        "errors": code_and_error_messages,
                        "execution_log": execution_log,
                    },
                }

    async def execute_and_update_step(self,
                              data_model: Dict,
                              code_generator_fn: Callable,
                              db_clients: Dict = None,
                              excel_files: List = None,
                              step=None,  # Optional override for current step
                              **generator_kwargs) -> bool:
        """
        Execute code generation/execution process and update the step with results

        Args:
            data_model: The data model to generate code for
            code_generator_fn: Function that generates code
            db_clients: Database clients
            excel_files: Excel files
            step: Override for the step object (uses self.step if None)
            **generator_kwargs: Additional arguments to pass to code_generator_fn

        Returns:
            Boolean indicating if execution was successful
        """
        # Use provided step or fall back to instance step
        current_step = step or self.step
        if not current_step:
            if self.logger:
                self.logger.error("No step provided for execute_and_update_step")
            return False

        df, final_code, code_and_error_messages = await self.generate_and_execute_with_retries(
            data_model=data_model,
            code_generator_fn=code_generator_fn,
            db_clients=db_clients,
            excel_files=excel_files,
            step=current_step,
            max_retries=self.organization_settings.get_config("limit_code_retries").value,
            **generator_kwargs
        )
        
        # Check if the DataFrame has columns, which indicates success even if empty
        if len(df.columns) > 0:
            # Format the data for widget display
            widget_data = self.format_df_for_widget(df)
            
            # Update step with data
            try:
                await self.project_manager.update_step_with_data(self.db, current_step, widget_data)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error updating step with data: {str(e)}")
                return False
            return True
        else:
            # Handle error case if all retries failed and we have no columns
            try:
                if self.report and self.head_completion and self.widget:
                    await self.project_manager.create_message(
                        report=self.report,
                        db=self.db,
                        message="I faced some issues while generating data. The result had no columns. Can you try explaining again?",
                        status="success",
                        completion=self.head_completion,
                        widget=self.widget,
                        role="ai_agent"
                    )
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error creating error message: {str(e)}")
            return False