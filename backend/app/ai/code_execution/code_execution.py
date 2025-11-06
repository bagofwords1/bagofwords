import io
import sys
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
        """Execute Python code and return the resulting DataFrame and captured stdout log."""
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
            if isinstance(obj, (np.datetime64, datetime.date)):
                return pd.Timestamp(obj).isoformat()
            if isinstance(obj, pd.Timestamp):
                return obj.isoformat()
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return obj
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

    def postprocess_df(self, widget: Dict) -> Dict:
        """Clean and format DataFrame data for widget display."""
        def clean_value(value):
            if isinstance(value, (pd.Timestamp, datetime.date)):
                return value.isoformat()
            elif isinstance(value, uuid.UUID):
                return str(value)
            elif pd.isna(value):
                return None
            return value

        if 'rows' in widget:
            widget['rows'] = [{k: clean_value(v) for k, v in row.items()} for row in widget['rows']]
        return widget

    def format_df_for_widget(self, df: pd.DataFrame, max_rows: int = 1000) -> Dict:
        """Format a DataFrame into a widget-compatible structure."""
        columns = [{"headerName": col, "field": col} for col in df.columns]
        if df.empty:
            rows = []
            df_info = {
                "total_rows": 0,
                "total_columns": int(len(df.columns)),
                "column_info": {col: {
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
            rows = df.to_dict(orient='records')[:max_rows]
            df_info = self.get_df_info(df)
        widget = {
            "rows": rows,
            "columns": columns,
            "loadingColumn": False,
            "info": df_info,
        }
        return self.postprocess_df(widget)

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
        validator_fn: Optional[Callable] = None,
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

            # Optional validation
            if validator_fn:
                try:
                    yield {"type": "progress", "payload": {"stage": "validating_code", "attempt": retries}}
                    validation = await validator_fn(final_code, data_model)
                    
                    if not validation.get("valid", True):
                        error_msg = validation.get('reasoning', 'Validation failed')
                        if self.logger:
                            self.logger.warning(f"Validation failed (attempt {retries+1}/{max_retries}): {error_msg}")
                        # Create validation failed message
                        yield {"type":"progress", "payload": {"stage": "validated_code", "valid": False, "error": error_msg, "attempt": retries}}
                        code_and_error_messages.append((final_code, error_msg))
                        retries += 1
                        if retries < max_retries:
                            yield {"type": "progress", "payload": {"stage": "validating_code.retry", "attempt": retries}}
                        continue
                    else:
                        # Validation succeeded; emit event and proceed to execution without looping
                        yield {"type": "progress", "payload": {"stage": "validated_code", "valid": True, "attempt": retries}}
                        # Do not continue; fall through to execution stage below
                except Exception as e:
                    msg = f"Validation error: {str(e)}"
                    code_and_error_messages.append((final_code, msg))
                    yield {"type": "stdout", "payload": msg}
                    retries += 1
                    if retries < max_retries:
                        yield {"type": "progress", "payload": {"stage": "validating_code.retry", "attempt": retries}}
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
        validator_fn: Optional[Callable] = None,
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

            if validator_fn:
                try:
                    yield {"type": "progress", "payload": {"stage": "validating_code", "attempt": retries}}
                    validation = await validator_fn(final_code, {})
                    if not validation.get("valid", True):
                        error_msg = validation.get('reasoning', 'Validation failed')
                        yield {"type": "progress", "payload": {"stage": "validated_code", "valid": False, "error": error_msg, "attempt": retries}}
                        code_and_error_messages.append((final_code, error_msg))
                        retries += 1
                        if retries < max_retries:
                            yield {"type": "progress", "payload": {"stage": "validating_code.retry", "attempt": retries}}
                        continue
                    else:
                        yield {"type": "progress", "payload": {"stage": "validated_code", "valid": True, "attempt": retries}}
                except Exception as e:
                    msg = f"Validation error: {str(e)}"
                    code_and_error_messages.append((final_code, msg))
                    yield {"type": "stdout", "payload": msg}
                    retries += 1
                    if retries < max_retries:
                        yield {"type": "progress", "payload": {"stage": "validating_code.retry", "attempt": retries}}
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
                              validator_fn: Optional[Callable] = None,
                              db_clients: Dict = None,
                              excel_files: List = None,
                              step=None,  # Optional override for current step
                              **generator_kwargs) -> bool:
        """
        Execute code generation/validation/execution process and update the step with results
        
        Args:
            data_model: The data model to generate code for
            code_generator_fn: Function that generates code
            validator_fn: Optional function to validate code
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
            validator_fn=validator_fn,
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