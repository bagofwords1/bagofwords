import datetime
import json
import pandas as pd
import numpy as np
from functools import wraps
import re
from partialjson.json_parser import JSONParser
import uuid
import io
import sys
from contextlib import redirect_stdout

from .llm.llm import LLM
from app.ai.agents.planner import Planner
from app.ai.agents.answer import Answer
from app.ai.agents.dashboard_designer import DashboardDesigner

from app.ai.agents.excel import ExcelAgent
from app.models.completion import Completion
from app.models.mention import Mention, MentionType
from app.models.step import Step
from app.models.memory import Memory
from app.models.widget import Widget
from app.project_manager import ProjectManager
from app.ai.agents.coder.coder import Coder
from app.ai.agents.reporter.reporter import Reporter

from sqlalchemy import select


class Agent:

    def __init__(self, db=None, report=None, model=None, head_completion=None, widget=None, step=None, messages=[], main_router="table"):

        self.llm = LLM(model=model)

        self.planner = Planner(model=model)
        self.answer = Answer(model=model)
        self.dashboard_designer = DashboardDesigner(model=model)
        self.project_manager = ProjectManager()
        self.coder = Coder(model=model)
        self.reporter = Reporter(model=model)

        if db:
            self.db = db

        self.persona = "Financial Analyst"
        self.main_router = main_router
        self.widget = None
        self.report = None
        self.step = None
        self.memory_mentions = []
        self.file_mentions = []
        self.data_source_mentions = []

        if head_completion:
            self.head_completion = head_completion

            if widget:
                self.widget = widget
                self.step = step

        if report:
            self.report = report

            self.data_sources = self.report.data_sources
            self.clients = { data_source.name: data_source.get_client() for data_source in self.data_sources }
            self.files = self.report.files

            # should be last

    async def main_execution(self):

        try:
            results = []
            action_results = {}
            dashboard_widgets = {
                "text_widgets": [],
                "widgets": []
            }
            schemas = await self._build_schemas_context()
            memories = await self._build_memories_context()
            previous_messages = await self._build_messages_context()
            head_completion = self.head_completion

            async for json_result in self.planner.execute(schemas, self.persona, head_completion.prompt, memories, previous_messages, self.widget, self.step):
                if not json_result or 'plan' not in json_result:
                    continue

                if not isinstance(json_result['plan'], list):
                    continue

                # Create action_results entries for any new actions
                for i, action in enumerate(json_result['plan']):
                    if not isinstance(action, dict) or 'action' not in action:
                        continue

                    action_id = f"action_{i}"
                    if action_id not in action_results:
                        action_results[action_id] = {
                            "prefix_completion": None,
                            "widget": None,
                            "step": None,
                            "completed": False
                        }

                # Process each action in the plan
                for i, action in enumerate(json_result['plan']):
                    if not isinstance(action, dict):
                        continue

                    action_id = f"action_{i}"

                    # Skip if action is already completed
                    if action_results[action_id]["completed"]:
                        continue

                    # Safely check for action type
                    action_type = action.get('action')
                    if not action_type:
                        continue

                    # Handle different action types
                    if action_type == 'create_widget':
                        if action_results[action_id]['widget'] is None and action['details'].get('title'):
                            widget = await self.project_manager.create_widget(
                                self.db,
                                self.report,
                                action['details']['title']
                            )
                            action_results[action_id]['widget'] = widget
                        
                            await self.project_manager.update_completion_with_widget(
                                self.db,
                                action_results[action_id]['prefix_completion'],
                                action_results[action_id]['widget']
                            )

                        if action_results[action_id]['step'] is None and action['details'].get('title'):
                            step = await self.project_manager.create_step(
                                self.db,
                                action['details']['title'],
                                action_results[action_id]['widget'],
                                "table"
                            )
                            action_results[action_id]['step'] = step
                            await self.project_manager.update_completion_with_step(
                                self.db,
                                action_results[action_id]['prefix_completion'],
                                action_results[action_id]['step']
                            )

                        if action_results[action_id]['widget'] is not None and action_results[action_id]['step'] is not None:
                            action_completed = await self._handle_generate_widget_data(
                                head_completion.prompt,
                                action,
                                action_results[action_id]['widget'],
                                action_results[action_id]['step']
                            )
                            if action_completed:
                                action_results[action_id]['completed'] = True

                    elif action_type == 'modify_widget':
                        # Add this block before attempting updates
                        if action_results[action_id]['prefix_completion'] is None:
                            completion = await self.project_manager.create_message(
                                report=self.report,
                                db=self.db,
                                message=action['prefix'],
                                completion=self.head_completion,
                                widget=self.widget,
                                role="system"
                            )
                            action_results[action_id]['prefix_completion'] = completion

                        # Get widget data using modify supertable
                        if not self.widget:
                            continue

                        if action.get('action_end', True): 
                            action_completed = await self._handle_modify_widget(
                                head_completion.prompt,
                                self.widget,
                                action
                            )
                        else:
                            continue

                        if action_completed:
                            last_step = await self._get_last_step()
                            action_results[action_id]['widget'] = self.widget
                            action_results[action_id]['step'] = last_step
                            action_results[action_id]['completed'] = True

                            await self.project_manager.update_completion_with_widget(
                                self.db,
                                action_results[action_id]['prefix_completion'],
                                action_results[action_id]['widget']
                            )
                            await self.project_manager.update_completion_with_step(
                                self.db,
                                action_results[action_id]['prefix_completion'],
                                action_results[action_id]['step']
                            )
                        
                    elif action_type == 'answer_question':
                        if not action['details'].get('extracted_question'):
                            continue
                        
                        # Create prefix completion if it doesn't exist (following pattern from other actions)
                        if action_results[action_id]['prefix_completion'] is None:
                            completion = await self.project_manager.create_message(
                            report=self.report,
                                db=self.db,
                                message=action['prefix'],
                                completion=self.head_completion,
                                widget=None,
                                role="system"
                            )
                            action_results[action_id]['prefix_completion'] = completion

                        try:
                            question = action['details']['extracted_question']
                            full_answer = action['prefix'] + " "

                            async for chunk in self.answer.execute(
                                prompt=self.head_completion.prompt,
                                schemas=schemas,
                                memories=memories,
                                previous_messages=previous_messages,
                                widget=self.widget,
                            ):
                                full_answer += chunk
                                # Update message with progress
                                await self.project_manager.update_message(
                                    self.db,
                                    action_results[action_id]['prefix_completion'],
                                    full_answer
                                )

                            # Mark as completed only if we get here without errors
                            action_results[action_id].update({
                                "answer": full_answer,
                                "completed": True
                            })

                        except Exception as e:
                            await self.project_manager.create_message(
                                report=self.report,
                                db=self.db,
                                message=f"I encountered an error while answering: {str(e)}",
                                completion=self.head_completion,
                                widget=self.widget,
                                role="ai_agent"
                            )
                            # Don't mark as completed if there was an error
                            continue

                    elif action_type == 'design_dashboard':
                        widgets = [x['widget']
                                for x in action_results.values() if x['widget'] is not None]
                        steps = [x['step']
                                for x in action_results.values() if x['step'] is not None]
                        if not widgets or not steps:
                            print("design_dashboard action has no widgets or steps")
                            continue

                        if action_results[action_id]['prefix_completion'] is None:
                            dashboard_completion = await self.project_manager.create_message(
                                report=self.report,
                                db=self.db,
                                message="Now putting it together and creating a dashboard..",
                                completion=self.head_completion,
                                widget=self.widget,
                                role="system"
                            )
                            action_results[action_id]['prefix_completion'] = dashboard_completion

                        async for dashboard_design in self.dashboard_designer.execute(
                            prompt=head_completion.prompt,
                            widgets=widgets,
                            steps=steps,
                            previous_messages=previous_messages
                        ):

                            if dashboard_design.get('widgets'):
                                for widget in dashboard_design['widgets']:
                                    if not widget['id'] in [x['id'] for x in dashboard_widgets['widgets']]:
                                        dashboard_widgets['widgets'].append(widget)
                                        await self.project_manager.update_widget_position_and_size(
                                            self.db,
                                            widget['id'],
                                            widget['x'],
                                            widget['y'],
                                            widget['width'],
                                            widget['height']
                                        )
                            # Handle each dashboard design update
                            if dashboard_design.get('text_widgets'):
                                for i, text_widget in enumerate(dashboard_design['text_widgets']):
                                    if i not in [x.get('index') for x in dashboard_widgets['text_widgets']]:
                                        # Add index to track the text widget
                                        text_widget['index'] = i
                                        dashboard_widgets['text_widgets'].append(
                                            text_widget)
                                        await self.project_manager.create_text_widget(
                                            self.db,
                                            text_widget['content'],
                                            text_widget['x'],
                                            text_widget['y'],
                                            text_widget['width'],
                                            text_widget['height'],
                                            self.report.id
                                        )
                            
                        await self.project_manager.update_message(
                            self.db,
                            action_results[action_id]['prefix_completion'],
                            "Now putting it together and creating a dashboard.. and it's ready!"
                        )

                        action_results[action_id] = {
                            "dashboard_design": dashboard_design,
                            "completed": True
                        }
                                # Create message for action prefix if it exists and has changed
                    if action.get('prefix'):
                        # Skip if this is a completed answer action
                        if action.get('action') == 'answer_question' and action_results[action_id].get('completed'):
                            continue
                            
                        if action_results[action_id]['prefix_completion'] is None:
                            completion = await self.project_manager.create_message(
                                report=self.report,
                                db=self.db,
                                message=action['prefix'],
                                completion=self.head_completion,
                                widget=self.widget,
                                role="system"
                            )
                            action_results[action_id]['prefix_completion'] = completion
                        elif action_results[action_id]['prefix_completion'].completion.get('content') != action['prefix']:
                            completion_obj = await self.project_manager.update_message(
                                self.db,
                                action_results[action_id]['prefix_completion'],
                                action['prefix']
                            )
                            action_results[action_id]['prefix_completion'] = completion_obj



            first_completion = await self.db.execute(select(Completion).filter(Completion.report_id == self.report.id).order_by(Completion.created_at.asc()).limit(1))
            first_completion = first_completion.scalar_one_or_none()

            if self.head_completion.id == first_completion.id:
                title = await self.reporter.generate_report_title(previous_messages, json_result['plan'])
                await self.project_manager.update_report_title(self.db, self.report, title)
            # Return all results at once
            plan_json = { "plan": json_result['plan'] }
            plan_json = json.dumps(plan_json)
            plan = await self.project_manager.create_plan(self.db, self.report, plan_json, self.head_completion)

            return action_results

        except Exception as e:
            error = await self.project_manager.create_error_completion(self.db, self.head_completion, str(e))
            print(f"Error in main_execution: {e}")
            raise e

    async def _handle_generate_widget_data(self, prompt, action, widget, step):
        # First condition - save data model as soon as it's available
        if action['details'].get('data_model') and action['details']['data_model'].get('columns'):
            data_model = action['details']['data_model']

            step.data_model = data_model
            self.db.add(step)
            await self.db.commit()
            await self.db.refresh(step)
        # Check if data model is complete before starting code generation
        if not action.get('action_end', False):
            return False

        code_and_error_messages = []
        retries = 0
        schemas = await self._build_schemas_context()
        memories = await self._build_memories_context()
        previous_messages = await self._build_messages_context()
        code = ""

        # Start code generation and execution only when data model is complete
        while retries < 3:
            try:
                # Generate code using the data model if this is first try or previous attempt failed
                code = await self.coder.data_model_to_code(
                    data_model, 
                    prompt=prompt, 
                    schemas=schemas, 
                    ds_clients=self.clients, 
                    excel_files=self.files,
                    code_and_error_messages=code_and_error_messages,
                    memories=memories,
                    previous_messages=previous_messages,
                    retries=retries,
                    prev_data_model_code_pair=None
                )
                # Execute the generated code
                df = self.execute_code_and_return_df(code)
                if df is not None and not df.empty:
                    await self.project_manager.update_step_status(self.db, step, "success")
                    break

            except Exception as e:
                retries += 1
                await self.project_manager.create_message(
                    report=self.report,
                    db=self.db,
                    message=f"Self-healing and optimizing code (attempt {
                        retries}/3)",
                    completion=self.head_completion,
                    widget=self.widget,
                    role="ai_agent")

                print(f"Retry: {retries}, Error: {e}")
                code_and_error_messages.append((code, str(e)))

                if retries >= 3:
                    await self.project_manager.update_step_status(self.db, step, "error")
                    await self.project_manager.create_message(
                        report=self.report,
                        db=self.db,
                        message="I faced some issues while generating data. Can you try explaining again? Error: " + str(e),
                        completion=self.head_completion,
                        widget=self.widget,
                        role="ai_agent"
                    )
                    df = pd.DataFrame()  # empty dataframe
                    continue
                

        # Formatting
        columns = [{"headerName": col, "field": col} for col in df.columns]
        rows = df.to_dict(orient='records')

        widget = {"rows": rows[:1000],
                  "columns": columns, "loadingColumn": False}
        cleaned_data = self.postprocess_df(widget)
        await self.project_manager.update_step_with_code(self.db, step, code)
        await self.project_manager.update_step_with_data(self.db, step, cleaned_data)
        

        return True

    async def _handle_modify_widget(self, prompt, widget, action):
        if not action.get('action_end', False):
            return False
        
        data_model_diff = action['details']
        code_and_error_messages = []
        retries = 0
        schemas = await self._build_schemas_context()
        memories = await self._build_memories_context()
        previous_messages = await self._build_messages_context()
        previous_step = await self._get_last_step()

        step = await self.project_manager.create_step(self.db, 
                                                      widget.title, 
                                                      widget, 
                                                      "table")
        
        updated_data_model = await self._apply_data_model_diff(previous_step.data_model, data_model_diff)

        step.data_model = updated_data_model

        prev_data_model_code_pair = {
            'data_model': previous_step.data_model,
            'code': previous_step.code
        }

        while retries < 3:
            try:
                # Generate code using the data model if this is first try or previous attempt failed
                code = await self.coder.data_model_to_code(
                    updated_data_model, 
                    prompt=prompt, 
                    schemas=schemas, 
                    ds_clients=self.clients, 
                    excel_files=self.files,
                    code_and_error_messages=code_and_error_messages,
                    memories=memories,
                    previous_messages=previous_messages,
                    retries=retries,
                    prev_data_model_code_pair=prev_data_model_code_pair
                )
                # Execute the generated code
                df = self.execute_code_and_return_df(code)
                if df is not None and not df.empty:
                    await self.project_manager.update_step_status(self.db, step, "success")
                    break

            except Exception as e:
                retries += 1
                await self.project_manager.create_message(
                    report=self.report,
                    db=self.db,
                    message=f"Self-healing and optimizing code (attempt {
                        retries}/3)",
                    completion=self.head_completion,
                    widget=self.widget,
                    role="ai_agent")

                print(f"Retry: {retries}, Error: {e}")
                code_and_error_messages.append((code, str(e)))

                if retries >= 3:
                    await self.project_manager.create_message(
                        report=self.report,
                        db=self.db,
                        message="Failed to generate data after 3 retries.",
                        completion=self.head_completion,
                        widget=self.widget,
                        role="ai_agent"
                    )
                    await self.project_manager.update_step_status(self.db, step, "error")
                    df = pd.DataFrame()  # empty dataframe
                    continue
                

        # Formatting
        columns = [{"headerName": col, "field": col} for col in df.columns]
        rows = df.to_dict(orient='records')

        widget = {"rows": rows[:1000], "columns": columns, "loadingColumn": False}
        cleaned_data = self.postprocess_df(widget)
        await self.project_manager.update_step_with_code(self.db, step, code)
        await self.project_manager.update_step_with_data(self.db, step, cleaned_data)

        return True

    def execute_code_and_return_df(self, code):
        db_clients = self.clients
        excel_files = self.files
        stdout_capture = io.StringIO()
        output_log = ""
        
        try:
            generate_df = self._code_runner(code, db_clients, excel_files)
            if generate_df:
                try:
                    with redirect_stdout(stdout_capture):
                        df = generate_df(db_clients, excel_files)
                    output_log = stdout_capture.getvalue()
                    print(f"Code execution output: {output_log}")  # Log captured output
                    return df
                except Exception as e:
                    import traceback
                    trace = traceback.format_exc()
                    print(f"Error executing code: {e}")
                    print(f"Traceback: {trace}")
                    print(f"Code execution output before error: {output_log}")
                    raise Exception(f"Error executing code: {e}\nTraceback: {trace}\nExecution output: {output_log}")
            else:
                raise Exception("No generate_df function found in code")
        except Exception as e:
            import traceback
            trace = traceback.format_exc()
            print(f"Error in code runner: {e}")
            print(f"Traceback: {trace}")
            print(f"Code execution output before error: {output_log}")
            raise Exception(f"Error in code runner: {e}\nTraceback: {trace}\nExecution output: {output_log}")

    def _format_df(self, df):
        columns = [{"headerName": col, "field": col} for col in df.columns]
        rows = df.to_dict(orient='records')

        widget = {"rows": rows[:1000],
                  "columns": columns, "loadingColumn": False}
        cleaned_data = self.postprocess_df(widget)

        return cleaned_data

