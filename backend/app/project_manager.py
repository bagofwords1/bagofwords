from app.models.completion import Completion
from app.models.text_widget import TextWidget
from sqlalchemy.orm import Session
import datetime
from app.schemas.completion_schema import PromptSchema
from typing import List, Optional
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionCreate, InstructionSchema
from app.models.instruction import Instruction
from app.models.widget import Widget
from app.models.step import Step
from app.models.plan import Plan
from app.models.report import Report
from sqlalchemy import select, delete
import logging
from app.services.table_usage_service import TableUsageService
from app.schemas.table_usage_schema import TableUsageEventCreate
from app.utils.lineage import extract_tables_from_data_model

# Agent execution tracking models
from app.models.agent_execution import AgentExecution
from app.models.plan_decision import PlanDecision
from app.models.tool_execution import ToolExecution
from app.models.context_snapshot import ContextSnapshot
from app.models.completion_block import CompletionBlock
from app.services.dashboard_layout_service import DashboardLayoutService
from app.schemas.dashboard_layout_version_schema import (
    DashboardLayoutBlocksPatch,
    BlockPositionPatch,
)
from app.services.visualization_service import VisualizationService
from app.services.query_service import QueryService
from app.schemas.visualization_schema import VisualizationCreate
from app.schemas.view_schema import ViewSchema, EncodingSchema, SeriesEncodingSchema

