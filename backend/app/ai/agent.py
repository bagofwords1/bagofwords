import datetime
import json
import pandas as pd
import numpy as np
from functools import wraps
import re
import uuid
import io
import sys
from contextlib import redirect_stdout
import asyncio

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

from app.settings.logging_config import get_logger
logger = get_logger("app.agent")

from app.ai.code_execution.code_execution import CodeExecutionManager
from app.websocket_manager import websocket_manager

class SigkillException(Exception):
    pass

class Agent:

    def __init__(self, db=None, organization_settings=None, report=None, model=None, head_completion=None, system_completion=None, widget=None, step=None, messages=[], main_router="table"):

        self.llm = LLM(model=model)
        self.organization_settings = organization_settings

        self.planner = Planner(model=model, organization_settings=self.organization_settings)
        self.answer = Answer(model=model, organization_settings=self.organization_settings)
        self.dashboard_designer = DashboardDesigner(model=model)
        self.project_manager = ProjectManager()
        self.coder = Coder(model=model, organization_settings=self.organization_settings)
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
        self.system_completion = system_completion  # Store the initial system completion

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

        # Create code execution manager with all required dependencies
        self.code_execution_manager = CodeExecutionManager(
            logger=logger,
            project_manager=self.project_manager,
            db=self.db,
            report=self.report,
            head_completion=self.head_completion if 'head_completion' in locals() else None,
            widget=self.widget,
            step=self.step,
            organization_settings=self.organization_settings
        )

        # Add event queue for sigkill detection
        self.sigkill_event = asyncio.Event()
        
        # Register websocket handler
        websocket_manager.add_handler(self._handle_completion_update)

    async def _handle_completion_update(self, message):
        try:
            data = json.loads(message)
            if (data['event'] == 'update_completion' and 
                data['completion_id'] == str(self.system_completion.id) and 
                data.get('sigkill') is not None):
                self.sigkill_event.set()
        except Exception as e:
            logger.error(f"Error handling completion update: {e}")

    async def main_execution(self):
        logger.info("Starting main execution")
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
            
            # Initialize observation data and tracking
            observation_data = None
            analysis_complete = False
            system_completion_used = False  # Flag to track if we've used system_completion
            first_reasoning_captured = ""
            analysis_step = 0

            # ReAct loop: Plan → Execute → Observe → Plan? 
            while (not analysis_complete or analysis_step < self.organization_settings.get_config("limit_analysis_steps").value):
                # Single check for sigkill
                if self.sigkill_event.is_set():
                    logger.info("Sigkill detected via websocket, stopping analysis")
                    break
                    
                action_results = {}  # Reset action results for each analysis step
                if observation_data is not None:
                    pass
                
                # 1. PLAN: Get actions from planner
                plan_generator = self.planner.execute(
                    schemas, 
                    self.persona, 
                    head_completion.prompt, 
                    memories, 
                    previous_messages,
                    observation_data,  
                    self.widget, 
                    self.step
                )
                
                current_plan = None
                plan_complete = False
                
                async for json_result in plan_generator:
                    if self.sigkill_event.is_set():
                        logger.info("Sigkill detected via websocket, stopping analysis")
                        break

                    if not json_result:
                        continue

                    # Update reasoning only on first iteration
                    if 'reasoning' in json_result and self.system_completion and first_reasoning_captured != json_result['reasoning'] and analysis_step == 0:
                        # Keep existing content but update reasoning
                        existing_content = self.system_completion.completion.get('content')
                        
                        await self.project_manager.update_message(
                            self.db,
                            self.system_completion,
                            message=existing_content,  # Preserve existing content
                            reasoning=json_result['reasoning']  # Update reasoning
                        )
                        first_reasoning_captured = json_result['reasoning']  # Mark reasoning as captured
                    
                    if 'plan' not in json_result or not isinstance(json_result['plan'], list):
                        continue
                        
                    current_plan = json_result
                    
                    # Only store plan when streaming is complete
                    if current_plan.get('streaming_complete', False) and not plan_complete:
                        plan_json = {
                            "reasoning": current_plan.get('reasoning', ''),
                            "analysis_complete": current_plan.get('analysis_complete', False),
                            "plan": current_plan.get('plan', []),
                            "streaming_complete": current_plan.get('streaming_complete', True),
                            "text": current_plan.get('text', ''),
                            "token_usage": current_plan.get('token_usage', {})
                        }
                        plan_json_str = json.dumps(plan_json)
                        await self.project_manager.create_plan(
                            self.db, 
                            self.report, 
                            plan_json_str, 
                            self.head_completion
                        )
                        plan_complete = True
                    
                    # Process each action in the plan
                    for i, action in enumerate(json_result['plan']):
                        if not isinstance(action, dict) or 'action' not in action:
                            continue

                        action_id = f"action_{i}"
                        # Initialize the action_results dictionary for this action_id if it doesn't exist
                        if action_id not in action_results:
                            action_results[action_id] = {
                                "prefix_completion": None,
                                "widget": None,
                                "step": None,
                                "completed": False
                            }

                        # Now we can safely access prefix_completion
                        if action.get('prefix') is not None:
                            # Skip if this is a completed answer action
                            if (action.get('action') == 'answer_question' and i > 0) and action_results[action_id].get('completed'):
                                continue
                            
                            # Handle completion creation/update
                            if action_results[action_id]['prefix_completion'] is None:
                                # Use system completion for first action in first analysis step
                                if analysis_step == 0 and self.system_completion and not system_completion_used:
                                    await self.project_manager.update_message(
                                        self.db,
                                        self.system_completion,
                                        action['prefix'],
                                        json_result['reasoning']
                                    )
                                    action_results[action_id]['prefix_completion'] = self.system_completion
                                    system_completion_used = True
                                else:
                                    # Create new completion for all other cases
                                    completion = await self.project_manager.create_message(
                                        report=self.report,
                                        db=self.db,
                                        message=action['prefix'],
                                        completion=self.head_completion,
                                        widget=self.widget,
                                        role="system",
                                        reasoning=json_result.get('reasoning')
                                    )
                                    action_results[action_id]['prefix_completion'] = completion
                            elif action_results[action_id]['prefix_completion'].completion.get('content') != action['prefix']:
                                if action_results[action_id].get('answer', None) is not None:
                                    continue

                                # Update existing completion if content changed
                                completion_obj = await self.project_manager.update_message(
                                    self.db,
                                    action_results[action_id]['prefix_completion'],
                                    action['prefix'],
                                    json_result.get('reasoning')
                                )
                                action_results[action_id]['prefix_completion'] = completion_obj

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
                                
                                # Create a completion if needed
                                if action_results[action_id]['prefix_completion'] is None:
                                    completion = await self.project_manager.create_message(
                                        report=self.report,
                                        db=self.db,
                                        message=action.get('prefix', f"Creating {action['details']['title']}..."),
                                        completion=self.head_completion,
                                        widget=widget,
                                        role="system"
                                    )
                                    action_results[action_id]['prefix_completion'] = completion
                                
                                # Update completion with widget if it exists
                                if action_results[action_id]['prefix_completion'] is not None and action_results[action_id]['widget'] is not None:
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
                                else:
                                    continue

                        elif action_type == 'modify_widget':
                            # Add this block before attempting updates
                            if action_results[action_id]['prefix_completion'] is None:
                                if self.system_completion:
                                    completion = self.system_completion
                                else:
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
                                # Extract question from the prompt if not provided in details
                                action['details']['extracted_question'] = head_completion.prompt['content']
                                
                            # Create prefix completion if it doesn't exist (following pattern from other actions)
                            if action_results[action_id]['prefix_completion'] is None:
                                if self.system_completion:
                                    completion = self.system_completion
                                else:
                                    completion = await self.project_manager.create_message(
                                        report=self.report,
                                        db=self.db,
                                        message=action['prefix'],
                                        reasoning=json_result.get('reasoning'),
                                        completion=self.head_completion,
                                        widget=None,
                                        role="system"
                                )
                                action_results[action_id]['prefix_completion'] = completion

                            try:
                                question = action['details']['extracted_question']
                                full_answer = action['prefix'] + " "

                                # Build observation data for the answer
                                current_observation_data = {
                                    "widgets": []
                                }
                                
                                # Include all completed widgets in observation data
                                report_widgets_context = await self._built_report_widgets_context()
                                current_observation_data["widgets"] = report_widgets_context
                                async for chunk in self.answer.execute(
                                    prompt=self.head_completion.prompt,
                                    schemas=schemas,
                                    memories=memories,
                                    previous_messages=previous_messages,
                                    widget=self.widget,
                                    observation_data=current_observation_data
                                ):
                                    full_answer += chunk
                                    # Update message with progress
                                    await self.project_manager.update_message(
                                        self.db,
                                        action_results[action_id]['prefix_completion'],
                                        full_answer,
                                        json_result.get('reasoning')
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
                            widgets_steps = await self._get_report_widgets_and_steps(self.report.id)
                            widgets = [x[0] for x in widgets_steps if x[0] is not None]
                            steps = [x[1] for x in widgets_steps if x[1] is not None]

                            if not widgets or not steps:
                                print("design_dashboard action has no widgets or steps")
                                continue

                            if action_results[action_id]['prefix_completion'] is None:
                                if self.system_completion:
                                    dashboard_completion = self.system_completion
                                else:
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

                            await self.project_manager.update_message(
                                self.db,
                                action_results[action_id]['prefix_completion'],
                                "Now putting it together and creating a dashboard.. and it's ready!"
                            )

                            action_results[action_id] = {
                                "prefix_completion": action_results[action_id]['prefix_completion'],
                                "widget": None,
                                "step": None,
                                "completed": True
                            }

                        # Check if this action requires observation
                        requires_observation = action.get('requires_observation', False)
                        if requires_observation and action_results[action_id]["completed"]:
                            # Stop processing more actions if observation is required
                            break
                
                # Check if analysis is complete
                if current_plan.get("analysis_complete") == True:
                    analysis_complete = True
                    break
                
                # OBSERVE: Build observation data for ALL widgets, not just the last one
                observation_data = {
                    "widgets": []
                }
                
                # Find all completed actions with widgets
                for action_id, result in sorted(action_results.items(), key=lambda x: x[0]):
                    if result["completed"] and result.get("widget") and result.get("step"):
                        widget_data = await self._build_observation_data(result["widget"], result["step"])
                        observation_data["widgets"].append(widget_data)
                
                if observation_data["widgets"]:
                    # Create an observation message
                    pass
                else:
                    # If no widget/step was created, end the loop
                    analysis_complete = True

                analysis_step += 1

            first_completion = await self.db.execute(select(Completion).filter(Completion.report_id == self.report.id).order_by(Completion.created_at.asc()).limit(1))
            first_completion = first_completion.scalar_one_or_none()

            if self.head_completion.id == first_completion.id:
                title = await self.reporter.generate_report_title(previous_messages, current_plan['plan'])
                await self.project_manager.update_report_title(self.db, self.report, title)
            
            # Final status check
            if self.sigkill_event.is_set():
                status = 'sigkill'
            else:
                status = 'success'

            await self.project_manager.update_completion_status(self.db, self.system_completion, status)
            logger.info("Main execution completed")
            
            # Clean up
            websocket_manager.remove_handler(self._handle_completion_update)
            
            return action_results
        
        except Exception as e:
            error = await self.project_manager.create_error_completion(self.db, self.head_completion, str(e))
            print(f"Error in main_execution: {e}")
            raise e


    async def _handle_generate_widget_data(self, prompt, action, widget, step):
        try:
            # Initialize data_model at the start
            data_model = None
            



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

            # Ensure we have a data model before proceeding
            if not data_model:
                await self.project_manager.update_step_status(self.db, step, "error")
                await self.project_manager.create_message(
                    report=self.report,
                    db=self.db,
                    message="Failed to generate data model",
                    completion=self.head_completion,
                    widget=self.widget,
                    role="ai_agent"
                )
                return False  # Return False to continue the loop

            # Prepare context for code generation
            schemas = await self._build_schemas_context()
            memories = await self._build_memories_context()
            previous_messages = await self._build_messages_context()

            # Setup validator function if enabled
            validator_fn = None
            if self.organization_settings.get_config("validator").value:
                validator_fn = self.coder.validate_code
            

            # Execute the full process: generate -> validate -> execute with retries
            df, final_code, code_and_error_messages = await self.code_execution_manager.generate_and_execute_with_retries(
                data_model=data_model,
                code_generator_fn=self.coder.data_model_to_code,
                validator_fn=validator_fn,
                max_retries=3,
                db_clients=self.clients,
                excel_files=self.files,
                prompt=prompt,
                schemas=schemas,
                ds_clients=self.clients,
                memories=memories,
                previous_messages=previous_messages,
                prev_data_model_code_pair=None
            )

            await self.project_manager.update_step_with_code(self.db, step, final_code)
            
            # Handle validation and execution messages
            for code, error_msg in code_and_error_messages:
                if "Validation failed" in error_msg:
                    await self.project_manager.create_message(
                        report=self.report,
                        db=self.db,
                        message=error_msg,
                        completion=self.head_completion,
                        widget=self.widget,
                        role="ai_agent"
                    )
            
            # Handle the final outcome
            if df.empty and code_and_error_messages:
                await self.project_manager.update_step_status(self.db, step, "error")
                await self.project_manager.create_message(
                    report=self.report,
                    db=self.db,
                    message="I faced some issues while generating data. Can you try explaining again?",
                    completion=self.head_completion,
                    widget=self.widget,
                    role="ai_agent"
                )
            else:
                await self.project_manager.update_step_status(self.db, step, "success")
            
            # Format data and update step
            widget_data = self.code_execution_manager.format_df_for_widget(df)
            
            await self.project_manager.update_step_with_data(self.db, step, widget_data)
            
            return True

        except Exception as e:
            # Log the error
            print(f"Error in _handle_generate_widget_data: {e}")
            
            # Update step status and create error message
            await self.project_manager.update_step_status(self.db, step, "error")
            await self.project_manager.create_message(
                report=self.report,
                db=self.db,
                message=f"An error occurred while generating data: {str(e)}",
                completion=self.head_completion,
                widget=self.widget,
                role="ai_agent"
            )
            
            # Return False to continue the loop
            return False

    async def _handle_modify_widget(self, prompt, widget, action):
        # Similar approach as _handle_generate_widget_data
        if not action.get('action_end', False):
            return False
            
        data_model_diff = action['details']
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

        # Prepare context 
        schemas = await self._build_schemas_context()
        memories = await self._build_memories_context()
        previous_messages = await self._build_messages_context()
        
        # Setup validator function if enabled
        validator_fn = None
        if self.organization_settings.get_config("validator").value:
            validator_fn = self.coder.validate_code
        # Execute the full process
        df, final_code, code_and_error_messages = await self.code_execution_manager.generate_and_execute_with_retries(
            data_model=updated_data_model,
            code_generator_fn=self.coder.data_model_to_code,
            validator_fn=validator_fn,
            max_retries=3,
            db_clients=self.clients,
            excel_files=self.files,
            prompt=prompt,
            schemas=schemas,
            ds_clients=self.clients,
            memories=memories,
            previous_messages=previous_messages,
            prev_data_model_code_pair=prev_data_model_code_pair
        )
        
        # Handle errors and success similar to _handle_generate_widget_data
        if df.empty and code_and_error_messages:
            await self.project_manager.update_step_status(self.db, step, "error")
            await self.project_manager.create_message(
                report=self.report,
                db=self.db,
                message="I faced some issues while modifying the widget. Can you try explaining again?",
                completion=self.head_completion,
                widget=self.widget,
                role="ai_agent"
            )
        else:
            await self.project_manager.update_step_status(self.db, step, "success")
            
        # Format and update
        widget_data = self.code_execution_manager.format_df_for_widget(df)
        await self.project_manager.update_step_with_code(self.db, step, final_code)
        await self.project_manager.update_step_with_data(self.db, step, widget_data)
        
        return True

    async def _build_schemas_context(self):
        context = []
        for data_source in self.data_sources:
            context.append(f"<data_source>: {data_source.name}</data_source>\n<data_source_type>: {data_source.type}</data_source_type>\n\n<schema>:")
            context.append(await data_source.prompt_schema(self.db, self.head_completion.prompt))
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
    
    async def _get_report_widgets_and_steps(self, report_id):
    
        result = []
        widgets = await self.db.execute(select(Widget).where(Widget.report_id == report_id))
        widgets = widgets.scalars().all()
        
        for widget in widgets:
            # Get the latest step for each widget
            latest_step = await self.db.execute(
                select(Step)
                .filter(Step.widget_id == widget.id)
                .order_by(Step.created_at.desc())
                .limit(1)
            )
            latest_step = latest_step.scalar_one_or_none()
            
            if latest_step:
                # Only add to context if we have both widget and step
                result.append([widget, latest_step])

        return result

    async def _built_report_widgets_context(self):
        # Get all widgets for the report
        widgets = await self._get_report_widgets_and_steps(self.report.id)

        # Only add to context if we have both widget and step
        context = []
        for widget, latest_step in widgets:
            context.append(await self._build_observation_data(widget, latest_step))
        
        return context

    async def _build_observation_data(self, widget, step):
        """Build structured observation data from widget and step results"""
        if not widget or not step:
            logger.warning("Cannot build observation data: widget or step is None")
            return {
                "widget_title": "N/A",
                "widget_type": "unknown",
                "data_preview": "No data available",
                "stats": {}
            }
        
        # Check if we're allowed to share data with LLM
        allow_llm_see_data = self.organization_settings.get_config("allow_llm_see_data").value
        
        observation_data = {
            "widget_id": widget.id,
            "widget_title": widget.title,
            "widget_type": "unknown",
            "step_id": step.id,
            "step_title": step.title,
        }
        
        # Safely get data model type
        if step.data_model and isinstance(step.data_model, dict):
            observation_data["widget_type"] = step.data_model.get("type", "unknown")
            observation_data["data_model"] = step.data_model
        
        # Always include metadata about the data
        if step.data and isinstance(step.data, dict):
            # Add metadata
            if "info" in step.data:
                observation_data["stats"] = step.data["info"]
            
            if "columns" in step.data:
                observation_data["column_names"] = [col["field"] for col in step.data["columns"]]
            
            if "rows" in step.data:
                observation_data["row_count"] = len(step.data["rows"])
                # Include the actual data rows
                observation_data["data"] = step.data["rows"]
            
            # Only include formatted preview if allowed
            if allow_llm_see_data and "rows" in step.data and "columns" in step.data:
                try:
                    # Create data preview with limited rows
                    columns = [col["field"] for col in step.data["columns"]]
                    preview_rows = step.data["rows"][:5]  # First 5 rows
                    
                    # Format preview as table
                    preview_lines = []
                    preview_lines.append(" | ".join(columns))
                    preview_lines.append("-" * (sum(len(col) for col in columns) + 3 * len(columns)))
                    
                    for row in preview_rows:
                        preview_lines.append(" | ".join(str(row.get(col, "N/A")) for col in columns))
                    
                    observation_data["data_preview"] = "\n".join(preview_lines)
                except Exception as e:
                    logger.error(f"Error building data preview: {e}")
        
        return observation_data
