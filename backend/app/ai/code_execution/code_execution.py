import io
import sys
import pandas as pd
import numpy as np
import datetime
import uuid
from contextlib import redirect_stdout
from typing import Dict, Any, Tuple, List, Optional, Callable, Coroutine

class CodeExecutionManager:
    """
    Manages the entire code generation, validation, and execution process with retries.
    """
    
    def __init__(self, logger=None, project_manager=None, db=None, report=None, head_completion=None, widget=None, step=None, organization_settings=None):
        """
        Initialize the CodeExecutionManager with all required dependencies.
        
        Args:
            logger: Logger instance
            project_manager: ProjectManager instance (required)
            db: Database session (required)
            report: Current report object
            head_completion: Parent completion object
            widget: Current widget object
            step: Current step object
        """
        self.logger = logger
        
        # Validate essential dependencies
        if not project_manager:
            raise ValueError("project_manager is required for CodeExecutionManager")
        if not db:
            raise ValueError("db is required for CodeExecutionManager")
            
        self.project_manager = project_manager
        self.db = db
        self.report = report
        self.head_completion = head_completion
        self.widget = widget
        self.step = step
        self.organization_settings = organization_settings

    async def generate_and_execute_with_retries(self, 
                                         data_model: Dict,
                                         code_generator_fn: Callable,
                                         validator_fn: Optional[Callable] = None,
                                         max_retries: int = 3,
                                         db_clients: Dict = None, 
                                         excel_files: List = None,
                                         step=None,  # Optional override for current step
                                         **generator_kwargs) -> Tuple[pd.DataFrame, str, List]:
        """
        Comprehensive function that handles:
        1. Code generation 
        2. Code validation
        3. Code execution
        4. Retries with error feedback
        
        Args:
            data_model: The data model to generate code for
            code_generator_fn: Async function that generates code
            validator_fn: Optional async function to validate code
            max_retries: Maximum number of retry attempts
            db_clients: Database clients
            excel_files: Excel files
            step: Override for the step object (uses self.step if None)
            **generator_kwargs: Additional arguments to pass to code_generator_fn
            
        Returns:
            Tuple containing (dataframe, final_code, error_messages)
        """
        retries = 0
        code_and_error_messages = []
        df = pd.DataFrame()
        output_log = ""
        code = ""
        
        # Use provided step or fall back to instance step
        current_step = step or self.step
        
        while retries < max_retries:
            try:
                # Generate code
                code = await code_generator_fn(
                    data_model=data_model,
                    code_and_error_messages=code_and_error_messages,
                    retries=retries,
                    excel_files=excel_files,
                    **generator_kwargs
                )
                
                # Validate if enabled
                if validator_fn:
                    validation_result = await validator_fn(code, data_model)
                    if validation_result.get('valid') is False:
                        error_msg = validation_result.get('reasoning', 'Validation failed')
                        if self.logger:
                            self.logger.warning(f"Validation failed (attempt {retries+1}/{max_retries}): {error_msg}")
                        
                        # Create validation failed message
                        try:
                            await self.project_manager.create_message(
                                report=self.report,
                                db=self.db,
                                message=f"Validation failed: {error_msg}",
                                completion=self.head_completion,
                                widget=self.widget,
                                role="ai_agent"
                            )
                        except Exception as msg_error:
                            if self.logger:
                                self.logger.error(f"Error creating validation message: {str(msg_error)}")
                    
                        code_and_error_messages.append((code, error_msg))
                        retries += 1
                        continue
                    elif validation_result.get('valid') is True:
                        try:
                            await self.project_manager.create_message(
                                report=self.report,
                                db=self.db,
                                message=f"Validation passed",
                                completion=self.head_completion,
                                widget=self.widget,
                                role="ai_agent"
                            )
                        except Exception as msg_error:
                            if self.logger:
                                self.logger.error(f"Error creating validation success message: {str(msg_error)}")

                # Execute code
                df, output_log = self._execute_code(code, db_clients, excel_files)
                
                # Check if the DataFrame has columns (even if empty)
                if len(df.columns) == 0:
                    raise Exception("Generated DataFrame has no columns")
                
                # Update step with code
                if current_step:
                    try:
                        await self.project_manager.update_step_with_code(self.db, current_step, code)
                    except Exception as step_error:
                        if self.logger:
                            self.logger.error(f"Error updating step with code: {str(step_error)}")
                
                # If we got here without exceptions, we succeeded
                if current_step:
                    try:
                        await self.project_manager.update_step_status(self.db, current_step, "success")
                    except Exception as status_error:
                        if self.logger:
                            self.logger.error(f"Error updating step status: {str(status_error)}")
                
                return df, code, code_and_error_messages
                
            except Exception as e:
                import traceback
                trace = traceback.format_exc()
                error_msg = f"{output_log}\n\nError in execution: {str(e)}\n{trace}"
                if self.logger:
                    self.logger.error(f"Code execution failed (attempt {retries+1}/{max_retries}): {error_msg}")
                
                # Create error message
                await self.project_manager.create_message(
                    report=self.report,
                    db=self.db,
                    message=f"Self-healing and optimizing code (attempt {retries+1}/{max_retries}): {str(e)}",
                    completion=self.head_completion,
                    widget=self.widget,
                    role="ai_agent"
                )
            
                code_and_error_messages.append((code, error_msg))
                retries += 1
                
                if retries >= max_retries:
                    # Update step status to error if all retries failed
                    if current_step:
                        try:
                            await self.project_manager.update_step_status(self.db, current_step, "error")
                        except Exception as status_error:
                            if self.logger:
                                self.logger.error(f"Error updating step status: {str(status_error)}")
                    
                    # Return an empty DataFrame with no columns to indicate failure
                    return pd.DataFrame(), code, code_and_error_messages
        
        # This should be unreachable but included as a fallback
        return df, code, code_and_error_messages

    def _execute_code(self, code: str, db_clients: Dict, excel_files: List) -> Tuple[pd.DataFrame, str]:
        """Execute Python code and return the resulting DataFrame and output log"""
        output_log = ""
        
        local_namespace = {
            'pd': pd, 
            'np': np,
            'db_clients': db_clients, 
            'excel_files': excel_files
        }
        
        if self.logger:
            self.logger.debug(f"Executing code:\n{code}")
            
        # Capture stdout during the entire execution process
        with io.StringIO() as stdout_capture:
            with redirect_stdout(stdout_capture):
                exec(code, local_namespace)
                
                generate_df = local_namespace.get('generate_df')
                if not generate_df:
                    raise Exception("No generate_df function found in code")
                    
                df = generate_df(db_clients, excel_files)
                
            # Get all the captured output
            output_log = stdout_capture.getvalue()
            
        return df, output_log

    def get_df_info(self, df: pd.DataFrame) -> Dict:
        """Extract comprehensive information from a DataFrame"""
        # Convert NumPy types to Python native types
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

        info_dict = {
            "total_rows": int(len(df)),
            "total_columns": int(len(df.columns)),
            "column_info": {},
            "memory_usage": int(df.memory_usage(deep=True).sum()),
            "dtypes_count": {str(k): int(v) for k, v in df.dtypes.value_counts().items()}
        }
        
        # Get statistical description for all types
        desc_dict = df.describe(include='all').to_dict()
        
        # Parse column information
        for column in df.columns:
            column_info = {
                "dtype": str(df[column].dtype),
                "non_null_count": int(df[column].count()),
                "memory_usage": int(df[column].memory_usage(deep=True)),
                "null_count": int(df[column].isna().sum()),
                "unique_count": int(df[column].nunique()),
            }
            
            # Merge statistical description if available
            if column in desc_dict:
                stats = {
                    stat: convert_to_native(value)
                    for stat, value in desc_dict[column].items()
                    if pd.notna(value)
                }
                column_info.update(stats)
            
            info_dict["column_info"][column] = column_info
        
        return info_dict

    def postprocess_df(self, widget: Dict) -> Dict:
        """Clean and format DataFrame data for widget display"""
        def clean_value(value):
            if isinstance(value, (pd.Timestamp, datetime.date)):
                return value.isoformat()
            elif isinstance(value, uuid.UUID):
                return str(value)
            elif pd.isna(value):
                return None
            return value

        if 'rows' in widget:
            widget['rows'] = [{k: clean_value(v) for k, v in row.items()} 
                             for row in widget['rows']]
        return widget

    def format_df_for_widget(self, df: pd.DataFrame, max_rows: int = 1000) -> Dict:
        """Format a DataFrame into a widget-compatible structure"""
        if df.empty:
            columns = []
            rows = []
            df_info = {}
        else:
            columns = [{"headerName": col, "field": col} for col in df.columns]
            rows = df.to_dict(orient='records')[:max_rows]
            df_info = self.get_df_info(df)
        
        widget = {
            "rows": rows,
            "columns": columns,
            "loadingColumn": False,
            "info": df_info,
        }
        
        return self.postprocess_df(widget)

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
            max_retries=self.organization_settings.get("limit_code_retries", {}).get("value", 3),
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
                        completion=self.head_completion,
                        widget=self.widget,
                        role="ai_agent"
                    )
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error creating error message: {str(e)}")
            return False