#
    def _code_runner(self, code, db_clients, excel_files):
        print(code)
        local_namespace = {'pd': pd, 'np': np,
                           'db_clients': db_clients, 'excel_files': excel_files}
        exec(code, local_namespace)

        generate_df = local_namespace.get('generate_df')
        if generate_df:
            return generate_df
        else:
            return None

    def validate_and_clean_code(self, code):
        # Replace triple single quotes with escaped double quotes
        code = code.replace("'''", '"""')
        # Escape backslashes and newlines if necessary
        code = code.replace("\\n", "\\\\n").replace("\\", "\\\\")

        # strip leading and trailing whitespace
        code = code.strip()

        return code

    def clean_data(self, data):
        if isinstance(data, dict):
            return {k: self.clean_data(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.clean_data(item) for item in data]
        # Handle both pd.Timestamp and datetime.date
        elif isinstance(data, (pd.Timestamp, datetime.date)):
            return data.isoformat()
        elif isinstance(data, pd.Series):
            return data.tolist()
        elif pd.api.types.is_list_like(data):
            return list(data)
        elif pd.isna(data):
            return None
        else:
            return data

    def postprocess_df(self, widget):
        def clean_value(value):
            if isinstance(value, (pd.Timestamp, datetime.date)):
                return value.isoformat()
            elif isinstance(value, (uuid.UUID)):  # Add UUID handling
                return str(value)
            elif pd.isna(value):
                return None
            return value

        if 'rows' in widget:
            widget['rows'] = [{k: clean_value(v) for k, v in row.items()} 
                             for row in widget['rows']]
        return widget

    async def _build_schemas_context(self):
        context = []
        for data_source in self.data_sources:
            context.append(f"<data_source>: {data_source.name}</data_source>\n<data_source_type>: {data_source.type}</data_source_type>\n\n<schema>:")
            context.append(await data_source.prompt_schema(self.db))
            context.append("</schema>\n")
            context.append(f"<data_source_context>: \n Use this context as business context and rules for the data source\n{data_source.context}\n</data_source_context>")
        
        for file in self.files:
            context.append(file.prompt_schema())
        return "\n".join(context)

    async def _build_messages_context(self):
        context = []
        report_completions = await self.db.execute(select(Completion).filter(Completion.report_id == self.report.id).order_by(Completion.created_at.asc()))
        report_completions = report_completions.scalars().all()

        # Skip the last completion if it's from a user
        completions_to_process = report_completions[:-1] if report_completions and report_completions[-1].role == 'user' else report_completions

        for completion in completions_to_process:
            # Get widget if exists
            widget = None
            step = None
            if completion.widget_id:
                widget = await self.db.execute(select(Widget).filter(Widget.id == completion.widget_id))
                widget = widget.scalars().first()
            
            # Get step if exists
            if completion.step_id:
                step = await self.db.execute(select(Step).filter(Step.id == completion.step_id))
                step = step.scalars().first()

            # Format each message in a more structured way
            message = {
                "role": completion.role,
                "timestamp": completion.created_at.isoformat(),
                "content": completion.prompt['content'] if completion.role == 'user' else completion.completion['content'],
                "widget": widget.title if widget else None,
                "step": {
                    "title": step.title if step else None,
                    "code": step.code if step else None,
                    "data_model": step.data_model if step else None
                } if step else None
            }
            
            # Convert to a clean, readable format
            context_parts = [
                f"[Message #{len(context) + 1}]",
                f"Role: {message['role']}",
                f"Time: {message['timestamp']}",
                f"Widget: {message['widget'] or 'None'}"
            ]

            if message['step']:
                context_parts.extend([
                    "\nWidget Step Information:",
                    "\nData Model:",
                    json.dumps(message['step']['data_model'], indent=2) if message['step']['data_model'] else "None",
                    "\nCode:",
                    message['step']['code'] if message['step']['code'] else "None"
                ])

            context_parts.extend([
                "\nContent:",
                message['content'],
                "\n---"
            ])

            context.append("\n".join(context_parts))
        
        return "\n\n".join(context)

    async def _build_memories_context(self):
        context = []
        mentions = await self.db.execute(select(Mention).where(Mention.type == MentionType.MEMORY).where(Mention.completion_id == self.head_completion.id))
        mentions = mentions.scalars().all()

        for mention in mentions:
            memory = await self.db.execute(select(Memory).where(Memory.id == mention.object_id))
            memory = memory.scalars().first()
            # need to get Step-memory.step_id and then get the step.data_model
            step = await self.db.execute(select(Step).where(Step.id == memory.step_id))
            step = step.scalars().first()
            if step:
                data_model = step.data_model
                data_sample = step.data  # step.data is already a dictionary
                code = step.code

                # Create DataFrame from the 'rows' data
                df_cols = [col['field'] for col in data_sample['columns']]
                df = pd.DataFrame(data_sample['rows'], columns=df_cols)

                # Take the first 3 rows
                df = df.head(3)

                context.append(f"memory {memory.title}: \n memory data model: {
                               data_model}\n memory code: {code}\n memory data sample: {df.to_dict(orient='records')}")

        return "\n".join(context)
    
    async def _get_last_step(self):
        last_step = await self.db.execute(select(Step).where(Step.widget_id == self.widget.id).order_by(Step.created_at.desc()))
        last_step = last_step.scalars().first()
        return last_step

    def _apply_code_diff(self, diff_code, original_code):
        # Split the original code into lines

        # sample diff code:
        # [{"replace": "  df['column_X'] = df['column_X'] * 2\na = 5 \n b=5\nx=55 \n \n", "line_start": 1, "line_end": 5}, {"replace": "    db_clients[0].execute_query("SELECT * FROM table_X")", "line_start": 23, "line_end": 23}]
        # line start with 1

        original_lines = original_code.split('\n')
        diff_code_sorted = sorted(
            diff_code, key=lambda x: x['line_start'], reverse=True)

        for change in diff_code_sorted:
            original_lines[change['line_start'] -
                           1:change['line_end']] = change['replace'].split('\n')

        updated_code = '\n'.join(original_lines)

        return updated_code

    async def _apply_data_model_diff(self, previous_data_model, diff_data_model):
        updated_data_model = previous_data_model

        # Handle data_model wrapper if present
        if "data_model" in diff_data_model:
            diff_data_model = diff_data_model["data_model"]

        # Update chart type if specified
        if "type" in diff_data_model:
            updated_data_model["type"] = diff_data_model["type"]

        # Update series if specified
        if "series" in diff_data_model:
            updated_data_model["series"] = diff_data_model["series"]

        # Handle existing column modifications
        if "add_columns" in diff_data_model:
            for column_to_add in diff_data_model["add_columns"]:
                if not any(col["generated_column_name"] == column_to_add["generated_column_name"] for col in updated_data_model["columns"]):
                    updated_data_model["columns"].append(column_to_add)

        if "remove_columns" in diff_data_model:
            for column_to_remove in diff_data_model["remove_columns"]:
                updated_data_model["columns"] = [
                    col for col in updated_data_model["columns"] if col["generated_column_name"] != column_to_remove]

        if "transform_columns" in diff_data_model:
            for column_to_transform in diff_data_model["transform_columns"]:
                for col in updated_data_model["columns"]:
                    if col["generated_column_name"] == column_to_transform["generated_column_name"]:
                        col = column_to_transform

        return updated_data_model

    async def _format_code_with_line_numbers(self, code):
        # line numbers start from 1
        # create a dict with line numbers as keys and lines as values

        code_lines = code.split("\n")
        code_dict = {index + 1: line for index, line in enumerate(code_lines)}

        return code_dict
    
    async def new_message_router(self, new_message):
        historical_messages = await self._build_messages_context()

        prompt = f"""
        Given this historical messages:
        {historical_messages}

        and this latest message:
        {new_message}

        Respond with the next step to take, among the following options
        * table - for requests like that require building a data model. charts, graphs, tables.
            examples:
            - list of all X
            - show me all Y
            - show me @mention
            -  what is the average of Z
            - how many customers are there
            - what is the revenue
            - create a chart of all X
            - show me a chart of Y
            - show me a chart of @mention

        * modify - for modifying the current table/chart/data model.
            examples:
            - transform column X to Y
            - remove column X
            - add column Y
            - add a new column Z
            - change to pie chart
            - change to line chart
            - change to bar chart
            - change to area chart
            - change to revenue not amount
            - i meant for customers not clients

        * question - for any question that can be answered based on the schema of files and database, for example about memories, mentions, columns, relationships, data types, etc
            examples:
            - what is the data type of column X
            - what is the relationship between table A and table B
            - what is the meaning of @mention
            - what is the meaning of tag X
            - what are the implicit relationships in the data
            - how should i model this question
            - what is the best way to model this

        Respond in one word only, no markdown
        """
        response = self.llm.inference(prompt)

        return response

    def create_title_from_prompt(self, prompt):
        prompt = f"""
        Given this prompt:
        {prompt}

        Create a title that summarizes the prompt in up to 5 words.
        for example:
        * "Create a pie chart of all customers" -> "Customers Pie Chart"
        * "List all customers in database" -> "Customers List"
        * "Summarize all sales by month" -> "Sales Summary by Month"

        Respond with the title only, no markdown
        """

        response = self.llm.inference(prompt)

        return response