class ProjectManager:

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.table_usage_service = TableUsageService()
        self.visualization_service = VisualizationService()
        self.query_service = QueryService()

    async def emit_table_usage(self, db, report: Report, step: Step, data_model: dict, user_id: str | None = None, user_role: str | None = None, source_type: str | None = None):
        try:
            report_ds_ids = [str(ds.id) for ds in (getattr(report, 'data_sources', []) or [])]
            lineage_entries = await extract_tables_from_data_model(db, data_model, report_ds_ids)
            for entry in lineage_entries:
                ds_id = entry.get("datasource_id") or (report_ds_ids[0] if len(report_ds_ids) == 1 else None)
                table_name = entry.get("table_name")
                if not ds_id or not table_name:
                    continue
                table_fqn = table_name.lower()
                payload = TableUsageEventCreate(
                    org_id=str(report.organization_id),
                    report_id=str(report.id),
                    data_source_id=ds_id,
                    step_id=str(step.id),
                    user_id=user_id,
                    table_fqn=table_fqn,
                    datasource_table_id=entry.get("datasource_table_id"),
                    source_type=source_type or "sql",
                    columns=entry.get("columns") or [],
                    success=(step.status == "success"),
                    user_role=user_role,
                    role_weight=None,
                )
                await self.table_usage_service.record_usage_event(db=db, payload=payload)
        except Exception as e:
            self.logger.warning(f"emit_table_usage failed: {e}")

    async def emit_table_usage_from_tables_by_source(self, db, report: Report, step: Step, tables_by_source: list[dict] | None, user_id: str | None = None, user_role: str | None = None, source_type: str | None = "sql"):
        """Fallback emission when Step.data_model has no columns.

        Uses tool_input.tables_by_source to emit TableUsageEvent rows and upsert TableStats.
        - Skips entries without a resolvable data_source_id (unless the report has exactly one DS).
        - Looks up datasource_table_id for enrichment when possible.
        """
        try:
            if not tables_by_source or not isinstance(tables_by_source, list):
                return

            # Determine single-DS fallback if applicable
            report_ds_ids = [str(ds.id) for ds in (getattr(report, 'data_sources', []) or [])]

            # Local import to avoid broader import side-effects
            from sqlalchemy import select as _select, func as _func
            from app.models.datasource_table import DataSourceTable

            for group in tables_by_source:
                if not isinstance(group, dict):
                    continue
                ds_id = group.get("data_source_id")
                tables = group.get("tables") or []

                # Fallback to the sole report DS when not specified
                if not ds_id and len(report_ds_ids) == 1:
                    ds_id = report_ds_ids[0]
                if not ds_id:
                    # Cannot validate/access without a concrete data source id
                    continue

                for t in tables:
                    if not isinstance(t, str):
                        continue
                    table_name = t.strip()
                    if not table_name:
                        continue

                    # Resolve datasource_table_id when present in catalog
                    ds_table_id = None
                    resolved_fqn = table_name.lower()
                    try:
                        table_lower = table_name.lower()
                        stmt = _select(DataSourceTable).where(
                            DataSourceTable.datasource_id == ds_id,
                            (
                                (_func.lower(DataSourceTable.name) == table_lower)
                                | (_func.lower(DataSourceTable.name).like(f'%.{table_lower}'))
                            )
                        )
                        res = await db.execute(stmt)
                        row = res.scalar_one_or_none()
                        if row:
                            ds_table_id = str(row.id)
                            # Prefer the canonical name from catalog (often includes schema, e.g., 'public.customer')
                            try:
                                resolved_fqn = str(getattr(row, "name", resolved_fqn) or resolved_fqn).lower()
                            except Exception:
                                pass
                    except Exception:
                        ds_table_id = None

                    payload = TableUsageEventCreate(
                        org_id=str(report.organization_id),
                        report_id=str(report.id),
                        data_source_id=ds_id,
                        step_id=str(step.id),
                        user_id=user_id,
                        table_fqn=resolved_fqn,
                        datasource_table_id=ds_table_id,
                        source_type=source_type or "sql",
                        columns=[],
                        success=(step.status == "success"),
                        user_role=user_role,
                        role_weight=None,
                    )
                    await self.table_usage_service.record_usage_event(db=db, payload=payload)
        except Exception as e:
            self.logger.warning(f"emit_table_usage_from_tables_by_source failed: {e}")

    async def create_error_completion(self, db, head_completion, error):
        error_completion = Completion(model=head_completion.model,completion={"content": error, "error": True},
            prompt=None,
            status="error",
            parent_id=head_completion.id,
            message_type="error",
            role="system",
            report_id=head_completion.report_id if head_completion.report_id else None,
            widget_id=head_completion.widget_id if head_completion.widget_id else None,
            external_platform=head_completion.external_platform,
            external_user_id=head_completion.external_user_id
        )

        db.add(error_completion)
        await db.commit()
        await db.refresh(error_completion)
        return error_completion

    async def create_message(self, db, report, message=None, status="in_progress", reasoning=None, completion=None, widget=None, role="system", step=None, external_platform=None, external_user_id=None):
        completion_message = PromptSchema(content="", reasoning="")
        if message is not None:
            completion_message.content = message
        if reasoning is not None:
            completion_message.reasoning = reasoning     

        completion_message = completion_message.dict()

        new_completion = Completion(
            completion=completion_message,
            model="gpt4o",
            status=status,
            turn_index=0,
            parent_id=completion.id if completion else None,
            message_type="ai_completion",
            role=role,
            report_id=report.id,  # Assuming 'report' is an instance of the Report model
            widget_id=widget.id if widget else None,   # or pass a widget ID if available
            step_id=step.id if step else None,
            external_platform=external_platform,
            external_user_id=external_user_id
        )

        db.add(new_completion)
        await db.commit()
        await db.refresh(new_completion)

        return new_completion
    
    async def update_completion_with_step(self, db, completion, step):
        completion.step_id = step.id
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion

    async def update_completion_with_widget(self, db, completion, widget):
        completion.widget_id = widget.id
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
    
    async def update_message(self, db, completion, message=None, reasoning=None):
        # Handle the case where completion.completion might be a string
        if isinstance(completion.completion, str):
            completion.completion = {'content': message, 'reasoning': reasoning}
        else:
            # Create a new dictionary to ensure SQLAlchemy detects the change
            completion.completion = {
                **completion.completion,  # Spread existing completion data
                'content': message,
                'reasoning': reasoning
            }
        #  Mark as modified to ensure SQLAlchemy picks up the change
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
    
    async def create_widget(self, db, report, title):
        # LEGACY (widget-based): deprecated in favor of Query + Visualization
        widget = Widget(
            title=title,
            report_id=report.id,
            status="draft",
            x=0,
            y=0,
            width=5,
            height=9,
            slug=title.lower().replace(" ", "-")
        )

        db.add(widget)
        await db.commit()
        await db.refresh(widget)

        return widget

    # ==============================
    # Query / Visualization helpers (new)
    # ==============================

    async def create_query_v2(self, db, report, title: str):
        try:
            from app.schemas.query_schema import QueryCreate
            payload = QueryCreate(title=title, report_id=str(report.id))
            # Note: current QueryService will create a widget under the hood (transitional)
            q = await self.query_service.create_query(db, payload, organization_id=str(report.organization_id), user_id=str(getattr(report, 'user_id', None)) if hasattr(report, 'user_id') else None)
            return q
        except Exception as e:
            self.logger.warning(f"create_query_v2 failed: {e}")
            raise

    async def create_visualization_v2(self, db, report_id: str, query_id: str, title: str, view: dict | ViewSchema | None = None, status: str = "draft"):
        try:
            payload = VisualizationCreate(
                title=title or "",
                status=status or "draft",
                report_id=str(report_id),
                query_id=str(query_id),
                view=(view if isinstance(view, ViewSchema) else ViewSchema(**(view or {})))
            )
            v = await self.visualization_service.create(db, payload)
            return v
        except Exception as e:
            self.logger.warning(f"create_visualization_v2 failed: {e}")
            raise

    async def set_visualization_status(self, db, visualization, status: str):
        try:
            from app.schemas.visualization_schema import VisualizationUpdate
            patch = VisualizationUpdate(status=status)
            v = await self.visualization_service.update(db, str(visualization.id), patch)
            return v
        except Exception as e:
            self.logger.warning(f"set_visualization_status failed: {e}")
            return visualization

    async def set_query_default_step_if_empty(self, db, query, step_id: str):
        try:
            # Direct update to avoid service cross-deps
            from sqlalchemy import update as _update
            if not getattr(query, 'default_step_id', None):
                await db.execute(
                    _update(type(query)).where(type(query).id == str(query.id)).values(default_step_id=str(step_id))
                )
                await db.commit()
        except Exception:
            pass

    def derive_encoding_from_data_model(self, data_model: dict | None) -> dict | None:
        try:
            if not isinstance(data_model, dict):
                return None
            series = data_model.get("series") or []
            if isinstance(series, list) and len(series) > 0 and isinstance(series[0], dict):
                first = series[0]
                category = first.get("key")
                if not category:
                    return None
                if len(series) == 1:
                    enc = {"category": str(category)}
                    if first.get("value"):
                        enc["value"] = str(first.get("value"))
                    if first.get("name"):
                        enc["name"] = str(first.get("name"))
                    return enc
                multi = {"category": str(category), "series": []}
                for s in series:
                    if not isinstance(s, dict):
                        continue
                    name = s.get("name")
                    val = s.get("value")
                    item = {}
                    if name is not None:
                        item["name"] = str(name)
                    if val is not None:
                        item["value"] = str(val)
                    if item:
                        multi["series"].append(item)
                return multi
            return None
        except Exception:
            return None
    
    async def create_step(self, db, title, widget, step_type):
        step = Step(
            title=title,
            slug=title.lower().replace(" ", "-"),
            type=step_type,
            widget_id=widget.id,
            code="",
            data={},
            data_model={},
            status="draft"
        )

        db.add(step)
        await db.commit()
        await db.refresh(step)

        return step
    
    async def update_step_with_code(self, db, step, code):
        step.code = code
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_step_with_data(self, db, step, data):
        step.data = data
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_step_with_data_model(self, db, step, data_model):
        # LEGACY path still used to persist Step.data_model; preferred flow sets Query+Visualization
        step.data_model = data_model
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step

    async def ensure_step_default_view(self, db, step, theme_name: str | None = None, theme_overrides: dict | None = None):
        """Persist a minimal default view if none exists. Keep backend generic; frontend registry handles specifics."""
        try:
            existing_view = getattr(step, "view", None)
        except Exception:
            existing_view = None

        if existing_view and isinstance(existing_view, dict) and len(existing_view.keys()) > 0:
            return step

        # Minimal default; component-specific defaults live in the frontend
        default_view = { "theme": theme_name or "default" }
        if theme_overrides and isinstance(theme_overrides, dict) and theme_overrides:
            default_view["style"] = theme_overrides

        step.view = default_view
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_step_status(self, db, step, status, status_reason=None):
        step.status = status
        step.status_reason = status_reason
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step

    async def create_step_for_query(self, db, query, title: str, step_type: str, initial_data_model: dict | None = None):
        from app.models.step import Step
        import uuid as _uuid
        step = Step(
            title=title,
            slug=f"step-{_uuid.uuid4().hex[:8]}",
            type=step_type,
            widget_id=getattr(query, 'widget_id', None),
            query_id=str(query.id),
            code="",
            data={},
            data_model=initial_data_model or {},
            status="draft",
        )
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_widget_position_and_size(self, db, widget_id, x, y, width, height):
        # LEGACY (widget-based): deprecated in favor of visualization blocks in layout
        widget = await db.get(Widget, widget_id)
        widget.x = x
        widget.y = y
        widget.width = width
        widget.height = height
        widget.status = "published"

        db.add(widget)
        await db.commit()
        await db.refresh(widget)
        return widget
    
    async def create_text_widget(self, db, content, x, y, width, height, report_id):
        text_widget = TextWidget(
            content=content,
            x=x,
            y=y,
            width=width,
            height=height,
            report_id=report_id
        )

        db.add(text_widget)
        await db.commit()
        await db.refresh(text_widget)

        return text_widget
    
    async def delete_text_widgets_for_report(self, db, report_id):
        """Deletes all TextWidget entries associated with a given report_id."""
        stmt = delete(TextWidget).where(TextWidget.report_id == report_id)
        await db.execute(stmt)
        await db.commit()
        # No object to refresh after deletion
        print(f"Deleted existing text widgets for report {report_id}") # Optional logging

    async def append_block_to_active_dashboard_layout(self, db, report_id: str, block: dict):
        """Append or update a block in the active dashboard layout for the report.
        - For widget blocks, position existing widgets
        - For text_widget blocks, create a TextWidget if needed, then position it
        """
        try:
            layout_svc = DashboardLayoutService()
            # Ensure there is an active layout (will create minimal if missing)
            await layout_svc.get_or_create_active_layout(db, report_id)

            btype = (block or {}).get("type")
            x = int((block or {}).get("x", 0))
            y = int((block or {}).get("y", 0))
            width = int((block or {}).get("width", 6))
            height = int((block or {}).get("height", 6))

            patch = None
            if btype == "widget":
                wid = (block or {}).get("widget_id") or (block or {}).get("id")
                if wid:
                    # Pass through view_overrides if any
                    vov = (block or {}).get("view_overrides")
                    patch = BlockPositionPatch(
                        type="widget",
                        widget_id=str(wid),
                        x=x, y=y, width=width, height=height,
                        view_overrides=vov,
                    )
            elif btype == "text_widget":
                text_widget_id = (block or {}).get("text_widget_id")
                # Try to reuse an existing text widget with same content/geometry to avoid duplicates
                if not text_widget_id:
                    content = (block or {}).get("content", "")
                    try:
                        from sqlalchemy import select as _select
                        existing = await db.execute(
                            _select(TextWidget).where(
                                TextWidget.report_id == report_id,
                                TextWidget.content == content,
                                TextWidget.x == x,
                                TextWidget.y == y,
                                TextWidget.width == width,
                                TextWidget.height == height,
                            )
                        )
                        existing_tw = existing.scalars().first()
                    except Exception:
                        existing_tw = None
                    if existing_tw:
                        text_widget_id = str(existing_tw.id)
                    else:
                        tw = await self.create_text_widget(db, content, x, y, width, height, report_id)
                        text_widget_id = str(tw.id)
                if text_widget_id:
                    vov = (block or {}).get("view_overrides")
                    patch = BlockPositionPatch(
                        type="text_widget",
                        text_widget_id=text_widget_id,
                        x=x, y=y, width=width, height=height,
                        view_overrides=vov,
                    )

            # Visualization blocks (new, preferred)
            if patch is None and btype == "visualization":
                viz_id = (block or {}).get("visualization_id") or (block or {}).get("id")
                if viz_id:
                    vov = (block or {}).get("view_overrides")
                    patch = BlockPositionPatch(
                        type="visualization",
                        visualization_id=str(viz_id),
                        x=x, y=y, width=width, height=height,
                        view_overrides=vov,
                    )

            if patch is None:
                return None

            updated = await layout_svc.patch_active_layout_blocks(
                db, report_id, DashboardLayoutBlocksPatch(blocks=[patch])
            )
            return updated
        except Exception as e:
            self.logger.warning(f"append_block_to_active_dashboard_layout failed: {e}")
            return None

    async def get_active_dashboard_layout_blocks(self, db, report_id: str) -> list[dict]:
        """Return blocks for the active dashboard layout (or empty list)."""
        try:
            layout_svc = DashboardLayoutService()
            layout = await layout_svc.get_or_create_active_layout(db, report_id)
            return list(getattr(layout, "blocks", []) or [])
        except Exception as e:
            self.logger.warning(f"get_active_dashboard_layout_blocks failed: {e}")
            return []

    async def update_visualization_view(self, db, visualization, view: dict | ViewSchema):
        try:
            from app.schemas.visualization_schema import VisualizationUpdate
            patch = VisualizationUpdate(view=(view if isinstance(view, ViewSchema) else ViewSchema(**(view or {}))))
            from app.services.visualization_service import VisualizationService as _VS
            vs = _VS()
            updated = await vs.update(db, str(visualization.id), patch)
            return updated
        except Exception as e:
            self.logger.warning(f"update_visualization_view failed: {e}")
            return visualization
    
    async def update_report_title(self, db, report, title):
        # Instead of merging, let's fetch a fresh instance
        stmt = select(Report).where(Report.id == report.id)
        report = (await db.execute(stmt)).scalar_one()
        
        # Update the title
        report.title = title
        
        # Explicitly mark as modified
        db.add(report)
        await db.commit()
        await db.refresh(report)
        return report
    
    async def create_plan(self, db, report, content, completion):
        plan = Plan(
            content=content,
            completion_id=completion.id,
            report_id=report.id,
            organization_id=report.organization_id,
            user_id=completion.user_id
        )

        db.add(plan)
        await db.commit()
        await db.refresh(plan)

        return plan
    
    async def update_plan(self, db, plan, content):
        plan.content = content
        db.add(plan)
        await db.commit()
        await db.refresh(plan)
        return plan
    

    async def update_completion_status(self, db, completion, status):
        completion.status = status
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
        
    async def update_completion_scores(self, db, completion, instructions_score=None, context_score=None):
        """Update instructions and context effectiveness scores for a completion."""
        if instructions_score is not None:
            completion.instructions_effectiveness = instructions_score
        if context_score is not None:
            completion.context_effectiveness = context_score
        
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion

    async def update_completion_response_score(self, db, completion, response_score):
        """Update response score for a completion."""
        completion.response_score = response_score
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion

    async def create_instruction_from_draft(
        self,
        db,
        organization,
        text: str,
        category: str = "general",
        agent_execution_id: str = None,
        trigger_reason: str = None,
        ai_source: str | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        """
        Create a single draft instruction owned by the system (user_id=None).
        """
        try:
            clean_text = (text or "").strip()
            if not clean_text:
                raise ValueError("Instruction text cannot be empty")
            
            instruction = Instruction(
                text=clean_text,
                status="draft",
                category=category or "general",
                user_id=user_id,
                global_status="suggested",
                is_seen=True,
                agent_execution_id=agent_execution_id,
                trigger_reason=trigger_reason,
                ai_source=ai_source,
                organization_id=str(organization.id),
            )
            db.add(instruction)
            await db.commit()
            await db.refresh(instruction)

            return instruction
        except Exception as e:
            self.logger.warning(f"create_instruction_from_draft failed: {e}")
            raise

    # ==============================
    # Agent Execution Tracking Methods
    # ==============================

    async def start_agent_execution(self, db, completion_id, organization_id=None, user_id=None, report_id=None, config_json=None):
        """Start tracking an agent execution run."""
        from app.settings.config import settings
        
        execution = AgentExecution(
            completion_id=completion_id,
            organization_id=organization_id,
            user_id=user_id,
            report_id=report_id,
            status='in_progress',
            started_at=datetime.datetime.utcnow(),
            config_json=config_json or {},
            bow_version=settings.PROJECT_VERSION,
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)
        return execution

    async def save_plan_decision(self, db, agent_execution, seq, loop_index, plan_type=None, 
                               analysis_complete=False, reasoning=None, assistant=None, 
                               final_answer=None, action_name=None, action_args_json=None, 
                               metrics_json=None, context_snapshot_id=None):
        """Upsert a planner decision frame by (agent_execution_id, seq)."""
        stmt = select(PlanDecision).where(
            PlanDecision.agent_execution_id == agent_execution.id,
            PlanDecision.seq == seq,
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()

        if existing:
            existing.loop_index = loop_index
            existing.plan_type = plan_type
            existing.analysis_complete = analysis_complete
            existing.reasoning = reasoning
            existing.assistant = assistant
            existing.final_answer = final_answer
            existing.action_name = action_name
            existing.action_args_json = action_args_json
            existing.metrics_json = metrics_json
            existing.context_snapshot_id = context_snapshot_id
            db.add(existing)
            await db.commit()
            await db.refresh(existing)
            return existing

        decision = PlanDecision(
            agent_execution_id=agent_execution.id,
            seq=seq,
            loop_index=loop_index,
            plan_type=plan_type,
            analysis_complete=analysis_complete,
            reasoning=reasoning,
            assistant=assistant,
            final_answer=final_answer,
            action_name=action_name,
            action_args_json=action_args_json,
            metrics_json=metrics_json,
            context_snapshot_id=context_snapshot_id,
        )
        db.add(decision)
        await db.commit()
        await db.refresh(decision)
        return decision

    async def start_tool_execution(self, db, agent_execution, plan_decision_id, tool_name, 
                                  tool_action, arguments_json, attempt_number=1, max_retries=0):
        """Start tracking a tool execution."""
        tool_exec = ToolExecution(
            agent_execution_id=agent_execution.id,
            plan_decision_id=plan_decision_id,
            tool_name=tool_name,
            tool_action=tool_action,
            arguments_json=arguments_json,
            status='in_progress',
            started_at=datetime.datetime.utcnow(),
            attempt_number=attempt_number,
            max_retries=max_retries,
        )
        db.add(tool_exec)
        await db.commit()
        await db.refresh(tool_exec)
        return tool_exec

    async def finish_tool_execution(self, db, tool_execution, status, success, result_summary=None,
                                   result_json=None, created_widget_id=None, created_step_id=None, created_visualization_ids: list[str] | None = None,
                                   error_message=None, token_usage_json=None, context_snapshot_id=None):
        """Finish tracking a tool execution."""
        tool_execution.status = status
        tool_execution.success = success
        tool_execution.completed_at = datetime.datetime.utcnow()
        if tool_execution.started_at:
            tool_execution.duration_ms = (tool_execution.completed_at - tool_execution.started_at).total_seconds() * 1000.0
        tool_execution.result_summary = result_summary
        tool_execution.result_json = result_json
        tool_execution.created_widget_id = created_widget_id
        tool_execution.created_step_id = created_step_id
        # Merge created visualization ids into artifact_refs_json (list-based)
        try:
            if created_visualization_ids:
                refs = tool_execution.artifact_refs_json or {}
                vis_list = list(refs.get('visualizations') or [])
                for vid in created_visualization_ids:
                    if vid and vid not in vis_list:
                        vis_list.append(vid)
                refs['visualizations'] = vis_list
                tool_execution.artifact_refs_json = refs
        except Exception:
            pass
        tool_execution.error_message = error_message
        tool_execution.token_usage_json = token_usage_json
        tool_execution.context_snapshot_id = context_snapshot_id
        db.add(tool_execution)
        await db.commit()
        await db.refresh(tool_execution)
        return tool_execution

    # Pydantic-friendly helpers
    async def save_plan_decision_from_model(self, db, agent_execution, seq: int, loop_index: int,
                                           planner_decision_model, context_snapshot_id: str | None = None):
        to_dict = planner_decision_model.model_dump() if hasattr(planner_decision_model, 'model_dump') else dict(planner_decision_model)
        action = to_dict.get('action') or {}
        metrics = to_dict.get('metrics') or None
        return await self.save_plan_decision(
            db,
            agent_execution=agent_execution,
            seq=seq,
            loop_index=loop_index,
            plan_type=to_dict.get('plan_type'),
            analysis_complete=bool(to_dict.get('analysis_complete', False)),
            reasoning=to_dict.get('reasoning_message'),
            assistant=to_dict.get('assistant_message'),
            final_answer=to_dict.get('final_answer'),
            action_name=(action.get('name') if isinstance(action, dict) else getattr(action, 'name', None)),
            action_args_json=(action.get('arguments') if isinstance(action, dict) else getattr(action, 'arguments', None)),
            metrics_json=(metrics.model_dump() if hasattr(metrics, 'model_dump') else metrics),
            context_snapshot_id=context_snapshot_id,
        )

    async def start_tool_execution_from_models(self, db, agent_execution, plan_decision_id: str | None,
                                              tool_name: str, tool_action: str | None, tool_input_model,
                                              attempt_number: int = 1, max_retries: int = 0):
        args = tool_input_model.model_dump() if hasattr(tool_input_model, 'model_dump') else dict(tool_input_model)
        return await self.start_tool_execution(
            db,
            agent_execution=agent_execution,
            plan_decision_id=plan_decision_id,
            tool_name=tool_name,
            tool_action=tool_action,
            arguments_json=args,
            attempt_number=attempt_number,
            max_retries=max_retries,
        )

    async def finish_tool_execution_from_models(self, db, tool_execution,
                                               result_model=None,
                                               summary: str | None = None,
                                               created_widget_id: str | None = None,
                                               created_step_id: str | None = None,
                                               created_visualization_ids: list[str] | None = None,
                                               error_message: str | None = None,
                                               context_snapshot_id: str | None = None,
                                               success: bool = True):
        # Handle result_model appropriately
        if result_model and hasattr(result_model, 'model_dump'):
            # Pydantic model - convert to dict
            result_json = result_model.model_dump()
        elif result_model is not None:
            # Regular dict - make sure it's JSON-serializable
            import json
            try:
                # Test if it can be serialized to JSON
                json.dumps(result_model, default=str)
                result_json = result_model
            except (TypeError, ValueError) as e:
                # If serialization fails, create a safe version
                result_json = {
                    "error": "Failed to serialize tool output",
                    "message": str(e),
                    "safe_summary": str(result_model)[:1000] + "..." if len(str(result_model)) > 1000 else str(result_model)
                }
        else:
            result_json = None
            
        status = 'success' if success else 'error'
        return await self.finish_tool_execution(
            db,
            tool_execution=tool_execution,
            status=status,
            success=success,
            result_summary=summary,
            result_json=result_json,
            created_widget_id=created_widget_id,
            created_step_id=created_step_id,
            created_visualization_ids=created_visualization_ids,
            error_message=error_message,
            context_snapshot_id=context_snapshot_id,
        )

    async def save_context_snapshot(self, db, agent_execution, kind, context_view_json, 
                                   prompt_text=None, prompt_tokens=None):
        """Save a context snapshot."""
        import json
        from datetime import datetime
        
        # Custom JSON encoder for datetime objects
        def json_encoder(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
        
        # Ensure JSON serialization works by converting to string and back
        if isinstance(context_view_json, dict):
            json_str = json.dumps(context_view_json, default=json_encoder)
            context_view_json = json.loads(json_str)
        
        snapshot = ContextSnapshot(
            agent_execution_id=agent_execution.id,
            kind=kind,
            context_view_json=context_view_json,
            prompt_text=prompt_text,
            prompt_tokens=str(prompt_tokens) if prompt_tokens else None,
        )
        db.add(snapshot)
        await db.commit()
        await db.refresh(snapshot)
        return snapshot

    async def finish_agent_execution(self, db, agent_execution, status, first_token_ms=None, 
                                    thinking_ms=None, token_usage_json=None, error_json=None):
        """Finish an agent execution run."""
        # If metrics are not provided, derive them from PlanDecision rows
        if first_token_ms is None or thinking_ms is None or token_usage_json is None:
            try:
                from sqlalchemy import select
                from app.models.plan_decision import PlanDecision

                prompt_total = 0
                completion_total = 0
                total_tokens_total = 0
                derived_first_token_ms = None
                derived_thinking_ms = None

                stmt = select(PlanDecision).where(PlanDecision.agent_execution_id == str(agent_execution.id)).order_by(PlanDecision.seq.asc())
                rows = (await db.execute(stmt)).scalars().all()
                for pd in rows:
                    metrics = getattr(pd, 'metrics_json', None) or {}
                    token_usage = metrics.get('token_usage') or {}
                    try:
                        prompt_total += int(token_usage.get('prompt_tokens') or 0)
                        completion_total += int(token_usage.get('completion_tokens') or 0)
                        total_tokens_total += int(token_usage.get('total_tokens') or 0)
                    except Exception:
                        pass
                    if derived_first_token_ms is None:
                        ft = metrics.get('first_token_ms')
                        if isinstance(ft, (int, float)):
                            derived_first_token_ms = float(ft)
                    tm = metrics.get('thinking_ms')
                    if isinstance(tm, (int, float)):
                        derived_thinking_ms = float(tm)

                if token_usage_json is None and (prompt_total or completion_total or total_tokens_total):
                    token_usage_json = {
                        'prompt_tokens': prompt_total,
                        'completion_tokens': completion_total,
                        'total_tokens': total_tokens_total or (prompt_total + completion_total),
                    }
                if first_token_ms is None:
                    first_token_ms = derived_first_token_ms
                if thinking_ms is None:
                    thinking_ms = derived_thinking_ms
            except Exception:
                pass

        agent_execution.status = status
        agent_execution.completed_at = datetime.datetime.utcnow()
        if agent_execution.started_at:
            agent_execution.total_duration_ms = (agent_execution.completed_at - agent_execution.started_at).total_seconds() * 1000.0
        agent_execution.first_token_ms = first_token_ms
        agent_execution.thinking_ms = thinking_ms
        agent_execution.token_usage_json = token_usage_json
        agent_execution.error_json = error_json
        db.add(agent_execution)
        await db.commit()
        await db.refresh(agent_execution)
        return agent_execution

    async def next_seq(self, db, agent_execution):
        """Get next sequence number for streaming events."""
        agent_execution.latest_seq = (agent_execution.latest_seq or 0) + 1
        db.add(agent_execution)
        await db.commit()
        await db.refresh(agent_execution)
        return agent_execution.latest_seq

    # ==============================
    # Completion Blocks (Timeline Projection)
    # ==============================

    async def upsert_block_for_decision(self, db, completion, agent_execution, plan_decision: PlanDecision):
        """Create or update a render-ready block for a plan decision."""
        # Determine ordering and presentation
        block_index = int((plan_decision.seq or 0) * 10)
        title = f"Planning ({plan_decision.plan_type or 'unknown'})"
        status = 'completed' if plan_decision.analysis_complete else 'in_progress'
        icon = '🧠'
        # Project content rules:
        # - If analysis is complete: prefer final_answer, fall back to assistant
        # - If analysis is not complete: surface assistant text so the UI isn't stuck on "Thinking"
        if plan_decision.analysis_complete:
            content = plan_decision.final_answer or plan_decision.assistant or None
        else:
            content = plan_decision.assistant or None
        reasoning = plan_decision.reasoning or None

        # Try to find an existing block for this loop iteration
        stmt = select(CompletionBlock).where(
            CompletionBlock.agent_execution_id == agent_execution.id,
            CompletionBlock.loop_index == plan_decision.loop_index,
            CompletionBlock.source_type == 'decision',
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()

        if existing:
            # Update existing block with latest decision info
            existing.plan_decision_id = str(plan_decision.id)  # Update to latest decision ID
            existing.block_index = block_index
            existing.loop_index = plan_decision.loop_index
            existing.title = title
            existing.status = status
            existing.icon = icon
            existing.content = content
            existing.reasoning = reasoning
            if plan_decision.analysis_complete and not existing.completed_at:
                existing.completed_at = datetime.datetime.utcnow()
            db.add(existing)
            await db.commit()
            await db.refresh(existing)
            return existing

        block = CompletionBlock(
            completion_id=str(completion.id),
            agent_execution_id=str(agent_execution.id),
            source_type='decision',
            plan_decision_id=str(plan_decision.id),
            tool_execution_id=None,
            block_index=block_index,
            loop_index=plan_decision.loop_index,
            title=title,
            status=status,
            icon=icon,
            content=content,
            reasoning=reasoning,
            started_at=plan_decision.created_at,
            completed_at=plan_decision.updated_at if plan_decision.analysis_complete else None,
        )
        db.add(block)
        await db.commit()
        await db.refresh(block)
        return block

    async def upsert_block_for_tool(self, db, completion, agent_execution, tool_execution: ToolExecution):
        """Update existing decision block with tool execution data."""
        # Find the block for the related decision
        if not tool_execution.plan_decision_id:
            return None  # No decision to update
            
        # Find the decision block for this plan_decision_id to update with tool info
        stmt = select(CompletionBlock).where(
            CompletionBlock.agent_execution_id == agent_execution.id,
            CompletionBlock.plan_decision_id == tool_execution.plan_decision_id,
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        
        if not existing:
            return None  # No decision block to update
            
        # Update block with tool execution info
        existing.tool_execution_id = str(tool_execution.id)
        existing.title = f"{existing.title.split(' →')[0]} → {tool_execution.tool_name}"
        # Normalize status values for blocks
        if tool_execution.status == 'success':
            existing.status = 'completed'
        elif tool_execution.status == 'error':
            existing.status = 'error'
        else:
            existing.status = 'in_progress'
        existing.completed_at = tool_execution.completed_at
        
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        return existing

    async def rebuild_completion_from_blocks(self, db, completion, agent_execution):
        """Recompose transcript content/reasoning from stored blocks."""
        stmt = select(CompletionBlock).where(
            CompletionBlock.completion_id == completion.id
        ).order_by(CompletionBlock.block_index)
        blocks = (await db.execute(stmt)).scalars().all()

        content_parts = []
        reasoning_parts = []
        for b in blocks:
            if b.content:
                status_suffix = ' ✓' if b.status == 'completed' else ' ⏳' if b.status == 'in_progress' else ' ✗'
                content_parts.append(f"**{b.icon} {b.title}{status_suffix}**\n{b.content}")
            if b.reasoning:
                reasoning_parts.append(b.reasoning)

        # Ensure dict
        base = completion.completion if isinstance(completion.completion, dict) else {}
        completion.completion = {
            **base,
            'content': '\n\n'.join(content_parts),
            'reasoning': ' | '.join(reasoning_parts[-3:]) if reasoning_parts else base.get('reasoning') if isinstance(base, dict) else None,
        }
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion

    async def mark_error_on_latest_block(self, db, agent_execution, error_message: str | None = None):
        """Mark the latest decision block as error and append error message to its content."""
        from datetime import datetime
        stmt = select(CompletionBlock).where(
            CompletionBlock.agent_execution_id == agent_execution.id
        ).order_by(CompletionBlock.block_index.desc())
        block = (await db.execute(stmt)).scalar_one_or_none()
        if not block:
            return None
        block.status = 'error'
        if error_message:
            base = block.content or ''
            suffix = f"\n\nError: {error_message}"
            block.content = (base + suffix) if suffix not in base else base
        if not block.completed_at:
            block.completed_at = datetime.utcnow()
        db.add(block)
        await db.commit()
        await db.refresh(block)
        return block